"""
Plugin to monitor Pi connectivity.

NOTE: every other plugin in this directory runs on a sensor. This one does not:
it runs in the *second* brokkr instance -- the one on the VPS, as user
`monitor`, driving the `array_status` pipeline. Both instances are named
`brokkr-hamma-default.service` and both are started with `--system hamma`, so
establish which one you are looking at before drawing conclusions. (Sensor-side
plugins are not exclusive to sensors -- `state_monitor` is deployed on the VPS
too -- but this is the only one that exists solely for it.)

It imports `script_name` (in practice `/home/monitor/mjol_array.py`, a symlink
to `server/mjol_array.py` in this repo) and calls it once per pipeline
iteration, then flattens the resulting DataFrame into one DataValue per sensor
per field. A change to `server/mjol_array.py` therefore changes what this plugin
reports -- but only after brokkr is restarted, since the module is imported at
pipeline construction, not per iteration.

Two traps worth knowing before editing:

1. `read_raw_data` tries `status_module.collect_data()` first, but that name
   exists only as a *method* of `MjolnirArray` -- there is no module-level
   function -- so the `try` branch always raises AttributeError and the
   `except` fallback is what has actually run every iteration since this was
   written. Do not read the `try` branch as the live path.
2. Values are matched to `data_types` **positionally**, not by name. The 6th
   key `collect_data()` returns is `'Num GPS'` while the configs' 6th
   `data_names` entry is `'GPS Satellites'`; this works only because both sit
   in the same slot. Reordering either side silently mislabels sensor data
   with no error. `monitor_input_steps` and `base_path` are likewise accepted
   by __init__ and then dropped -- they are never forwarded to super().

This file is the versioned source of truth but is NOT what executes: brokkr
loads the plugin from whatever directory `systempath.toml` resolves to, which
is currently a dedicated system dir outside this checkout (HAM-192). Check that
before assuming an edit here is live. Whether that stays the arrangement is
HAM-198.
"""

# Standard Library Imports
from pathlib import Path
import importlib.util  # We're going to use this to import from script
import itertools

# Third party imports

# Local imports
import brokkr.pipeline.baseinput
import brokkr.pipeline.decode
import brokkr.pipeline.datavalue


class ArrayStatus(brokkr.pipeline.baseinput.ValueInputStep):
    """Input step yielding one DataValue per sensor per field in `data_names`.

    `sensors` is the list of unit numbers for the array being polled (e.g.
    [50, 51, 52, 53, 54, 56] for PAMMA); it defaults to range(1, 9), which is
    stale -- the configs pass it explicitly.
    """

    def __init__(
        self,
        script_name,
        data_names,
        monitor_input_steps=None,
        base_path=None,
        sensors=None,
        **value_input_kwargs,
        ):

        if sensors is None:
            sensors = range(1, 9)  # TODO: This should be passed to collect_data!

        self.sensors = sensors
        sensor_names = [f"Mjolnir {_i:02} " for _i in self.sensors]
        data_types = list()
        for _s in sensor_names:
            for _name in data_names:
                data_types.append(brokkr.pipeline.datavalue.DataType(name=_s + _name))

        # Pass arguments to superclass init
        super().__init__(data_types, **value_input_kwargs)

        # Setup class initial state
        self._previous_data = None

        # "Import" the script
        spec = importlib.util.spec_from_file_location("array", Path(script_name))
        arr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(arr)

        self.status_module = arr

    def read_raw_data(self, input_data=None):
        try:  # TEMP AD HOC FIX UNTIL WE GET THE NEW MJOL_ARRAY WORKING FOR EVERYONE
            df = self.status_module.collect_data()
        except AttributeError:
            mj_array = self.status_module.MjolnirArray(sensors=self.sensors)
            df = mj_array.collect_data()

        vals = list(itertools.chain(*df.values.tolist()))

        # print(df)
#         print(input_data)
#         self.logger.info(f"********{type(vals)}")
        return vals


    # def execute(self, input_data=None):
    #     """
    #     Execute an action upon detection an arbitrary condition in the data.
    #
    #     Parameters
    #     ----------
    #     input_data : Mapping[str, DataValue], optional
    #         Per iteration input data passed to this function from previous
    #         PipelineSteps. Used to extract the data values to report.
    #         The default is None.
    #
    #     Returns
    #     -------
    #     input_data : same as `input_data`
    #         Input data passed through unchanged, for further steps to consume.
    #     """
    #
    #     # Handle first iteration
    #     if self._previous_data is None:
    #         self._previous_data = input_data
    #
    #
    #     try:
    #         x = 2
    #         # df = self.status_module.collect_data()
    #         # self.input_data['array status'] = df
    #         dt = brokkr.pipeline.datavalue.DataType('heeey')
    #         dv = brokkr.pipeline.datavalue.DataValue(x, dt)
    #         # print(input_data)
    #     # If expression evaluation fails, presumably due to bad data values
    #     except Exception as e:
    #         self.logger.error(
    #             "%s evaluating in %s on step %s: %s",
    #             type(e).__name__, type(self), self.name, e)
    #         self.logger.info("Error details:", exc_info=True)
    #         for pretty_name, data in [("Current", input_data),
    #                                   ("Previous", self._previous_data)]:
    #             self.logger.info(
    #                 "%s data: %r", pretty_name,
    #                 {key: str(value) for key, value in data.items()})
    #
    #     # Update state for next pass through the pipeline
    #     self._previous_data = input_data
    #
    #     # Pass through the input for consumption by any further steps
    #     return input_data

