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


def _alert_step(module, persist=3, reset_after_under=2, re_alert_interval_s=21600):
    step = module.NoiseDiag.__new__(module.NoiseDiag)
    step.logger = MagicMock()
    step.notifier = MagicMock()
    step.alert_threshold_frac = 0.8
    step.alert_persist_count = persist
    step.reset_after_under = reset_after_under
    step.re_alert_interval_s = re_alert_interval_s
    step._over_count = 0
    step._under_count = 0
    step._last_alert_time = None
    return step


def _m(ratio):
    return {"noise_thresh_ratio": ratio, "fast_noise": 0.075, "threshold": 0.083}


def _drive(step, module, ratios, start=None, dt=60):
    if start is None:
        start = datetime(2026, 6, 23, 17, 0, 0)
    with patch.object(module, "_sensor_prefix", return_value="mj00 (Lab): "):
        for i, r in enumerate(ratios):
            step._maybe_alert(_m(r), start + timedelta(seconds=i * dt))


def test_no_alert_before_persist_count():
    module = load_module()
    step = _alert_step(module)
    _drive(step, module, [0.9, 0.92])            # only 2 of 3
    assert step.notifier.send.call_count == 0


def test_alert_fires_on_nth_over_reading():
    module = load_module()
    step = _alert_step(module)
    _drive(step, module, [0.9, 0.92, 0.95])      # 3rd over -> fire
    assert step.notifier.send.call_count == 1


def test_no_repeat_alert_while_sustained():
    module = load_module()
    step = _alert_step(module)
    _drive(step, module, [0.9, 0.92, 0.95, 0.96, 0.97])
    assert step.notifier.send.call_count == 1


def test_single_dips_do_not_reset_or_refire():
    module = load_module()
    step = _alert_step(module)                    # N=3, M=2
    _drive(step, module, [0.9, 0.92, 0.95, 0.5, 0.95, 0.5, 0.95])
    assert step.notifier.send.call_count == 1     # lone dips never re-fire


def test_single_dip_during_rampup_still_fires():
    module = load_module()
    step = _alert_step(module)                    # N=3, M=2
    _drive(step, module, [0.9, 0.92, 0.5, 0.95])  # dip at idx 2 doesn't reset
    assert step.notifier.send.call_count == 1


def test_two_consecutive_dips_reset():
    module = load_module()
    step = _alert_step(module)                    # N=3, M=2
    _drive(step, module, [0.9, 0.92, 0.5, 0.5, 0.9, 0.92])
    assert step.notifier.send.call_count == 0     # reset before reaching 3


def test_new_episode_after_full_reset_fires_again():
    module = load_module()
    step = _alert_step(module)                    # N=3, M=2
    _drive(step, module, [0.9, 0.92, 0.95,        # episode 1 -> fire
                          0.4, 0.4,               # M under -> reset
                          0.9, 0.92, 0.95])       # episode 2 -> fire
    assert step.notifier.send.call_count == 2


def test_persist_count_is_configurable():
    module = load_module()
    step = _alert_step(module, persist=2)
    _drive(step, module, [0.9, 0.92])             # 2nd over with N=2 -> fire
    assert step.notifier.send.call_count == 1


def test_reset_after_under_of_one_resets_on_single_dip():
    module = load_module()
    step = _alert_step(module, reset_after_under=1)   # M=1: single dip resets
    # With M=1 the dip at idx 2 resets, so the run never reaches 3 -> no fire.
    # (Same sequence with the default M=2 WOULD fire, so this exercises M.)
    _drive(step, module, [0.9, 0.92, 0.5, 0.9])
    assert step.notifier.send.call_count == 0


def test_alert_text_includes_count_and_pct():
    module = load_module()
    step = _alert_step(module)
    _drive(step, module, [0.9, 0.92, 0.95])
    sent = step.notifier.send.call_args[0][0]
    assert "readings over threshold" in sent
    assert "95%" in sent
    assert sent.startswith("mj00 (Lab): ")


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


def test_persist_count_and_reset_clamped_to_at_least_one():
    module = load_module()
    with patch.dict("sys.modules", {"notifiers": MagicMock()}):
        step = module.NoiseDiag(alert_persist_count=0, reset_after_under=0)
    assert step.alert_persist_count == 1
    assert step.reset_after_under == 1
    step.logger.warning.assert_called()


def test_still_elevated_refires_after_interval():
    module = load_module()
    step = _alert_step(module, re_alert_interval_s=300)   # 5 min
    _drive(step, module, [0.9] * 12)                      # onset@i2(t120); still@i7(t420)
    assert step.notifier.send.call_count == 2
    msgs = [c[0][0] for c in step.notifier.send.call_args_list]
    assert "sustained high" in msgs[0]
    assert "STILL elevated" in msgs[1]


def test_still_elevated_not_before_interval():
    module = load_module()
    step = _alert_step(module, re_alert_interval_s=300)
    _drive(step, module, [0.9] * 6)                       # onset@t120; last t300 (180s<300)
    assert step.notifier.send.call_count == 1


def test_recovery_clears_realert_clock_next_is_onset():
    module = load_module()
    step = _alert_step(module, re_alert_interval_s=300)
    _drive(step, module, [0.9, 0.9, 0.9, 0.4, 0.4, 0.9, 0.9, 0.9])  # onset, recover, onset
    assert step.notifier.send.call_count == 2
    msgs = [c[0][0] for c in step.notifier.send.call_args_list]
    assert all("sustained high" in m for m in msgs)       # both onset, no STILL


def test_still_elevated_disabled_when_interval_zero():
    module = load_module()
    step = _alert_step(module, re_alert_interval_s=0)
    _drive(step, module, [0.9] * 20)
    assert step.notifier.send.call_count == 1             # onset only


def test_default_alert_threshold_frac_is_90pct():
    module = load_module()
    with patch.dict("sys.modules", {"notifiers": MagicMock()}):
        step = module.NoiseDiag()
    assert step.alert_threshold_frac == 0.9
