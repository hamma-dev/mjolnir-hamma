"""Tests for state_monitor construct_message."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# --- Module loading with mocked dependencies ---

REPO_ROOT = Path(__file__).parent.parent.parent
PLUGIN_PATH = REPO_ROOT / "plugins" / "state_monitor.py"


class MockOutputStep:
    """Stand-in for brokkr.pipeline.base.OutputStep."""

    def __init__(self, **kwargs):
        self.logger = MagicMock()
        self.name = kwargs.get("name", "test_step")


def load_state_monitor_module():
    """Load the state_monitor plugin with mocked dependencies."""
    mock_base = MagicMock()
    mock_base.OutputStep = MockOutputStep

    mock_pipeline = MagicMock()
    mock_pipeline.base = mock_base

    mock_brokkr = MagicMock()
    mock_brokkr.pipeline = mock_pipeline
    mock_brokkr.pipeline.base = mock_base

    with patch.dict("sys.modules", {
        "brokkr": mock_brokkr,
        "brokkr.pipeline": mock_pipeline,
        "brokkr.pipeline.base": mock_base,
        "brokkr.pipeline.decode": MagicMock(),
        "brokkr.utils": MagicMock(),
        "brokkr.utils.output": MagicMock(),
        "notifiers": MagicMock(),
        "notifiers.slack": MagicMock(),
        "notifiers.google_chat": MagicMock(),
    }):
        spec = importlib.util.spec_from_file_location(
            "state_monitor", str(PLUGIN_PATH))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    return module


MODULE = load_state_monitor_module()
StateMonitor = MODULE.StateMonitor


# --- Tests ---

class TestConstructMessage:
    """Test compact alert message format."""

    def test_compact_format(self):
        """With a site_description, format is 'mj05 (UAH): <alert>'."""
        alert = "Power has dropped from 25.00 to 12.00."
        mock_unit_config = {"number": 5, "site_description": "UAH"}
        mock_metadata = {"name": "mj"}

        with patch.dict("sys.modules", {
            "brokkr.config.unit": MagicMock(UNIT_CONFIG=mock_unit_config),
            "brokkr.config.metadata": MagicMock(METADATA=mock_metadata),
        }):
            result = StateMonitor.construct_message(alert)

        assert result == "mj05 (UAH): Power has dropped from 25.00 to 12.00."

    def test_compact_format_empty_site(self):
        """With empty site_description, format is 'mj05: <alert>'."""
        alert = "Power has dropped from 25.00 to 12.00."
        mock_unit_config = {"number": 5, "site_description": ""}
        mock_metadata = {"name": "mj"}

        with patch.dict("sys.modules", {
            "brokkr.config.unit": MagicMock(UNIT_CONFIG=mock_unit_config),
            "brokkr.config.metadata": MagicMock(METADATA=mock_metadata),
        }):
            result = StateMonitor.construct_message(alert)

        assert result == "mj05: Power has dropped from 25.00 to 12.00."
