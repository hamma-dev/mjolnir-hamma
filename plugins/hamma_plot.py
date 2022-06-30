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
import brokkr.utils.output

# Standard library imports
from pathlib import Path


class HammaPlot(brokkr.pipeline.base.OutputStep):
    """Generate a plot of HAMMA data."""

    def __init__(self,
                 min_update_time,
                 output_path=Path(),
                 filename_template=None,
                 **output_step_kwargs):
        """
        Make a plot of HAMMA data.

        Parameters
        ----------
        min_update_time : numeric
            The minimum time (in seconds) that should elapse before a new plot
            is saved.
        output_path : str or pathlib.Path, optional
            The path to which the plot is saved.
            By default, the system base data directory.
        filename_template : str, optional
            Template to use to generate the output file name.
            By default, the configured default filename template.
        output_step_kwargs : **kwargs, optional
            Keyword arguments to pass to the OutputStep constructor.

        """

        self._last_save_time = None

        self.min_update_time = min_update_time
        self.output_path = output_path
        self.filename_template = filename_template

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
        if self._last_save_time is None:
            # Track the last time we saved a plot
            self._last_save_time = input_data['time']
        try:
            # How long since we last updated?
            dt = input_data['time'].value - self._last_save_time.value

            if dt.total_seconds() > self.min_update_time:

                # Build the filename to save the plot to
                save_file = brokkr.utils.output.render_output_filename(
                    output_path=self.output_path,
                    filename_template=self.filename_template)
                save_file.parent.mkdir(parents=True, exist_ok=True)

                # Read in this file and save the plot
                _, ax = mpl_subplots(figsize=(8, 4))
                h = hamma.Header()
                data = h.read_stream(input_data['science_packet'].value)

                line = hamma_plot(data.timesFast, data.voltFast, axes=ax)
                line.axes.figure.savefig(save_file, dpi=72)

                mpl_close(line.axes.figure)

                # Update the last run time before leaving
                self._last_save_time = input_data['time']

                self.logger.debug('Created HAMMA Plot %r', save_file.as_posix())

        # If expression evaluation fails, presumably due to bad data values
        except Exception as e:
            self.logger.error(
                "%s evaluating in %s on step %s: %s",
                type(e).__name__, type(self), self.name, e)
            self.logger.info("Error details:", exc_info=True)

            # todo: also a status bot error message?

        # Pass through the input for consumption by any further steps
        return input_data
