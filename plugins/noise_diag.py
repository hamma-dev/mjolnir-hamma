"""
Plugin to compute fast-channel noise diagnostics from live HAMMA triggers.
"""

from pathlib import Path

import hamma
from hamma.header.core import _diagnostic_data

import brokkr.pipeline.base


class NoiseDiag(brokkr.pipeline.base.OutputStep):
    """Sample the fast-channel noise floor and report it."""

    def __init__(self,
                 min_update_time=60,
                 medsize=200000,
                 output_path=Path(),
                 filename_template=None,
                 alert_threshold_frac=0.8,
                 alert_cooldown_s=3600,
                 method=None,
                 key_file=None,
                 channel=None,
                 **output_step_kwargs):
        super().__init__(**output_step_kwargs)
        self._last_run_time = None
        self._was_over = False
        self._last_alert_time = None
        self.min_update_time = min_update_time
        self.medsize = medsize
        self.output_path = output_path
        self.filename_template = filename_template
        self.alert_threshold_frac = alert_threshold_frac
        self.alert_cooldown_s = alert_cooldown_s
        from notifiers import Notifier
        self.notifier = Notifier(
            method=method, key_file=key_file, channel=channel, logger=self.logger)

    def _compute(self, input_data):
        """Decode the packet and derive fast-channel noise metrics."""
        h = hamma.Header()
        data = h.read_stream(input_data['science_packet'].value)
        if getattr(data, "voltFast", None) is None:
            self.logger.info("No fast channel in trigger; skipping noise diag.")
            return None
        offset, vmax, vmin, noise = _diagnostic_data(data.voltFast, self.medsize)
        vpp = float(vmax) - float(vmin)
        snr = vpp / noise if noise else float("nan")
        threshold = float(h.data.threshold.iloc[0])
        ratio = noise / threshold if threshold else float("nan")
        return {
            "fast_offset": float(offset),
            "fast_noise": float(noise),
            "fast_vpp": float(vpp),
            "fast_snr": float(snr),
            "threshold": float(threshold),
            "noise_thresh_ratio": float(ratio),
        }
