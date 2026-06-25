"""Input step that produces NA values for all defined data types.

Replaces the sunsaver modbus input in nochargecontroller mode.
Accepts and discards hardware-specific kwargs (unit, serial_port, etc.)
from the inherited preset so the CSV schema is preserved with NA values.
"""
import brokkr.pipeline.baseinput


class NullInput(brokkr.pipeline.baseinput.ValueInputStep):
    def __init__(self, data_types=None, name="NullInput",
                 exit_event=None, skip_na=False, **_hw_kwargs):
        super().__init__(data_types=data_types or [], name=name,
                         exit_event=exit_event, skip_na=skip_na)

    def read_raw_data(self, input_data=None):
        return None
