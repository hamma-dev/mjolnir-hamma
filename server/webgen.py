#!/usr/bin/env python

# Generate HTML Status Page

import argparse
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np

BASE_WEBSITE_PATH = Path('/var/www/hamma.dev/public_html')

def read_latest_log_file(log_path):
    # Find the latest log file and read in the most recent valid data.
    # Format the recent valid data to be passed into HTML generator
    # file - Pathlib file

    latest_file = max(log_path.glob('*.csv'), key=lambda p: p.stat().st_ctime)
    data = pd.read_csv(latest_file)

    log_time = data.time.iloc[-1]

    # For now, we won't use the log times....
    data.drop(columns='time', inplace=True)

    # The log file is "flat" - all attrs from all sensors are on one line.
    # So, we have some work to do.

    # Start with the column names:
    cols = data.columns.to_list()

    # Sensor name is at beginning of column. Get them (uniquely) and sort:
    sensor_names = [c[0:10] for c in cols]
    sensor_names = list(set(sensor_names))
    sensor_names.sort()

    # Now, find the unique attrs. It's also in the fields, but at the end:
    attrs = [c[11:] for c in cols]  # start at 11 to avoid leading whitespace

    # We want to preserve order, so use Pandas which does so:
    ##### NOTE: We might want to do this for the sensor names too?
    attrs = pd.unique(attrs).tolist()

    # For the output, we want the data for one sensor on one row
    val_df = pd.DataFrame(index=sensor_names, columns=attrs)

    for name in sensor_names:
        _df = data.filter(regex=name)

        # For the attributes, always get the latest value
        for _at in ["Mjolnir Up", "Brokkr Up", "Sindri Up", "Sensor Up"]:
            val_df.loc[name][_at] = _df[f"{name} {_at}"].iloc[-1]

        # For these, we want the last non-nan value
        for _at in ["Last trigger", "GPS Satellites", "Threshold"]:
            this_col = _df[f"{name} {_at}"]
            if _at == "Last trigger":
                this_col = this_col.astype('datetime64[ns]')

            last_val_ind = this_col.last_valid_index()
            last_val = this_col.iloc[last_val_ind] if last_val_ind is not None else np.nan

            val_df.loc[name][_at] = last_val

    return val_df, log_time


def format_log_data(data):
    # Format a log dataframe for web

    # data['Threshold'] = [f"{val:.2f}" for val in df['Threshold']]
    #
    pass


def _body(values, log_time, plot=None):
    # values is dataframe -
    # log_time is a string that has time with time zone and fractional seconds
    # plot should be the array string name

    current_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    _l_time = log_time.split('.')[0]

    body = f"""
    <body>
    <p>Latest {plot.upper() if plot is not None else 'Array'} Status as of {_l_time}; Page creation time: {current_time}Z</p>

    <table border="1" style="border-collapse: collapse; width: 100%;"
    <tbody>
    """

    # Header row:
    body += "<tr>"
    body += f"<td align='center'>Sensor</td>"
    for attr in values.columns:
        body += f"<td align='center'>{attr}</td>"

    if plot is not None:
        body += f"<td align='center'>Recent Plot</td>"
    body += "</tr>"

    # TODO: We really need a better way to do this
    plot_loc = 'https://www.hamma.dev/latest'

    # Start with the data rows
    for name, row_val in values.iterrows():
        # TODO: We really need a better way to do this (too)
        s_num = name[-2:]
        plot_name = f"hamma{s_num}.png"
        site_name = f"hamma{int(s_num)}"  # no zero fill

        body += "<tr>"
        # Here, add a link to the main page to the name
        main_page = f'https://www.hamma.dev/{site_name}'
        body += f"<td align='center'><a href='{main_page}'>{name}</td>"

        for _col_name, _val in row_val.items():
            cell_opts = "align='center'"
            if _col_name in ['Mjolnir Up', 'Brokkr Up', 'Sindri Up', 'Sensor Up']:
                if not _val:
                    cell_opts += ' style="background-color:#C86464"'
                else:
                    cell_opts += ' style="background-color:#8CC882"'
            # elif _col_name == 'GPS Satellites':
            #     if _val < 6:

            body += f"<td {cell_opts}>{_val}</td>"

        if plot is not None:
            link_loc = f"{plot_loc}/{plot_name}"
            body += f"<td align='center'><a href='{link_loc}'><img class='plot' height='100' src='{link_loc}' alt='{plot_name}'> </a></td>"

        body += "</tr>"

    body += """

    </tbody>
    </table>
    </body>
    """

    return body


def run_once(
        log_path,
        array='hamma',
        output_file=BASE_WEBSITE_PATH / 'test2.html'
        ):

    # log path - path to where logs are found

    # eventually, be able to pass in different sensor arrays

    values, log_time = read_latest_log_file(log_path)

    html_code = f"""
    <html xmlns="http://www.w3.org/1999/xhtml">
    <head>
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
    <meta http-equiv="refresh" content="600" />

    <title>Latest {array.upper()} Status</title>
    <style type="text/css">
    <!--
    .plot {{
      max-height: 100%;
      width: auto;
    }}
    -->
    </style>

    </head>
    """

    html_code += _body(values, log_time, plot=array)

    html_code += f"""
    </html>
    """

    with open(output_file, 'w') as f:
        f.write(html_code)


def main():
    # print(read_latest_log_file())

    arg_parser = argparse.ArgumentParser(
        description='Generate status page for website')

    arg_parser.add_argument(
        "-a",
        dest="array",
        help="Which array?",
    )

    parsed_args = arg_parser.parse_args()

    if parsed_args.array == 'pamma':
        output_file = BASE_WEBSITE_PATH / 'latest/pamma_status.html'
        log_path = Path('/home/monitor/brokkr/hamma/status/pamma')
    elif parsed_args.array == 'hamma':
        output_file =BASE_WEBSITE_PATH / 'latest/hamma_status.html'
        log_path = Path('/home/monitor/brokkr/hamma/status/')
    elif parsed_args.array == 'aumma':
        output_file =BASE_WEBSITE_PATH / 'latest/aumma_status.html'
        log_path = Path('/home/monitor/brokkr/hamma/status/aumma')
    else:
        print('No array specified')
        return


    run_once(log_path, output_file=output_file, array=parsed_args.array)

if __name__ == '__main__':
    main()

# <style type="text/css">
# <!--
# .row {
#   display: flex;
#   flex-direction: row;
#   flex-wrap: wrap;
#   width: 100%;
#   align-items: center;
#   margin: 5px;
# }
#
# .column {
#   display: flex;
#   flex-direction: column;
#   flex-basis: 100%;
#   flex: 1;
# }
# .text_column {
#   display: flex;
#   flex-direction: column;
#   flex-basis: 50%;
#   flex: 0.25;
# }
#
# -->
# </style>
#
#


#

#
# <div >
#   <div class='row'>
#     <div class='text_column'>
#       <div>
#         HAMMA 1
#       </div>
#     </div>
#     <div class='column'>
#       <div>
#         <img src="hamma01.png" alt="HAMMA 1 Plot">
#       </div>
#     </div>
#   </div>
#   <div class='row'>
#     <div class='text_column'>
#       <div>
#         HAMMA 2
#       </div>
#     </div>
#     <div class='column'>
#       <div>
#         <img src="hamma02.png" alt="HAMMA 2 Plot">
#       </div>
#     </div>
#   </div>
#   <div class='row'>
#     <div class='text_column'>
#       <div>
#         HAMMA 3
#       </div>
#     </div>
#     <div class='column'>
#       <div>
#         <img src="hamma03.png" alt="HAMMA 3 Plot">
#       </div>
#     </div>
#   </div>
#   <div class='row'>
#     <div class='text_column'>
#       <div>
#         HAMMA 4
#       </div>
#     </div>
#     <div class='column'>
#       <div>
#         <img src="hamma04.png" alt="HAMMA 4 Plot">
#       </div>
#     </div>
#   </div>
#   <div class='row'>
#     <div class='text_column'>
#       <div>
#         HAMMA 5
#       </div>
#     </div>
#     <div class='column'>
#       <div>
#         <img src="hamma05.png" alt="HAMMA 5 Plot">
#       </div>
#     </div>
#   </div>
#
# </div>
#
#
#
