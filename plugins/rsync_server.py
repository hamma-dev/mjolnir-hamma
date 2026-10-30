"""
Plugin to run rsync to sync files to a server.
"""

# Local imports
import brokkr.pipeline.base
import brokkr.utils.output

# Standard library imports
import datetime
import fnmatch
import os
from pathlib import Path
import random
import signal
import subprocess
import time
from types import SimpleNamespace


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
                 retry_on_failure=False,
                 kill_process_group=False,
                 jitter_s=0,
                 skip_if_unchanged=False,
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
            Extra options for the ssh command rsync uses, e.g.
            "-o ConnectTimeout=30". Placed before the built-in options, because
            ssh keeps the FIRST value it sees for each option. The default is
            None (none added).
        retry_on_failure : bool, optional
            If True, a non-zero rsync exit does not reset the update clock, so
            the push is retried on the next pipeline period instead of waiting
            a full `update_time`. The default is False.
        kill_process_group : bool, optional
            If True, run rsync in its own session and SIGKILL the whole process
            group on timeout. On Python 3.7.3, `subprocess.run(timeout=...)`
            kills rsync but then waits without a timeout for rsync's ssh child,
            which still holds the output pipe -- so `timeout` alone is not a
            bound. The default is False.
        jitter_s : float, optional
            If > 0, start the update clock at a random phase within
            `update_time`, and sleep a random 0..`jitter_s` seconds before each
            push, so units restarted together do not all connect to the server
            at the same instant. The default is 0 (off).
        skip_if_unchanged : bool, optional
            If True, skip the push entirely (no ssh connection) when
            `local_path` is missing, or no file matching `include` has changed
            since the last successful push. The default is False.
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
        self.retry_on_failure = retry_on_failure
        self.kill_process_group = kill_process_group
        self.jitter_s = jitter_s
        self.skip_if_unchanged = skip_if_unchanged
        self._last_returncode = None
        self._pushed_mtime = None   # newest included mtime at the last success

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
            if self.jitter_s:
                # Random phase, so units restarted together stay apart.
                offset = random.uniform(0, self.update_time)
                self._last_save_time = SimpleNamespace(
                    value=input_data['time'].value
                    - datetime.timedelta(seconds=offset))

        try:
            # How long since we last updated?
            dt = input_data['time'].value - self._last_save_time.value

            if dt.total_seconds() > self.update_time:
                newest = None
                if self.skip_if_unchanged:
                    newest = self._newest_included_mtime()
                    if newest is None or newest == self._pushed_mtime:
                        self.logger.debug("Nothing new to rsync; skipping")
                        self._last_save_time = input_data['time']
                        return input_data

                if self.jitter_s:
                    time.sleep(random.uniform(0, self.jitter_s))

                # Now, sync the files to the server
                out = self.rsync()
                self.logger.debug("Rsync output: %s", out)

                if self.retry_on_failure and self._last_returncode != 0:
                    # Leave the clock alone: retry on the next period.
                    return input_data
                if self.skip_if_unchanged:
                    self._pushed_mtime = newest

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
            # First value wins in ssh, so configured options must come first.
            ssh = f"ssh {self.ssh_options} {ssh[len('ssh '):]}"

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

        output = self._run_command(cmd)
        self._last_returncode = output.returncode

        if output.returncode:
            self.logger.info("Rsync failure: %s", output.stdout)

        return output.stdout

    def _run_command(self, cmd):
        """Run `cmd`, bounded by `timeout`. Returns an object with
        `returncode` and `stdout`; raises subprocess.TimeoutExpired."""
        if not self.kill_process_group:
            return subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                encoding="utf-8",
                timeout=self.timeout,
            )

        # Own session, so the timeout can kill rsync AND its ssh child; the
        # child holding the pipe is what makes subprocess.run overrun on 3.7.3.
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            start_new_session=True,
        )
        try:
            stdout, _ = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        return SimpleNamespace(returncode=process.returncode, stdout=stdout)

    def _newest_included_mtime(self):
        """Newest mtime of the top-level files rsync would send, or None."""
        local = Path(self.local_path)
        if not local.is_dir():
            return None
        newest = None
        for entry in os.scandir(local):
            if not entry.is_file():
                continue
            if self.include and not fnmatch.fnmatch(entry.name, self.include):
                continue
            mtime = entry.stat().st_mtime
            if newest is None or mtime > newest:
                newest = mtime
        return newest
