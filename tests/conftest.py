"""Test wiring for the woow_paas_smart_home component repo.

Makes the package importable in isolation AND pulls in Home Assistant's
native test fixtures (``hass`` etc.) from the in-tree ``tests/conftest.py``
without requiring pytest-homeassistant-custom-component.
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
pytest_plugins = ["tests.conftest"]
