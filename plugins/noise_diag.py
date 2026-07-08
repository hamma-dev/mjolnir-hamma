"""
Plugin to compute fast-channel noise diagnostics from live HAMMA triggers.
"""

import csv
from pathlib import Path

import hamma
from hamma.header.core import _diagnostic_data

# HAMMA2 fast-channel sample rate. preTriggerSize is a count of fast samples;
# the header's sampleRateFast field is not populated, so use the known rate.
FAST_SAMPLE_RATE_HZ = 10_000_000

import brokkr.pipeline.base
from brokkr.utils.output import render_output_filename


def _sensor_prefix():
    """Return the '<name><NN> (<site>): ' prefix for this unit's messages."""
    from brokkr.config.unit import UNIT_CONFIG
    from brokkr.config.metadata import METADATA

    sensor_name = f"{METADATA['name']}{UNIT_CONFIG['number']:02d}"
    site = UNIT_CONFIG['site_description']
    return f"{sensor_name} ({site}): " if site else f"{sensor_name}: "


class NoiseDiag(brokkr.pipeline.base.OutputStep):
    """Sample the fast-channel noise floor and report it."""

    CSV_COLUMNS = ["time", "trigger_time", "fast_offset", "fast_noise", "fast_vpp",
                   "fast_snr", "threshold", "noise_thresh_ratio"]

    def __init__(self,
                 min_update_time=60,
                 medsize=200000,
                 min_pretrigger_ms=50,
                 output_path=None,
                 filename_template=None,
                 alert_threshold_frac=0.9,
                 alert_sustain_s=1800,
                 reset_after_under=2,
                 method=None,
                 key_file=None,
                 channel=None,
                 **output_step_kwargs):
        super().__init__(**output_step_kwargs)
        self._last_run_time = None
        self._over_since = None
        self._under_count = 0
        self.min_update_time = min_update_time
        self.medsize = medsize
        self.min_pretrigger_ms = min_pretrigger_ms
        self.output_path = output_path if output_path is not None else Path()
        self.filename_template = filename_template
        self.alert_threshold_frac = alert_threshold_frac
        self.alert_sustain_s = (alert_sustain_s
                                if (alert_sustain_s and alert_sustain_s > 0) else 1800)
        self.reset_after_under = reset_after_under if reset_after_under >= 1 else 1
        if self.reset_after_under != reset_after_under:
            self.logger.warning(
                "noise_diag: reset_after_under must be >= 1; clamped to %d.",
                self.reset_after_under)
        from notifiers import Notifier
        self.notifier = Notifier(
            method=method, key_file=key_file, channel=channel, logger=self.logger)

    def _write_csv(self, metrics, sample_time):
        """Append one metrics row, writing the header if the file is new."""
        out_file = render_output_filename(
            output_path=self.output_path,
            filename_template=self.filename_template)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        new_file = not out_file.exists()
        with open(out_file, "a", newline="") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(self.CSV_COLUMNS)
            writer.writerow([sample_time] + [metrics[c] for c in self.CSV_COLUMNS[1:]])

    def _compute(self, input_data):
        """Decode the packet and derive fast-channel noise metrics."""
        h = hamma.Header()
        data = h.read_stream(input_data['science_packet'].value)
        if getattr(data, "voltFast", None) is None:
            self.logger.info("No fast channel in trigger; skipping noise diag.")
            return None
        # The noise floor is measured on the pre-trigger baseline. If the
        # pre-trigger window is shorter than min_pretrigger_ms, that baseline
        # would be contaminated by the trigger signal, so skip without
        # computing any noise. Read straight from the header: preTriggerSize is
        # a fast-sample count, converted to ms via the fast sample rate.
        pretrigger_ms = (int(h.data['preTriggerSize'].iloc[0])
                         / FAST_SAMPLE_RATE_HZ * 1000.0)
        if pretrigger_ms < self.min_pretrigger_ms:
            self.logger.info(
                "Pre-trigger %.1f ms < %.1f ms; skipping noise diag.",
                pretrigger_ms, self.min_pretrigger_ms)
            return None

        offset, vmax, vmin, noise = _diagnostic_data(data.voltFast, self.medsize)
        vpp = float(vmax) - float(vmin)
        snr = vpp / noise if noise else float("nan")
        threshold = float(h.data.threshold.iloc[0])
        ratio = noise / threshold if threshold else float("nan")

        # Absolute trigger instant: the fast-channel timestamp at triggerPos
        # (the pre/post boundary). triggerPos indexes the FAST array.
        times_fast = getattr(data, "timesFast", None)
        trigger_time = ""
        if times_fast is not None and len(times_fast):
            trig_pos = int(h.data['triggerPos'].iloc[0])
            if 0 <= trig_pos < len(times_fast):
                trigger_time = str(times_fast[trig_pos])
            else:
                trigger_time = str(times_fast[0])

        return {
            "trigger_time": trigger_time,
            "fast_offset": float(offset),
            "fast_noise": float(noise),
            "fast_vpp": float(vpp),
            "fast_snr": float(snr),
            "threshold": float(threshold),
            "noise_thresh_ratio": float(ratio),
        }

    def _maybe_alert(self, metrics, now):
        """Alert when the noise floor has stayed at/above the threshold fraction
        continuously for alert_sustain_s (tolerating up to reset_after_under-1
        consecutive dips). Re-fires every alert_sustain_s while it stays elevated,
        so a real problem keeps nagging until fixed. reset_after_under consecutive
        under-threshold readings end the episode (recovery)."""
        over = metrics["noise_thresh_ratio"] >= self.alert_threshold_frac
        if over:
            self._under_count = 0
            if self._over_since is None:
                self._over_since = now
            elif (now - self._over_since).total_seconds() >= self.alert_sustain_s:
                pct = int(round(metrics["noise_thresh_ratio"] * 100))
                mins = int(round((now - self._over_since).total_seconds() / 60.0))
                msg = ("Noise floor stuck >= %d%% of threshold for %d+ min: "
                       "%.4f V = %d%% of threshold %.4f V"
                       % (int(round(self.alert_threshold_frac * 100)), mins,
                          metrics["fast_noise"], pct, metrics["threshold"]))
                self.logger.info(msg)
                self.notifier.send(_sensor_prefix() + msg)
                self._over_since = now   # restart clock -> nag every alert_sustain_s
        else:
            self._under_count += 1
            if self._under_count >= self.reset_after_under:
                self._over_since = None

    def execute(self, input_data=None):
        if self._last_run_time is None:
            self._last_run_time = input_data['time']
        try:
            dt = input_data['time'].value - self._last_run_time.value
            if dt.total_seconds() > self.min_update_time:
                metrics = self._compute(input_data)
                if metrics is not None:
                    self._write_csv(metrics, input_data['time'].value)
                    self._maybe_alert(metrics, input_data['time'].value)
                self._last_run_time = input_data['time']
        except Exception as e:
            self.logger.error(
                "%s evaluating in %s on step %s: %s",
                type(e).__name__, type(self), self.name, e)
            self.logger.info("Error details:", exc_info=True)
        return input_data
