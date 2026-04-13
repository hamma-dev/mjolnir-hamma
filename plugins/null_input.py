"""Input step that produces NA values for all defined data types."""
import brokkr.pipeline.baseinput


class NullInput(brokkr.pipeline.baseinput.ValueInputStep):
    def read_raw_data(self, input_data=None):
        return None
