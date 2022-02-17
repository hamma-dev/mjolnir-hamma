"""
Plugin to run rsync to sync files to a server.
"""

# Local imports
import brokkr.pipeline.base
import brokkr.pipeline.decode

# Standard library imports
import sys
import subprocess
import os.path


class RsyncServer(brokkr.pipeline.base.OutputStep):
    """Sync files to a server via rsync"""

    def __init__(self,
                 server,
                 local_path,
                 server_path,
                 update_time,
                 include=None,
                 time_key='rsync_time',
                 username='pi',
                 **output_step_kwargs):
        """
        Sync files to a server via rsync.

        This class will "inject" a new entry into the dictionary Brokkr passes
        in pipelines that tracks the last time the rsync was performed.

        If you want to rsync two different paths in the same pipeline, you
        would provide two different steps and unique `time_key`s for each step.

        Parameters
        ----------
        server : str
            The server to sync the files to.
        local_path : str
            The local path to the files to be synced.
        server_path : str
            The path on the server the files will be synced to
        username : str
            The user name on the server.
        update_time : int
            The number of seconds that must elapse between successful syncs.
            Sets the cadence of syncing to the server.
        include : str
            Typically, a pattern of files to be synced. Passed to the `include` option
            of rsync.
        time_key : str
            The key corresponding to the time to track the last update.
        output_step_kwargs : **kwargs, optional
            Keyword arguments to pass to the OutputStep constructor.

        """
        # Pass arguments to superclass init
        super().__init__(**output_step_kwargs)

        self._previous_data = None

        self.server = server
        self.local_path = local_path
        self.server_path = server_path
        self.username = username
        self.update_time = update_time
        self.include = include
        self.time_key = time_key

    def execute(self, input_data=None):
        """
        Executing an action upon detection an arbitrary condition in the data.

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
            input_data[self.time_key] = input_data['time']
            self._previous_data = input_data

        try:
            # How long since we last updated?
            dt = input_data['time'].value-self._previous_data[self.time_key].value

            if dt.total_seconds() > self.update_time:
                # Now, sync the files to the server
                out = self.rsync()
                self.logger.info(out)

                # Update the last run time before leaving
                self._previous_data[self.time_key].value = input_data['time'].value

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

        # Passthrough the input for consumption by any further steps
        return input_data

    def rsync(self):
        """ Build the command for rsync and run it."""

        TIMEOUT = 10  # todo: Find the best value for this and/or make it an arg

        cmd = [
            "rsync",
            "-avz",
        ]

        # If we only include certain files, put that in there,
        # but be careful with quotes - bash and python syntax is not the same!
        if self.include:
            cmd = cmd + [
                f"--include={self.include}",
                "--exclude=*",
            ]

        # Finish building the command for rsync
        cmd = cmd + [
            f"{self.local_path}",
            f"{self.username}@{self.server}:{self.server_path}",
        ]

        output = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            encoding="utf-8",
            timeout=TIMEOUT,
        )

        return output.stdout
