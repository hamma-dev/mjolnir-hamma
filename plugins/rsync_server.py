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
                 username='pi',
                 timeout=10,
                 partial_dir=None,
                 io_timeout=None,
                 ssh_options=None,
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
        timeout : float
            Seconds to let rsync run before it is killed. The default is 10.
        partial_dir : str, optional
            Passed to the `--partial-dir` option of rsync, so a transfer that
            is interrupted resumes on the next sync instead of restarting.
            Relative paths are relative to `server_path`. The default is None
            (not used).
        io_timeout : int, optional
            Passed to the `--timeout` option of rsync, which exits if no data
            moves for this many seconds. Set it below `timeout`: on Python
            3.7.3, `subprocess.run` can block past `timeout` after killing
            rsync, until rsync's ssh child exits. The default is None (not used).
        ssh_options : str, optional
            Extra options appended to the ssh command rsync uses, e.g.
            "-o ConnectTimeout=30". The default is None (none added).
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
        self.timeout = timeout
        self.partial_dir = partial_dir
        self.io_timeout = io_timeout
        self.ssh_options = ssh_options

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
                self.logger.debug("Rsync output: %s", out)

                # Update the last run time before leaving
                self._last_save_time = input_data['time']

        # If expression evaluation fails, presumably due to bad data values
        except Exception as e:
            self.logger.error(
                "%s evaluating in %s on step %s: %s",
                type(e).__name__, type(self), self.name, e)
            self.logger.info("Error details:", exc_info=True)
            self.logger.info(
                "Current data: %r",
                {key: str(value) for key, value in input_data.items()})

            # todo: also a status bot error message?

        # Passthrough the input for consumption by any further steps
        return input_data

    def rsync(self):
        """ Build the command for rsync and run it."""

        ssh = "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        if self.ssh_options:
            ssh = f"{ssh} {self.ssh_options}"

        cmd = [
            "rsync",
            "-rltDvz",
            "--omit-dir-times",
            "-e", ssh,
        ]

        # Let rsync give up on a stalled transfer before we have to kill it
        if self.io_timeout:
            cmd = cmd + [f"--timeout={self.io_timeout}"]

        # Keep interrupted transfers so they resume on the next sync
        if self.partial_dir:
            cmd = cmd + [f"--partial-dir={self.partial_dir}"]

        # If we only include certain files, put that in there,
        # but be careful with quotes - bash and python syntax is not the same!
        if self.include:
            cmd = cmd + [
                f"--include={self.include}",
                "--exclude=*",
            ]

        # Finish building the command for rsync
        cmd = cmd + [
            f"{self.local_path.as_posix()}/",
            f"{self.username}@{self.server}:{self.server_path}",
        ]

        output = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            encoding="utf-8",
            timeout=self.timeout,
        )
        
        if output.returncode:
            self.logger.info("Rsync failure: %s", output.stdout)
        
        return output.stdout
