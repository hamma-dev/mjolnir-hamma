"""
Plugin to generate a plot of HAMMA data and save it locally.
"""

# Third party imports
import matplotlib  # Do this first to ensure backend
matplotlib.use('Agg')
from matplotlib.pyplot import close as mpl_close
from matplotlib.pyplot import subplots as mpl_subplots

import hamma
from hamma.plotting import plot as hamma_plot

# Local imports
import brokkr.pipeline.decode
from brokkr.config.unit import UNIT_CONFIG
from brokkr.config.metadata import METADATA

# Standard library imports
from pathlib import Path


class HammaPlot(brokkr.pipeline.base.OutputStep):
    """Generate a plot of HAMMA data."""

    def __init__(self,
                 min_update_time,
                 save_path,
                 **output_step_kwargs):
        """
        Make a plot of HAMMA data.

        This step will "inject" a new entry into the dictionary Brokkr passes
        in pipelines that tracks the last time a plot was made.

        Parameters
        ----------
        min_update_time : numeric
            The minimum time (in seconds) that should elapse before a new plot
            is saved.
        save_path : str
            The path to which the plot is saved.
        output_step_kwargs : **kwargs, optional
            Keyword arguments to pass to the OutputStep constructor.

        """

        self._previous_data = None

        self.min_update_time = min_update_time
        self.save_path = save_path

        # Pass arguments to superclass init
        super().__init__(**output_step_kwargs)

    def execute(self, input_data=None):
        """
        Execute an action upon detection an arbitrary condition in the data.

        Parameters
        ----------
        input_data : any, optional
            Per iteration input data passed to this function from previous
            PipelineSteps. Not used here but retained for compatibility with
            the generalized PipelineStep API. The default is None.

        Returns
        -------
        input_data : same as input_data
            Input data passed through, for further steps to consume. Note
            a new key will be added. See documentation for initialization
            of the class.

        """

        # Handle first iteration
        if self._previous_data is None:
            # Inject a entry to track the last time we saved a plot
            input_data['last_save_time'] = input_data['time']
            self._previous_data = input_data
        try:
            # How long since we last updated?
            dt = input_data['time'].value-self._previous_data['last_save_time'].value

            if dt.total_seconds() > self.min_update_time:

                # Build the filename to save the plot to
                sensor_name = f"{METADATA['name']}{UNIT_CONFIG['number']:02d}"
                save_file = Path(self.save_path).joinpath(f'{sensor_name}.png')

                # Read in this file and save the plot
                _, ax = mpl_subplots(figsize=(8, 4))
                h = hamma.Header()
                data = h.read_stream(input_data['science_packet'].value)

                line = hamma_plot(data.timesFast, data.voltFast, axes=ax)
                line.axes.figure.savefig(save_file, dpi=72)

                mpl_close(line.axes.figure)

                # Update the last run time before leaving
                self._previous_data['last_save_time'].value = input_data['time'].value

                self.logger.debug('Created HAMMA Plot')

        # If expression evaluation fails, presumably due to bad data values
        except Exception as e:
            self.logger.error(
                "%s evaluating in %s on step %s: %s",
                type(e).__name__, type(self), self.name, e)
            self.logger.info("Error details:", exc_info=True)
            for pretty_name, data in [("Current", input_data),
                                      ]:
                self.logger.info(
                    "%s data: %r", pretty_name,
                    {key: str(value) for key, value in data.items()})

            # todo: also a status bot error message?

        # Pass through the input for consumption by any further steps
        return input_data
