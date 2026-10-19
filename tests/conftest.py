"""Test wiring for the woow_paas_smart_home component repo.

Makes the package importable in isolation. Uses the installed
pytest-homeassistant-custom-component fixtures when available, otherwise
Home Assistant's in-tree ``tests/conftest.py`` fixtures.
"""
import sys
from pathlib import Path

# config/ holds the custom_components package; put it on sys.path so
# `import custom_components.woow_paas_smart_home` resolves.
_CONFIG_DIR = Path(__file__).resolve().parents[3]
if str(_CONFIG_DIR) not in sys.path:
    sys.path.insert(0, str(_CONFIG_DIR))

# Register HA core's conftest as a plugin so its fixtures (hass,
# enable_custom_integrations, ...) are available to tests living here.
try:
    import pytest_homeassistant_custom_component  # noqa: F401
except ImportError:
    pytest_plugins = ["tests.conftest"]
else:
    pytest_plugins = ["pytest_homeassistant_custom_component"]
