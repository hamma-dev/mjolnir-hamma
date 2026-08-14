"""
Plugin to monitor Pi connectivity.
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
    """LOL"""

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

