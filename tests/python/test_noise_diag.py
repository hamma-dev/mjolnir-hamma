import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
PLUGIN_PATH = REPO_ROOT / "plugins" / "noise_diag.py"


class MockOutputStep:
    def __init__(self, **kwargs):
        self.logger = MagicMock()
        self.name = kwargs.get("name", "test_step")


def load_module(diag_return=(0.1, 4.7, -4.7, 0.035), volt_fast=object()):
    mock_base = MagicMock()
    mock_base.OutputStep = MockOutputStep
    mock_pipeline = MagicMock(); mock_pipeline.base = mock_base
    mock_brokkr = MagicMock(); mock_brokkr.pipeline = mock_pipeline
    mock_brokkr.pipeline.base = mock_base

    mock_hamma = MagicMock()
    data = MagicMock(); data.voltFast = volt_fast
    header = MagicMock(); header.read_stream.return_value = data
    header.data.threshold.iloc.__getitem__.return_value = 0.083
    mock_hamma.Header.return_value = header
    mock_core = MagicMock(); mock_core._diagnostic_data.return_value = diag_return

    with patch.dict("sys.modules", {
        "brokkr": mock_brokkr, "brokkr.pipeline": mock_pipeline,
        "brokkr.pipeline.base": mock_base,
        "brokkr.utils": MagicMock(), "brokkr.utils.output": MagicMock(),
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
    module = load_module(diag_return=(0.1, 4.7, -4.7, 0.035))
    step = module.NoiseDiag.__new__(module.NoiseDiag)
    step.medsize = 200000
    step.logger = MagicMock()
    m = step._compute(make_input())
    assert m["fast_offset"] == pytest.approx(0.1)
    assert m["fast_noise"] == pytest.approx(0.035)
    assert m["fast_vpp"] == pytest.approx(9.4)
    assert m["fast_snr"] == pytest.approx(9.4 / 0.035)
    assert m["threshold"] == pytest.approx(0.083)
    assert m["noise_thresh_ratio"] == pytest.approx(0.035 / 0.083)


def test_compute_returns_none_without_fast_channel():
    module = load_module(volt_fast=None)
    step = module.NoiseDiag.__new__(module.NoiseDiag)
    step.medsize = 200000
    step.logger = MagicMock()
    assert step._compute(make_input()) is None
