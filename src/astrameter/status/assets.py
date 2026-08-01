"""Access to the built dashboard bundle shipped inside the package.

The bundle is a single self-contained HTML file generated from
``web/ts/dashboard/`` and committed under ``src/astrameter/static/``.  It is
committed rather than built at image time because neither the Docker build
nor ``esphome compile`` has Node available; a CI job rebuilds it and fails
on any difference, so the committed copy cannot drift.
"""

from __future__ import annotations

import importlib.resources

ASSET_NAME = "static/dashboard.html"

_cache: bytes | None = None
_loaded = False


def dashboard_html() -> bytes | None:
    """The dashboard page, or ``None`` when the build did not include it."""
    global _cache, _loaded
    if not _loaded:
        _loaded = True
        try:
            _cache = (
                importlib.resources.files("astrameter")
                .joinpath(ASSET_NAME)
                .read_bytes()
            )
        except (FileNotFoundError, OSError):
            _cache = None
    return _cache
