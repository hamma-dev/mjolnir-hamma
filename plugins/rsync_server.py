"""
Plugin to run rsync to sync files to a server.
"""

# Local imports
import brokkr.pipeline.base
import brokkr.utils.output

# Standard library imports
import subprocess


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
        output_step_kwargs : **kwargs, optional
            Keyword arguments to pass to the OutputStep constructor.

        """
        # Pass arguments to superclass init
        super().__init__(**output_step_kwargs)

        self._last_save_time = None

        self.server = server
        self.local_path = brokkr.utils.output.render_output_filename(
            output_path=local_path,
            filename_template="")
        self.server_path = server_path
        self.username = username
        self.update_time = update_time
        self.include = include

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
        if self._last_save_time is None:
            self._last_save_time = input_data['time']

        try:
            # How long since we last updated?
            dt = input_data['time'].value - self._last_save_time.value

            if dt.total_seconds() > self.update_time:
                # Now, sync the files to the server
                out = self.rsync()
                self.logger.info(out)

                # Update the last run time before leaving
                self._last_save_time = input_data['time']

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
            f"{self.local_path.as_posix()}",
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
