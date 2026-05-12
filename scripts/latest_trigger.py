#!/usr/bin/env python

# Bring the entire array up or down, or just run a status report

import subprocess
import argparse
import traceback
from pathlib import Path

import pandas as pd
import numpy as np

import hamma


def get_latest_trigger():
    # numeric return suggests an error
    # on success, return a dict of primatives
    import pandas as pd

    from hamma.header.utilities import base_trigger_time

    vals = dict()

    header_path = Path('/home/pi/brokkr/hamma/headers')

    if not header_path.exists():
        return 1
    try:
        latest_file = max(header_path.glob('*.csv'), key=lambda p: p.stat().st_ctime)
        brokkr_hdrs = pd.read_csv(latest_file)

        latest_hdr = brokkr_hdrs.iloc[-1]

        # Some of this is copied from hamma repo...needs to be refactored
        # The fields match what is in brokkr, not hamma

        # NOTE: WE CAN ONLY RETURN PRIMATIVES

        vals['threshold'] = (5./4096)*latest_hdr['threashold_1']/6.024
        vals['num_sat'] = (latest_hdr['gps_satellites'] & np.sum(2**(np.arange(4)+4))) >> 4

        time = base_trigger_time(latest_hdr['gps_week'],
                                 np.floor(latest_hdr['gps_time_week']) + 1,
                                 latest_hdr['gps_utc_offset'])
        vals['time'] = time

        # Convert the brokkr header to a raw header, as defined by HAMMA
        # hamma_hdr = convert_brokkr_header(brokkr_hdrs)

    except Exception as e:
        print("Error occurred: " + str(e))
        print(traceback.format_exc())
        return 2

    return vals

def convert_brokkr_header(br_hdr):
    from hamma.header.versions import version20

    rename_dict = {
        'firmware_version': 'firmware',
        'board_id': 'boardID',
        'payload_size': 'datasize',
        'channel_count': 'channels',
        'temperature': 'temp1',
        'humidity': 'temp2',
        'watchdog_counter': 'watchdog',
        'fifo_counter': 'fifo_overflow',
        'threashold_1': 'thresh1',
        'threashold_2': 'thresh2',
        'threashold_3': 'thresh3',
        'threashold_4': 'thresh4',
        'threashold_5': 'thresh5',
        'threashold_6': 'thresh6',
        'threashold_7': 'thresh7',
        'threashold_8': 'thresh8',
        # 'block_type': '',
        # 'block_size': '',
        'trigger_position': 'triggerPos',
        'sub_position': 'triggerSubPos',
        'retrigger_position': 'reTriggerPos',
        'trigger_channel': 'triggerChannel',
        'gps_lat': 'lat',
        'gps_lon': 'lon',
        'gps_alt': 'alt',
        'gps_time_week': 'gpsTimeWeek',
        'gps_week': 'gpsWeek',
        'gps_utc_offset': 'gpsOffset',
        'gps_status_code': 'gpsStatusx46',
        'gps_health': 'gpsStatusx6D',
        'gps_satellites': '',
        'gps_subsecond': 'gpsSubSecond',
        'gps_ecc': 'gpsSubSecondECC',
        'time_tag_position': 'timeTagPos',
        'trigger_mask': 'triggerChannelMask',
        'packet_sequence': 'packetSequence',
    }

    raw_hdr = br_hdr.rename(columns=rename_dict)
    print(raw_hdr.iloc[-1])
    new_hdr = version20.convert(raw_hdr.iloc[-1])

    return new_hdr


def main():
    print(get_latest_trigger())
    # arg_parser = argparse.ArgumentParser(description="Bring the array up or down")
    #
    # arg_parser.add_argument("-p",
    #                         dest="ports",
    #                         action="append",  # if argparse 3.8, then can use extend
    #                         help="Which ports to bring up/down, mod 10000")
    # # NOTE: PASS multiple -p for multiple ports until argparse -> 3.8
    #
    # # grp = arg_parser.add_mutually_exclusive_group(required=True)
    #
    # arg_parser.add_argument("--status",
    #                  help="Get a status report",
    #                  dest="do_status",
    #                  action='store_true',
    #                  default=False,
    #                  )
    #
    # grp = arg_parser.add_mutually_exclusive_group()
    # grp.add_argument("--up",
    #                  dest='bring_up',
    #                  action="store_true",
    #                  default=False,
    #                  help="Bring array up",
    #                  )
    #
    # grp.add_argument("--down",
    #                  dest='bring_down',
    #                  action="store_true",
    #                  default=False,
    #                  help="Bring array down",
    #                  )
    #
    # parsed_args = arg_parser.parse_args()
    #
    # if parsed_args.do_status:
    #     _ = status_array(ports=parsed_args.ports)
    # elif parsed_args.bring_up | parsed_args.bring_down:
    #     # Is it up or down?
    #     bring_up = parsed_args.bring_up
    #
    #     updown_array(parsed_args.bring_up, ports=parsed_args.ports)
    # else:
    #     pass


if __name__ == '__main__':
    main()
