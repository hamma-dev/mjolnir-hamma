"""
Plugin to compress HAMMA trigger data files during quiet hours.

Uses the hamma.compression module for optimized compression of trigger data.
Only runs during configured quiet hours, deferring CPU and I/O to other processes.
"""

# Standard library imports
import datetime
import os
import re
import subprocess
from pathlib import Path

# Local imports
import brokkr.pipeline.base

# HAMMA compression module
from hamma.compression import compress_file

DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}$")


class CompressData(brokkr.pipeline.base.OutputStep):
    """Compress HAMMA trigger data files during quiet hours."""

    def __init__(self,
                 source_path,
                 output_subdir="compressed",
                 min_age_days=1,
                 delete_originals=True,
                 method="quantize",
                 step=8,
                 quiet_start=22,
                 quiet_end=14,
                 drive_glob=None,
                 **output_step_kwargs):
        """
        Compress HAMMA trigger data files during quiet hours.

        Parameters
        ----------
        source_path : str
            The path containing files to compress (e.g., /media/pi).
        output_subdir : str
            Subdirectory name within source_path for compressed archives.
            Default is "compressed". Archives are stored here to keep them
            on the same drive as the source data.
        min_age_days : int
            Minimum age in days for files to be compressed.
            Files newer than this are skipped to avoid compressing
            files still being written. Default is 1 day.
        delete_originals : bool
            Whether to delete original files after successful compression.
            Default is True.
        method : str
            Compression method for hamma.compression:
            - 'lossless': Lossless LZMA compression (~25% of original)
            - 'quantize': Quantization + LZMA (smaller, configurable fidelity)
            Default is 'quantize'.
        step : int
            Quantization step size (only used if method='quantize'):
            - step=1: lossless
            - step=2: ~18% of original, RMSE<1 (virtually lossless)
            - step=4: ~12% of original, RMSE~2 (high fidelity)
            - step=8: ~7% of original, RMSE~4
            Default is 8.
        quiet_start : int
            Hour (0-23 UTC) when quiet period starts. Compression only
            runs during quiet hours. Supports wraparound (e.g., 22 to 14
            means 10 PM to 2 PM UTC). Default is 22 (10 PM UTC).
        quiet_end : int
            Hour (0-23 UTC) when quiet period ends. Default is 14
            (2 PM UTC).
        drive_glob : str, optional
            Glob pattern for data drive directories under source_path
            (e.g., "DATA??"). When set, the plugin looks for date dirs
            inside each matching drive dir. When None, date dirs are
            expected directly under source_path. Default is None.
        output_step_kwargs : **kwargs, optional
            Keyword arguments to pass to the OutputStep constructor.

        """
        super().__init__(**output_step_kwargs)

        self.source_path = Path(source_path)
        self.output_subdir = output_subdir
        self.min_age_days = min_age_days
        self.delete_originals = delete_originals
        self.method = method
        self.step = step
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self.drive_glob = drive_glob
        self._priority_set = False

    def _is_quiet_time(self, current_time):
        """
        Check if current time is within the quiet period.

        Parameters
        ----------
        current_time : datetime
            The current time to check.

        Returns
        -------
        bool
            True if within quiet hours, False otherwise.
        """
        current_hour = current_time.hour

        # Handle wraparound (e.g., quiet_start=22, quiet_end=5)
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= current_hour < self.quiet_end
        else:
            return current_hour >= self.quiet_start or current_hour < self.quiet_end

    def _get_data_roots(self):
        """
        Get the root directories that contain date-named data directories.

        If drive_glob is set, globs source_path for matching drive
        directories (e.g., DATA37, DATA38) and returns those that exist
        and are directories. If no drives match, returns an empty list.

        If drive_glob is not set, returns [source_path] for backward
        compatibility (date dirs directly under source_path).

        Returns
        -------
        list of Path
            Directories to search for date-named subdirectories.
        """
        if self.drive_glob is None:
            return [self.source_path]

        drives = sorted(
            p for p in self.source_path.glob(self.drive_glob)
            if p.is_dir()
        )
        if not drives:
            self.logger.debug(
                "No drives matching '%s' found in %s",
                self.drive_glob, self.source_path)
        return drives

    def _find_resume_position(self, data_root):
        """Find the newest date directory in compressed output.

        Scans the compressed output subdirectory for the newest
        date-named directory (YYYY-MM-DDTHH format). This is where
        the previous compression pass left off.

        Parameters
        ----------
        data_root : Path
            The data root directory (e.g., /media/pi/DATA41).

        Returns
        -------
        str or None
            The name of the newest date directory, or None if no
            compressed directories exist.
        """
        output_path = data_root / self.output_subdir
        if not output_path.is_dir():
            return None

        date_dirs = sorted(
            d.name for d in output_path.iterdir()
            if d.is_dir() and DATE_DIR_RE.match(d.name)
        )
        if not date_dirs:
            return None

        return date_dirs[-1]

    def _set_low_priority(self):
        """Set this process to lowest CPU and I/O priority.

        Called once on the first execute() call, which runs in the
        worker subprocess. Uses nice 19 (lowest CPU priority) and
        ionice idle class (only gets I/O when nothing else needs it).
        """
        if self._priority_set:
            return

        try:
            os.nice(19)
            subprocess.call(
                ["ionice", "-c", "3", "-p", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.logger.info(
                "Set compression to low priority (nice=19, ionice=idle)")
        except Exception as e:
            self.logger.warning("Could not set low priority: %s", e)

        self._priority_set = True

    def execute(self, input_data=None):
        """
        Execute compression if within quiet hours.

        Parameters
        ----------
        input_data : any, optional
            Per iteration input data passed from previous PipelineSteps.
            Expected to contain 'time' key with current timestamp.

        Returns
        -------
        input_data : same as input_data
            Input data passed through for further steps to consume.
        """
        self._set_low_priority()

        try:
            current_time = input_data['time'].value

            # Only work during quiet hours
            if not self._is_quiet_time(current_time):
                self.logger.debug(
                    "Outside quiet hours (%02d:00-%02d:00), skipping",
                    self.quiet_start, self.quiet_end)
                return input_data

            # Compress eligible files during quiet hours
            compressed_count, skipped_count, error_count = self.compress_old_files()

            if compressed_count > 0 or error_count > 0:
                self.logger.info(
                    "Compression pass: %d compressed, %d already done, %d errors",
                    compressed_count, skipped_count, error_count)

        except Exception as e:
            self.logger.error(
                "%s in %s on step %s: %s",
                type(e).__name__, type(self), self.name, e)
            self.logger.info("Error details:", exc_info=True)

        return input_data

    def compress_old_files(self):
        """
        Find and compress trigger files older than min_age_days.

        Iterates over data roots (drive directories if drive_glob is set,
        otherwise source_path itself) and compresses eligible files in
        each. Each data root gets its own compressed output subdirectory.

        Returns
        -------
        tuple
            (compressed_count, skipped_count, error_count)
        """
        compressed_count = 0
        skipped_count = 0
        error_count = 0

        if not self.source_path.exists():
            self.logger.warning(
                "Source path does not exist: %s", self.source_path)
            return compressed_count, skipped_count, error_count

        data_roots = self._get_data_roots()

        cutoff_time = datetime.datetime.now() - datetime.timedelta(
            days=self.min_age_days)

        for data_root in data_roots:
            # Each data root gets its own output subdirectory
            output_path = data_root / self.output_subdir
            output_path.mkdir(parents=True, exist_ok=True)

            # Find directories that match the date pattern (YYYY-MM-DD*)
            # This handles the hourly subdirectories like 2024-01-06T12
            for data_dir in sorted(data_root.iterdir()):
                # Re-check quiet hours so we stop when the window ends
                if not self._is_quiet_time(datetime.datetime.now()):
                    self.logger.info(
                        "Quiet hours ended, stopping compression")
                    return compressed_count, skipped_count, error_count

                if not data_dir.is_dir():
                    continue

                # Skip the output subdirectory itself
                if data_dir.name == self.output_subdir:
                    continue

                try:
                    # Check if directory is old enough based on modification time
                    dir_mtime = datetime.datetime.fromtimestamp(
                        data_dir.stat().st_mtime)
                    if dir_mtime > cutoff_time:
                        self.logger.debug(
                            "Skipping directory (too recent): %s",
                            data_dir.name)
                        continue

                    # Process .bin files in this directory
                    result = self._compress_directory_files(
                        data_dir, output_path)
                    compressed_count += result[0]
                    skipped_count += result[1]
                    error_count += result[2]

                except Exception as e:
                    self.logger.error(
                        "Error processing directory %s: %s",
                        data_dir.name, e)
                    error_count += 1

        return compressed_count, skipped_count, error_count

    def _compress_directory_files(self, data_dir, output_path):
        """
        Compress all .bin files in a directory.

        Parameters
        ----------
        data_dir : Path
            The directory containing .bin files.
        output_path : Path
            The directory where compressed files will be stored.

        Returns
        -------
        tuple
            (compressed_count, skipped_count, error_count)
        """
        compressed_count = 0
        skipped_count = 0
        error_count = 0

        # Create corresponding output subdirectory to preserve structure
        out_subdir = output_path / data_dir.name
        out_subdir.mkdir(parents=True, exist_ok=True)

        # Find all .bin files in the directory
        bin_files = list(data_dir.glob("*.bin"))

        for bin_file in bin_files:
            # Re-check quiet hours so we stop when the window ends
            if not self._is_quiet_time(datetime.datetime.now()):
                break

            # Check if already compressed
            hmc_file = out_subdir / (bin_file.stem + ".hmc")
            if hmc_file.exists():
                skipped_count += 1
                continue

            # Compress the file
            if self._compress_file(bin_file, out_subdir):
                compressed_count += 1
                # Delete original if configured
                if self.delete_originals:
                    self._remove_file(bin_file)
            else:
                error_count += 1

        # If directory is now empty and delete_originals is True, remove it
        if self.delete_originals:
            remaining = list(data_dir.glob("*"))
            if not remaining:
                self._remove_directory(data_dir)

        return compressed_count, skipped_count, error_count

    def _compress_file(self, input_file, output_dir):
        """
        Compress a single trigger file using hamma.compression.

        Parameters
        ----------
        input_file : Path
            The .bin file to compress.
        output_dir : Path
            The directory where the .hmc file will be stored.

        Returns
        -------
        bool
            True if compression was successful, False otherwise.
        """
        self.logger.debug("Compressing file: %s", input_file.name)

        try:
            # Use hamma.compression.compress_file
            results = compress_file(
                str(input_file),
                output_dir=str(output_dir),
                method=self.method,
                step=self.step
            )

            if results and len(results) > 0:
                result = results[0]
                self.logger.debug(
                    "Compressed %s: %.1f%% (method=%s)",
                    input_file.name,
                    result['ratio'] * 100,
                    result['method'])
                return True
            else:
                self.logger.error(
                    "Compression returned no results for %s", input_file.name)
                return False

        except Exception as e:
            self.logger.error(
                "Error compressing %s: %s", input_file.name, e)
            return False

    def _remove_file(self, file_path):
        """
        Safely remove a file.

        Parameters
        ----------
        file_path : Path
            The file to remove.
        """
        try:
            file_path.unlink()
        except Exception as e:
            self.logger.warning(
                "Error removing file %s: %s", file_path, e)

    def _remove_directory(self, directory):
        """
        Safely remove an empty directory.

        Parameters
        ----------
        directory : Path
            The directory to remove.
        """
        try:
            directory.rmdir()
        except Exception as e:
            self.logger.warning(
                "Error removing directory %s: %s", directory, e)
