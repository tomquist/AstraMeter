"""Extended tests for version_info — covers the get_version() fallback logic."""

from unittest.mock import patch

import astrameter.version_info as version_info


def test_get_version_from_package_metadata():
    with patch("astrameter.version_info._pkg_version", return_value="1.2.3"):
        assert version_info.get_version() == "1.2.3"


def test_get_version_falls_back_to_git_sha(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    monkeypatch.setenv("GIT_COMMIT_SHA", "deadbeef")
    with patch(
        "astrameter.version_info._pkg_version",
        side_effect=PackageNotFoundError,
    ):
        assert version_info.get_version() == "deadbeef"


def test_get_version_falls_back_to_unknown(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
    with patch(
        "astrameter.version_info._pkg_version",
        side_effect=PackageNotFoundError,
    ):
        assert version_info.get_version() == "unknown"
