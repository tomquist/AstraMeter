"""Shared fixtures for the native AstraMeter integration tests.

These require the Home Assistant test harness
(``pytest-homeassistant-custom-component``), which needs Python >= 3.13 (modern
HA) and is run from a dedicated environment rather than the core 3.11 suite. When
that harness isn't installed, the whole directory is skipped so the core suite
collects cleanly.
"""

from __future__ import annotations

try:
    import pytest_homeassistant_custom_component  # noqa: F401

    _HA_HARNESS = True
except ImportError:  # pragma: no cover - core 3.11 env
    _HA_HARNESS = False

if _HA_HARNESS:
    import pytest

    pytest_plugins = ["pytest_homeassistant_custom_component"]

    @pytest.fixture(autouse=True)
    def _auto_enable_custom_integrations(enable_custom_integrations):
        """Make HA load custom_components/astrameter during tests."""
        yield
else:  # pragma: no cover
    collect_ignore_glob = ["*"]
