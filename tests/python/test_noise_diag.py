import importlib.util
import math
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import numpy as np

REPO_ROOT = Path(__file__).parent.parent.parent
PLUGIN_PATH = REPO_ROOT / "plugins" / "noise_diag.py"


class MockOutputStep:
    def __init__(self, **kwargs):
        self.logger = MagicMock()
        self.name = kwargs.get("name", "test_step")


def load_module(diag_return=(0.1, 4.7, -4.7, 0.035), volt_fast=object(), threshold=0.083,
                times_fast=None, trig_pos=1, pretrigger_size=953250):
    mock_base = MagicMock()
    mock_base.OutputStep = MockOutputStep
    mock_pipeline = MagicMock(); mock_pipeline.base = mock_base
    mock_brokkr = MagicMock(); mock_brokkr.pipeline = mock_pipeline
    mock_brokkr.pipeline.base = mock_base

    mock_hamma = MagicMock()
    if times_fast is None:
        times_fast = np.array(["2026-06-23T21:36:57.000", "2026-06-23T21:36:58.857",
                               "2026-06-23T21:36:59.000"], dtype="datetime64[ms]")
    data = MagicMock(); data.voltFast = volt_fast; data.timesFast = times_fast
    header = MagicMock(); header.read_stream.return_value = data
    header.data.threshold.iloc.__getitem__.return_value = threshold

    def _col(name):
        # h.data['triggerPos'].iloc[0] / h.data['preTriggerSize'].iloc[0]
        col = MagicMock()
        col.iloc.__getitem__.return_value = {
            "triggerPos": trig_pos,
            "preTriggerSize": pretrigger_size,
        }.get(name, 0)
        return col
    header.data.__getitem__.side_effect = _col
    mock_hamma.Header.return_value = header
    mock_core = MagicMock(); mock_core._diagnostic_data.return_value = diag_return

    with patch.dict("sys.modules", {
        "brokkr": mock_brokkr, "brokkr.pipeline": mock_pipeline,
        "brokkr.pipeline.base": mock_base,
        "brokkr.utils": MagicMock(), "brokkr.utils.output": MagicMock(),
        "brokkr.config": MagicMock(),
        "brokkr.config.unit": MagicMock(),
        "brokkr.config.metadata": MagicMock(),
        "hamma": mock_hamma, "hamma.header": MagicMock(),
        "hamma.header.core": mock_core,
        "notifiers": MagicMock(),
    }):
        spec = importlib.util.spec_from_file_location("noise_diag", str(PLUGIN_PATH))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def make_input():
    sp = MagicMock(); sp.value = b"raw"
    return {"science_packet": sp}


def test_compute_derives_vpp_snr_ratio():
    module = load_module(diag_return=(0.1, 4.7, -4.7, 0.035), trig_pos=1)
    step = module.NoiseDiag.__new__(module.NoiseDiag)
    step.medsize = 200000
    step.min_pretrigger_ms = 50
    step.logger = MagicMock()
    m = step._compute(make_input())
    assert m["fast_offset"] == pytest.approx(0.1)
    assert m["fast_noise"] == pytest.approx(0.035)
    assert m["fast_vpp"] == pytest.approx(9.4)
    assert m["fast_snr"] == pytest.approx(9.4 / 0.035)
    assert m["threshold"] == pytest.approx(0.083)
    assert m["noise_thresh_ratio"] == pytest.approx(0.035 / 0.083)
    # trig_pos=1 -> times[1]
    assert m["trigger_time"] == "2026-06-23T21:36:58.857"


def test_compute_returns_none_without_fast_channel():
    module = load_module(volt_fast=None)
    step = module.NoiseDiag.__new__(module.NoiseDiag)
    step.medsize = 200000
    step.min_pretrigger_ms = 50
    step.logger = MagicMock()
    assert step._compute(make_input()) is None


def test_compute_snr_nan_when_noise_zero():
    module = load_module(diag_return=(0.1, 4.7, -4.7, 0.0))  # noise == 0
    step = module.NoiseDiag.__new__(module.NoiseDiag)
    step.medsize = 200000
    step.min_pretrigger_ms = 50
    step.logger = MagicMock()
    m = step._compute(make_input())
    assert math.isnan(m["fast_snr"])
    assert m["fast_vpp"] == pytest.approx(9.4)


def test_compute_ratio_nan_when_threshold_zero():
    module = load_module(threshold=0.0)
    step = module.NoiseDiag.__new__(module.NoiseDiag)
    step.medsize = 200000
    step.min_pretrigger_ms = 50
    step.logger = MagicMock()
    m = step._compute(make_input())
    assert math.isnan(m["noise_thresh_ratio"])


def test_compute_trigger_time_out_of_range_falls_back_to_first():
    """When triggerPos is beyond timesFast, trigger_time falls back to timesFast[0]."""
    times = np.array(["2026-06-23T21:36:57.000", "2026-06-23T21:36:58.857"],
                     dtype="datetime64[ms]")
    module = load_module(trig_pos=999, times_fast=times)  # 999 >= len=2
    step = module.NoiseDiag.__new__(module.NoiseDiag)
    step.medsize = 200000
    step.min_pretrigger_ms = 50
    step.logger = MagicMock()
    m = step._compute(make_input())
    assert m["trigger_time"] == "2026-06-23T21:36:57.000"


def test_compute_skips_short_pretrigger():
    """preTriggerSize below min_pretrigger_ms -> skip (None), no noise computed."""
    # 300000 fast samples / 10 MHz = 30 ms, below the 50 ms threshold.
    module = load_module(pretrigger_size=300000)
    step = module.NoiseDiag.__new__(module.NoiseDiag)
    step.medsize = 200000
    step.min_pretrigger_ms = 50
    step.logger = MagicMock()
    with patch.object(module, "_diagnostic_data") as mock_diag:
        result = step._compute(make_input())
    assert result is None
    mock_diag.assert_not_called()


def test_compute_keeps_long_pretrigger():
    """preTriggerSize above min_pretrigger_ms -> computed normally."""
    # 953250 fast samples / 10 MHz = 95.3 ms, above the 50 ms threshold.
    module = load_module(pretrigger_size=953250)
    step = module.NoiseDiag.__new__(module.NoiseDiag)
    step.medsize = 200000
    step.min_pretrigger_ms = 50
    step.logger = MagicMock()
    m = step._compute(make_input())
    assert m is not None
    assert m["trigger_time"] == "2026-06-23T21:36:58.857"


def test_write_csv_creates_header_then_appends(tmp_path):
    module = load_module()
    step = module.NoiseDiag.__new__(module.NoiseDiag)
    step.logger = MagicMock()
    csv_file = tmp_path / "noise_mj02_2026-06-23.csv"
    step.output_path = str(tmp_path)
    step.filename_template = "noise_mj02_2026-06-23.csv"
    with patch.object(module, "render_output_filename", return_value=csv_file):
        metrics = {"trigger_time": "2026-06-23T21:36:58.857",
                   "fast_offset": 0.1, "fast_noise": 0.035, "fast_vpp": 9.4,
                   "fast_snr": 268.5, "threshold": 0.083, "noise_thresh_ratio": 0.42}
        step._write_csv(metrics, "2026-06-23T17:00:00")
        step._write_csv(metrics, "2026-06-23T17:01:00")
    lines = csv_file.read_text().strip().splitlines()
    assert lines[0] == "time,trigger_time,fast_offset,fast_noise,fast_vpp,fast_snr,threshold,noise_thresh_ratio"
    assert len(lines) == 3  # header + 2 rows
    assert lines[1].startswith("2026-06-23T17:00:00,")
    assert lines[2].startswith("2026-06-23T17:01:00,")


def _alert_step(module):
    step = module.NoiseDiag.__new__(module.NoiseDiag)
    step.logger = MagicMock()
    step.notifier = MagicMock()
    step.alert_threshold_frac = 0.8
    step.alert_cooldown_s = 3600
    step._was_over = False
    step._last_alert_time = None
    return step


def test_alert_fires_on_rising_edge_only():
    module = load_module()
    step = _alert_step(module)
    t0 = datetime(2026, 6, 23, 17, 0, 0)
    with patch.object(module, "_sensor_prefix", return_value="mj00 (Lab): "):
        step._maybe_alert({"noise_thresh_ratio": 0.5, "fast_noise": 0.04, "threshold": 0.083}, t0)
        step._maybe_alert({"noise_thresh_ratio": 0.9, "fast_noise": 0.075, "threshold": 0.083}, t0 + timedelta(seconds=60))
        step._maybe_alert({"noise_thresh_ratio": 0.92, "fast_noise": 0.076, "threshold": 0.083}, t0 + timedelta(seconds=120))
    assert step.notifier.send.call_count == 1  # only the crossing


def test_alert_respects_cooldown_after_reset():
    module = load_module()
    step = _alert_step(module)
    t0 = datetime(2026, 6, 23, 17, 0, 0)
    with patch.object(module, "_sensor_prefix", return_value="mj00 (Lab): "):
        step._maybe_alert({"noise_thresh_ratio": 0.9, "fast_noise": 0.075, "threshold": 0.083}, t0)  # fire
        step._maybe_alert({"noise_thresh_ratio": 0.5, "fast_noise": 0.04, "threshold": 0.083}, t0 + timedelta(seconds=60))  # drop
        step._maybe_alert({"noise_thresh_ratio": 0.9, "fast_noise": 0.075, "threshold": 0.083}, t0 + timedelta(seconds=120))  # within cooldown
    assert step.notifier.send.call_count == 1


def test_execute_swallows_exceptions_and_passes_through():
    module = load_module()
    step = module.NoiseDiag.__new__(module.NoiseDiag)
    step.logger = MagicMock()
    step.name = "noise_diag"
    step._last_run_time = None
    step.min_update_time = 60
    time_dv = MagicMock(); time_dv.value = datetime(2026, 6, 23, 17, 0, 0)
    input_data = {"time": time_dv}
    with patch.object(module.NoiseDiag, "_compute", side_effect=ValueError("boom")):
        # pre-seed _last_run_time so elapsed > min_update_time and _compute is reached
        step._last_run_time = MagicMock(); step._last_run_time.value = datetime(2026, 6, 23, 16, 0, 0)
        out = step.execute(input_data)
    assert out is input_data
    step.logger.error.assert_called()
