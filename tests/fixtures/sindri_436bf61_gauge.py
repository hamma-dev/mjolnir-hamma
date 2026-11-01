"""VERBATIM copy of sindri's dashboard gauge code, hamma-dev/sindri @ 436bf61.

src/sindri/website/generate.py: DASHBOARD_DATA_ARGS_DEFAULT, safe_nan,
get_dashboard_plot_data -- extracted by AST node, not retyped. This is the code
every unit runs (sindri 0.3.x @ 436bf61, verified fleet-wide 2026-10-29), so the
website gauge tests exercise the real contract instead of a hand-written copy.
Re-extract if the deployed sindri changes. Do not edit by hand.
"""

import copy

import numpy as np

DASHBOARD_DATA_ARGS_DEFAULT = {
    "data_functions": [
        lambda base_data, data_args: base_data.iloc[-1],
        lambda base_data, data_args: (
            base_data.last(data_args["delta_period"]).iloc[0]),
        lambda base_data, data_args: (
            base_data.last(data_args["threshold_period"])),
        ],
    "delta_period": "1H",
    "overlay_functions": [lambda value: value for __ in range(3)],
    "threshold_period": "24H",
    "threshold_type": None,
    }


def safe_nan(value):
    if not np.isfinite(value):
        return None
    return value


def get_dashboard_plot_data(full_data, plot_type, plot_data):
    if not plot_type:
        return None

    data_args = copy.deepcopy(DASHBOARD_DATA_ARGS_DEFAULT)
    data_args.update(**plot_data)

    if data_args.get("unit_id", None) is not None:
        try:
            full_data = full_data[data_args["unit_id"]]
        except KeyError:
            print(f'Unit {data_args["unit_id"]} data not found; skipping')
            return [None] * 3

    if plot_type == "numeric":
        if callable(data_args["variable"]):
            base_data = data_args["variable"](full_data)
        else:
            base_data = full_data.loc[:, data_args["variable"]]

        if data_args["threshold_type"]:
            if data_args["threshold_type"] == "min":
                data_args["overlay_functions"][2] = lambda val: min(val)
            elif data_args["threshold_type"] == "max":
                data_args["overlay_functions"][2] = lambda val: max(val)
            elif data_args["threshold_type"] == "mean":
                data_args["overlay_functions"][2] = (
                    lambda val: sum(val) / len(val))
            else:
                raise ValueError(
                    "Either overlay_functions must be defined or "
                    "threshold_type must be one of {'min', 'max', 'mean'} "
                    "with plot_type 'numeric', not "
                    + str(data_args['threshold_type'])
                    )

    elif plot_type == "custom":
        base_data = full_data
    else:
        raise ValueError("Plot type must be one of {None, 'numeric'}, not "
                         + repr(plot_type))

    plot_data = tuple(safe_nan(overlay_function(data_function(base_data,
                                                              data_args)))
                      for data_function, overlay_function
                      in zip(data_args["data_functions"],
                             data_args["overlay_functions"]))
    return plot_data
