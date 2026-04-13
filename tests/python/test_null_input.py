"""Unit tests for plugins/null_input.py — NullInput plugin."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
PLUGIN_PATH = REPO_ROOT / "plugins" / "null_input.py"


class MockValueInputStep:
    """Stand-in for brokkr.pipeline.baseinput.ValueInputStep."""

    def __init__(self, data_types=None, **kwargs):
        self.data_types = data_types or []
        self.logger = MagicMock()
        self.name = kwargs.get("name", "test_step")


def load_null_input_module():
    """Load the null_input plugin with mocked brokkr dependency."""
    mock_baseinput = MagicMock()
    mock_baseinput.ValueInputStep = MockValueInputStep

    mock_pipeline = MagicMock()
    mock_pipeline.baseinput = mock_baseinput

    mock_brokkr = MagicMock()
    mock_brokkr.pipeline = mock_pipeline
    mock_brokkr.pipeline.baseinput = mock_baseinput

    with patch.dict("sys.modules", {
        "brokkr": mock_brokkr,
        "brokkr.pipeline": mock_pipeline,
        "brokkr.pipeline.baseinput": mock_baseinput,
    }):
        spec = importlib.util.spec_from_file_location(
            "null_input", PLUGIN_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def null_input_module():
    return load_null_input_module()


class TestNullInput:
    def test_class_exists(self, null_input_module):
        """NullInput class should exist and be a subclass of the mock."""
        assert hasattr(null_input_module, "NullInput")

    def test_inherits_value_input_step(self, null_input_module):
        """NullInput should inherit from ValueInputStep."""
        assert issubclass(
            null_input_module.NullInput, MockValueInputStep)

    def test_read_raw_data_returns_none(self, null_input_module):
        """read_raw_data should always return None."""
        instance = null_input_module.NullInput(data_types=[])
        result = instance.read_raw_data()
        assert result is None

    def test_read_raw_data_ignores_input(self, null_input_module):
        """read_raw_data should return None regardless of input_data."""
        instance = null_input_module.NullInput(data_types=[])
        result = instance.read_raw_data(input_data={"some": "data"})
        assert result is None
