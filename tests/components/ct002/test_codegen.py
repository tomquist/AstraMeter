"""Codegen unit tests for the ct002 ESPHome external component.

These tests import the schema validators from `esphome/components/ct002/__init__.py`
directly and exercise them without spinning up the full ESPHome codegen
pipeline. Skipped if ESPHome isn't installed, since the schema imports
`esphome.codegen` etc.; the YAML compile matrix in CI is the integration-level
guard.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

esphome = pytest.importorskip(
    "esphome", reason="ESPHome not installed; skipping codegen unit tests"
)

# Make the `esphome/components/ct002/__init__.py` importable as a plain module.
REPO_ROOT = Path(__file__).parent.parent.parent.parent
COMPONENTS_PATH = REPO_ROOT / "esphome" / "components"
sys.path.insert(0, str(COMPONENTS_PATH))

import ct002 as ct002_component  # noqa: E402


@contextlib.contextmanager
def _target_platform(platform):
    """Point CORE at *platform* for the duration, then put it back as it was.

    Restoring means *deleting* a key that was absent, not writing None over it:
    the code under test asks CORE what the target is, and a leftover None is a
    third answer neither branch expects.
    """
    from esphome.core import CORE

    core_data = CORE.data.setdefault(esphome.const.KEY_CORE, {})
    missing = object()
    previous = core_data.get(esphome.const.KEY_TARGET_PLATFORM, missing)
    core_data[esphome.const.KEY_TARGET_PLATFORM] = platform
    try:
        yield CORE
    finally:
        if previous is missing:
            core_data.pop(esphome.const.KEY_TARGET_PLATFORM, None)
        else:
            core_data[esphome.const.KEY_TARGET_PLATFORM] = previous


@pytest.fixture
def esp32_core():
    """CORE pointed at an ESP32, which is where the dashboard exists."""
    with _target_platform(esphome.const.PLATFORM_ESP32) as core:
        yield core


@pytest.fixture
def host_core():
    """CORE pointed at the host platform, which has no ESPHome web server."""
    with _target_platform(esphome.const.PLATFORM_HOST) as core:
        yield core


def test_validate_ct_mac_accepts_empty():
    assert ct002_component._validate_ct_mac("") == ""


def test_validate_ct_mac_normalizes_colons_and_case():
    assert ct002_component._validate_ct_mac("02:B2:50:12:AB:CD") == "02b25012abcd"
    assert ct002_component._validate_ct_mac("02-B2-50-12-AB-CD") == "02b25012abcd"
    assert ct002_component._validate_ct_mac("02b25012abcd") == "02b25012abcd"


def test_validate_ct_mac_rejects_wrong_length():
    import esphome.config_validation as cv

    with pytest.raises(cv.Invalid):
        ct002_component._validate_ct_mac("02b250")


def test_validate_ct_mac_rejects_non_hex():
    import esphome.config_validation as cv

    with pytest.raises(cv.Invalid):
        ct002_component._validate_ct_mac("zzbb250012abcd"[:12])


def test_three_phase_validator_accepts_l1_only():
    config = {ct002_component.CONF_POWER_SENSOR_L1: "grid_l1"}
    assert ct002_component._validate_three_phase_sensors(config) is config


def test_three_phase_validator_accepts_all_three():
    config = {
        ct002_component.CONF_POWER_SENSOR_L1: "grid_l1",
        ct002_component.CONF_POWER_SENSOR_L2: "grid_l2",
        ct002_component.CONF_POWER_SENSOR_L3: "grid_l3",
    }
    assert ct002_component._validate_three_phase_sensors(config) is config


def test_three_phase_validator_rejects_only_l2():
    import esphome.config_validation as cv

    config = {
        ct002_component.CONF_POWER_SENSOR_L1: "grid_l1",
        ct002_component.CONF_POWER_SENSOR_L2: "grid_l2",
    }
    with pytest.raises(cv.Invalid):
        ct002_component._validate_three_phase_sensors(config)


def test_three_phase_validator_rejects_only_l3():
    import esphome.config_validation as cv

    config = {
        ct002_component.CONF_POWER_SENSOR_L1: "grid_l1",
        ct002_component.CONF_POWER_SENSOR_L3: "grid_l3",
    }
    with pytest.raises(cv.Invalid):
        ct002_component._validate_three_phase_sensors(config)


def test_power_unit_scale_accepts_power_units():
    assert ct002_component._power_unit_scale("W") == 1.0
    assert ct002_component._power_unit_scale("kW") == 1000.0
    assert ct002_component._power_unit_scale("MW") == 1e6
    assert ct002_component._power_unit_scale("mW") == 0.001


def test_power_unit_scale_assumes_watts_when_undeclared():
    assert ct002_component._power_unit_scale(None) == 1.0
    assert ct002_component._power_unit_scale("") == 1.0


def test_power_unit_scale_rejects_non_power_units():
    # Case matters ("mW" milli vs "MW" mega), so "KW"/"w" are not accepted;
    # non-power units (temperature, energy, ...) must map to None → rejected.
    for unit in ("°C", "%", "V", "A", "kWh", "Wh", "KW", "w"):
        assert ct002_component._power_unit_scale(unit) is None


def test_validate_power_unit_accepts_watts_and_undeclared():
    ct002_component._validate_power_unit("power_sensor_l1", "grid_l1", "W")
    ct002_component._validate_power_unit("power_sensor_l1", "grid_l1", "kW")
    ct002_component._validate_power_unit("power_sensor_l1", "grid_l1", None)


def test_validate_power_unit_rejects_celsius():
    import esphome.config_validation as cv

    with pytest.raises(cv.Invalid) as exc_info:
        ct002_component._validate_power_unit("power_sensor_l1", "temp_sensor", "°C")
    assert "not a power unit" in str(exc_info.value)


def test_validate_power_unit_rejects_energy_unit():
    import esphome.config_validation as cv

    with pytest.raises(cv.Invalid):
        ct002_component._validate_power_unit("power_sensor_l2", "meter_total", "kWh")


def test_mqtt_insights_device_id_defaults_to_python_default():
    # A blank device_id must fall back to the same default the Python add-on
    # uses, so both stacks publish HA discovery under astrameter_ct002_device-1
    # (regression: ESPHome used to derive it from the ct002: component id,
    # yielding astrameter_ct002_ct002_main).
    assert ct002_component.DEFAULT_MQTT_INSIGHTS_DEVICE_ID == "device-1"
    assert ct002_component._resolve_mqtt_insights_device_id("") == "device-1"


def test_mqtt_insights_device_id_uses_explicit_value():
    assert ct002_component._resolve_mqtt_insights_device_id("garage") == "garage"


def test_mqtt_insights_schema_device_id_blank_by_default():
    # The schema default must stay blank so _resolve_mqtt_insights_device_id
    # (not the schema) owns the fallback at to_code time. The sub-block schema
    # gates on an `mqtt:` component via cv.requires_component, so mark it loaded.
    from esphome.core import CORE

    added = "mqtt" not in CORE.loaded_integrations
    if added:
        CORE.loaded_integrations.add("mqtt")
    try:
        config = ct002_component.MQTT_INSIGHTS_SCHEMA({})
    finally:
        if added:
            CORE.loaded_integrations.discard("mqtt")
    assert config[ct002_component.CONF_DEVICE_ID] == ""
    assert (
        ct002_component._resolve_mqtt_insights_device_id(
            config[ct002_component.CONF_DEVICE_ID]
        )
        == "device-1"
    )


# ── dashboard sub-block ────────────────────────────────────────────────


def test_dashboard_shorthand_accepts_bare_key_and_true():
    # Turning the dashboard on must not require knowing an option name:
    # `dashboard:` and `dashboard: true` both mean "on, with the defaults".
    assert ct002_component._dashboard_shorthand(None) == {}
    assert ct002_component._dashboard_shorthand(True) == {}


def test_dashboard_shorthand_passes_a_block_through():
    block = {"path": "/astrameter"}
    assert ct002_component._dashboard_shorthand(block) is block


def test_dashboard_off_by_substitution_string(esp32_core):
    # `dashboard: ${enable_dashboard}` expands to a string, not a bool, so the
    # two spellings have to mean the same thing.
    for spelling in ("false", "False", "no", "off"):
        config = {"power_sensor_l1": "grid_l1", "dashboard": spelling}
        assert ct002_component._resolve_dashboard(config) == {
            "power_sensor_l1": "grid_l1"
        }, spelling


def test_dashboard_on_by_substitution_string(esp32_core):
    config = {"power_sensor_l1": "grid_l1", "dashboard": "true"}
    ct002_component._resolve_dashboard(config)
    assert ct002_component._dashboard_shorthand(config["dashboard"]) == {}


def test_dashboard_false_drops_the_key_entirely(esp32_core):
    # `dashboard: false` must leave nothing behind, so a disabled dashboard
    # pulls in no HTTP server and no page in flash.
    config = {"power_sensor_l1": "grid_l1", "dashboard": False}
    assert ct002_component._resolve_dashboard(config) == {"power_sensor_l1": "grid_l1"}


def test_dashboard_absent_means_on(esp32_core):
    # The whole point of opt-out: a configuration that never mentions the
    # dashboard still gets one.
    config = {"power_sensor_l1": "grid_l1"}
    assert ct002_component._resolve_dashboard(config) == {
        "power_sensor_l1": "grid_l1",
        "dashboard": {},
    }


def test_dashboard_absent_stays_absent_off_esp32(host_core):
    # ESPHome has no HTTP server for the other targets, so the default must
    # not conjure a block the schema would then reject.
    config = {"power_sensor_l1": "grid_l1"}
    assert ct002_component._resolve_dashboard(config) == {"power_sensor_l1": "grid_l1"}


def test_dashboard_path_normalizes_to_a_prefix():
    # The mount prefix is concatenated with "/api/status" at runtime, so it
    # must never carry a trailing slash; the root becomes the empty prefix.
    assert ct002_component._validate_dashboard_path("/") == ""
    assert ct002_component._validate_dashboard_path("/astrameter") == "/astrameter"
    assert ct002_component._validate_dashboard_path("/astrameter/") == "/astrameter"


def test_dashboard_path_requires_a_leading_slash():
    import esphome.config_validation as cv

    with pytest.raises(cv.Invalid):
        ct002_component._validate_dashboard_path("astrameter")


def test_dashboard_path_rejects_a_query_string():
    import esphome.config_validation as cv

    with pytest.raises(cv.Invalid):
        ct002_component._validate_dashboard_path("/astrameter?x=1")


def test_auto_load_pulls_the_web_server_only_for_a_resolved_dashboard():
    # A parameterized AUTO_LOAD runs *after* CONFIG_SCHEMA, so it is handed the
    # validated config — one _resolve_dashboard has already been through. There
    # the key is present exactly when a dashboard was asked for, which is the
    # only shape worth asserting: reading the raw spelling instead would treat
    # `dashboard: false` (deleted by then) as absent and load the server anyway.
    assert "web_server_base" in ct002_component.AUTO_LOAD({"dashboard": {}})
    assert "web_server_base" not in ct002_component.AUTO_LOAD({})


def test_auto_load_matches_resolve_dashboard_for_every_spelling(esp32_core):
    # Belt and braces on the above: run each spelling through the resolver the
    # way ESPHome does, and check AUTO_LOAD agrees with the outcome.
    for spelling, wants_server in (
        ({}, True),  # absent — the default
        ({"dashboard": None}, True),  # bare `dashboard:`
        ({"dashboard": True}, True),
        ({"dashboard": {"controls": True}}, True),
        ({"dashboard": False}, False),
        ({"dashboard": "false"}, False),  # a substitution expands to a string
    ):
        resolved = ct002_component._resolve_dashboard(dict(spelling))
        loaded = "web_server_base" in ct002_component.AUTO_LOAD(resolved)
        assert loaded is wants_server, f"{spelling} -> {resolved}"


def test_auto_load_leaves_the_web_server_out_off_esp32(host_core):
    assert "web_server_base" not in ct002_component.AUTO_LOAD(
        ct002_component._resolve_dashboard({})
    )


def test_auto_load_always_carries_the_sub_block_infrastructure(esp32_core):
    for component in ("socket", "json", "md5"):
        assert component in ct002_component.AUTO_LOAD({})


def test_dashboard_without_a_path_takes_the_root():
    # No `web_server:` in the configuration, so the root is the dashboard's.
    config = {ct002_component.CONF_DASHBOARD: {}}
    ct002_component._final_validate_dashboard_path(config, {})
    assert config[ct002_component.CONF_DASHBOARD][ct002_component.CONF_PATH] == ""


def test_dashboard_steps_aside_for_the_esphome_web_server():
    # A default-on dashboard must never be the reason an existing
    # `web_server:` build stops working, so it moves rather than collides.
    config = {ct002_component.CONF_DASHBOARD: {}}
    ct002_component._final_validate_dashboard_path(config, {"web_server": {}})
    assert (
        config[ct002_component.CONF_DASHBOARD][ct002_component.CONF_PATH]
        == ct002_component.DASHBOARD_ASIDE_PATH
    )


def test_dashboard_at_the_root_conflicts_with_esphome_web_server():
    # Both mount on the shared HTTP server and the first handler to claim a
    # URL wins, so "/" for two pages would resolve on codegen order alone.
    import esphome.config_validation as cv

    config = {ct002_component.CONF_DASHBOARD: {ct002_component.CONF_PATH: ""}}
    with pytest.raises(cv.Invalid) as exc_info:
        ct002_component._final_validate_dashboard_path(config, {"web_server": {}})
    assert "path: /astrameter" in str(exc_info.value)


def test_dashboard_on_its_own_path_coexists_with_the_web_server():
    config = {
        ct002_component.CONF_DASHBOARD: {ct002_component.CONF_PATH: "/astrameter"}
    }
    ct002_component._final_validate_dashboard_path(config, {"web_server": {}})


def test_dashboard_at_the_root_is_fine_without_the_web_server():
    config = {ct002_component.CONF_DASHBOARD: {ct002_component.CONF_PATH: ""}}
    ct002_component._final_validate_dashboard_path(config, {})


def test_astrameter_version_is_read_from_the_repo():
    # The page shows this on its Diagnostics tab; an unusual layout must show
    # nothing rather than something wrong.
    version = ct002_component._astrameter_version()
    assert version and version[0].isdigit()


def test_astrameter_git_commit_is_read_from_the_checkout():
    # The firmware has no CI step to bake a SHA in, so the page's commit comes
    # from the repo the sources were compiled out of.
    root = Path(ct002_component.__file__).resolve().parents[3]
    if not (root / ".git").exists():
        pytest.skip("not running from a git checkout")
    sha = ct002_component._astrameter_git_commit()
    assert ct002_component._is_sha(sha), sha


def test_astrameter_git_commit_is_empty_without_a_repo(monkeypatch, tmp_path):
    # Vendored into a config directory rather than fetched as a checkout: the
    # page must then show no commit rather than one from an unrelated repo.
    fake = tmp_path / "esphome" / "components" / "ct002" / "__init__.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("")
    monkeypatch.setattr(ct002_component, "__file__", str(fake))
    assert ct002_component._astrameter_git_commit() == ""


def test_astrameter_git_commit_reads_a_linked_worktree(monkeypatch, tmp_path):
    # `git worktree add` leaves `.git` as a pointer to a private directory that
    # holds this worktree's HEAD — but the branch HEAD names lives in the
    # shared directory, so resolving it means following `commondir` too.
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"

    def git(cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "commit", "-q", "--allow-empty", "-m", "initial")

    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "side", str(linked))
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=linked,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    fake = linked / "esphome" / "components" / "ct002" / "__init__.py"
    monkeypatch.setattr(ct002_component, "__file__", str(fake))
    assert ct002_component._astrameter_git_commit() == expected


def _dashboard_default(key: str):
    """The schema default for one dashboard option.

    Read off the schema rather than by validating it: validation resolves
    component ids and the target platform, neither of which exists outside a
    real codegen run.
    """
    for marker in ct002_component.DASHBOARD_OPTIONS_SCHEMA.schema:
        if str(marker) == key:
            return marker.default()
    raise AssertionError(f"{key} is not a dashboard option")


def test_dashboard_controls_are_off_by_default():
    # The page has no login of its own, so steering someone's batteries from
    # the LAN has to be asked for — matching DASHBOARD_ALLOW_WRITE on the
    # Python side, which also defaults to off outside the add-on.
    assert _dashboard_default(ct002_component.CONF_CONTROLS) is False


# What CORE.data_dir consults *before* the config path — either one would
# send every test in this file at one shared directory, where the
# "nothing was written" assertions would start reading each other's output.
_DATA_DIR_ENV = ("ESPHOME_DATA_DIR", "ESPHOME_IS_HA_ADDON")


@contextlib.contextmanager
def _core_paths(tmp_path, name="device"):
    """Point CORE's config directory and device name at *tmp_path*, then restore.

    The web-server link is generated during validation, so it needs somewhere
    to write and a device name to file it under; outside a real codegen run
    neither is set.
    """
    from esphome.core import CORE

    previous = (CORE.config_path, CORE.name)
    previous_env = {key: os.environ.pop(key, None) for key in _DATA_DIR_ENV}
    CORE.config_path = tmp_path / f"{name}.yaml"
    CORE.name = name
    try:
        yield CORE
    finally:
        CORE.config_path, CORE.name = previous
        for key, value in previous_env.items():
            if value is not None:
                os.environ[key] = value


def _link_config(path=None, **dashboard):
    """A validated-shaped ct002 config with the dashboard's path settled."""
    dashboard.setdefault(ct002_component.CONF_PATH, path)
    if dashboard[ct002_component.CONF_PATH] is None:
        dashboard[ct002_component.CONF_PATH] = ct002_component.DASHBOARD_ASIDE_PATH
    return {ct002_component.CONF_DASHBOARD: dashboard}


def _generated_js():
    return ct002_component._web_server_link_path().read_text(encoding="utf-8")


def test_web_server_link_is_injected_into_the_esphome_page(tmp_path):
    # ESPHome's own frontend renders a fixed set of entity domains and
    # text-binds every name and state, so no entity can be a link and the page
    # would otherwise never mention the dashboard beside it. `js_include:` is
    # the only opening, so the component fills it in on the user's behalf.
    with _core_paths(tmp_path):
        config = _link_config()
        web_server = {}
        ct002_component._final_validate_web_server_link(
            config, {ct002_component.CONF_WEB_SERVER: web_server}
        )
        generated = ct002_component._web_server_link_path()
        assert web_server[esphome.const.CONF_JS_INCLUDE] == generated
        assert '"/astrameter/"' in _generated_js()


def test_web_server_link_points_at_a_custom_path(tmp_path):
    with _core_paths(tmp_path):
        config = _link_config("/power")
        ct002_component._final_validate_web_server_link(
            config, {ct002_component.CONF_WEB_SERVER: {}}
        )
        assert '"/power/"' in _generated_js()


def test_web_server_link_keeps_the_trailing_slash():
    # The dashboard answers the bare prefix with a redirect, so a link without
    # the slash costs every visitor a round trip.
    assert ct002_component._link_href(_link_config("/astrameter")) == "/astrameter/"
    assert ct002_component._link_href(_link_config("")) == "/"


def test_web_server_link_is_absent_without_the_esphome_page(tmp_path):
    # Nothing to link *from*: the dashboard has the root to itself.
    with _core_paths(tmp_path):
        ct002_component._final_validate_web_server_link(_link_config(""), {})
        assert not ct002_component._web_server_link_path().exists()


def test_web_server_link_can_be_turned_off(tmp_path):
    with _core_paths(tmp_path):
        web_server = {}
        ct002_component._final_validate_web_server_link(
            _link_config(**{ct002_component.CONF_WEB_SERVER_LINK: False}),
            {ct002_component.CONF_WEB_SERVER: web_server},
        )
        assert esphome.const.CONF_JS_INCLUDE not in web_server
        assert not ct002_component._web_server_link_path().exists()


def test_web_server_link_is_absent_for_a_dashboardless_build(tmp_path):
    with _core_paths(tmp_path):
        web_server = {}
        ct002_component._final_validate_web_server_link(
            {}, {ct002_component.CONF_WEB_SERVER: web_server}
        )
        assert esphome.const.CONF_JS_INCLUDE not in web_server


@pytest.mark.parametrize(
    "web_server",
    [
        # Builds its page in C++ and emits only /0.css, so js_include is never
        # requested.
        {esphome.const.CONF_VERSION: 1},
        # Serves the prebuilt INDEX_GZ, which does not reference /0.js either.
        {esphome.const.CONF_LOCAL: True},
    ],
    ids=["version-1", "local"],
)
def test_web_server_link_steps_aside_where_js_include_is_ignored(tmp_path, web_server):
    # Both are deliberate user choices, and the dashboard is reachable either
    # way — so this is a no-op with a log line, never a build failure.
    with _core_paths(tmp_path):
        ct002_component._final_validate_web_server_link(
            _link_config(), {ct002_component.CONF_WEB_SERVER: dict(web_server)}
        )
        assert not ct002_component._web_server_link_path().exists()


def test_web_server_link_keeps_a_user_js_include(tmp_path):
    # We are claiming a slot the user may already be using, so theirs has to
    # survive — it runs first, then ours.
    with _core_paths(tmp_path):
        theirs = tmp_path / "my-ui.js"
        theirs.write_text('console.log("mine");\n', encoding="utf-8")
        web_server = {esphome.const.CONF_JS_INCLUDE: "my-ui.js"}
        ct002_component._final_validate_web_server_link(
            _link_config(), {ct002_component.CONF_WEB_SERVER: web_server}
        )
        generated = _generated_js()
        assert generated.index('console.log("mine");') < generated.index("/astrameter/")
        assert web_server[esphome.const.CONF_JS_INCLUDE] != str(theirs)


def test_web_server_link_does_not_stack_on_revalidation(tmp_path):
    # Guards the footgun in the line above: if the generated file were ever
    # read back as "the user's", every run would append another copy.
    with _core_paths(tmp_path):
        full = {ct002_component.CONF_WEB_SERVER: {}}
        for _ in range(3):
            ct002_component._final_validate_web_server_link(_link_config(), full)
            full = {
                ct002_component.CONF_WEB_SERVER: {
                    esphome.const.CONF_JS_INCLUDE: full[
                        ct002_component.CONF_WEB_SERVER
                    ][esphome.const.CONF_JS_INCLUDE]
                }
            }
        assert _generated_js().count("document.body.prepend") == 1


def test_web_server_link_survives_an_unwritable_data_directory(tmp_path):
    # A convenience must never be the reason a build fails.
    with _core_paths(tmp_path) as core:
        core.data_dir.parent.mkdir(parents=True, exist_ok=True)
        core.data_dir.write_text("not a directory", encoding="utf-8")
        web_server = {}
        ct002_component._final_validate_web_server_link(
            _link_config(), {ct002_component.CONF_WEB_SERVER: web_server}
        )
        assert esphome.const.CONF_JS_INCLUDE not in web_server


def test_web_server_link_is_on_by_default():
    assert _dashboard_default(ct002_component.CONF_WEB_SERVER_LINK) is True


def test_web_server_link_is_not_written_into_the_build_directory(tmp_path, monkeypatch):
    # Regression: the build directory is the obvious home for a generated
    # file and the wrong one. `write_cpp` calls `update_storage_json` first,
    # which full-wipes that directory whenever the storage sidecar changed
    # (every first build among others) — after our final-validation, before
    # web_server reads the file. A snippet written there vanishes, and only
    # on some builds.
    from esphome.core import CORE

    with _core_paths(tmp_path) as core:
        # The shape a real run produces — `esphome compile` puts it at
        # <data_dir>/build/<name>. A path invented here would let the
        # assertion pass against a directory nothing actually wipes.
        #
        # monkeypatch rather than a bare assignment: it restores for us, and
        # it raises if `build_path` ever stops being an attribute of CORE,
        # so an ESPHome that moved it fails loudly here instead of leaving
        # this test quietly asserting against something that no longer means
        # anything.
        monkeypatch.setattr(CORE, "build_path", core.data_dir / "build" / core.name)
        generated = ct002_component._web_server_link_path()
        assert not generated.is_relative_to(CORE.build_path)


def test_web_server_link_is_namespaced_per_device(tmp_path):
    # ESPHome's data directory is shared by every configuration beside it.
    with _core_paths(tmp_path, name="kitchen"):
        kitchen = ct002_component._web_server_link_path()
    with _core_paths(tmp_path, name="garage"):
        assert ct002_component._web_server_link_path() != kitchen


def _run_module(source: str, tmp_path):
    """Execute *source* as an ES module against a stub DOM, return the anchor.

    Parsing it is not enough. The failure this guards against — a user's
    `js_include:` ending on an expression, so ASI reads our IIFE as its
    argument list — produces a *syntactically valid* call expression that
    only blows up when it runs.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed; cannot execute the generated module")
    harness = tmp_path / "harness.mjs"
    harness.write_text(
        """
globalThis.window = { name: "a-string-not-a-function" };
globalThis.document = {
  createElement: () => ({ style: {}, setAttribute() {} }),
  body: { prepend: (el) => { globalThis.__anchor = el; } },
};
"""
        + source
        + """
console.log(JSON.stringify(globalThis.__anchor ?? null));
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(harness)], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_generated_module_actually_builds_the_anchor(tmp_path):
    with _core_paths(tmp_path):
        ct002_component._final_validate_web_server_link(
            _link_config(), {ct002_component.CONF_WEB_SERVER: {}}
        )
        anchor = _run_module(_generated_js(), tmp_path)
    assert anchor["href"] == "/astrameter/"
    assert "AstraMeter" in anchor["textContent"]


def test_the_generated_module_runs_after_a_user_file_without_a_semicolon(tmp_path):
    # Both end up in one module, so a user file ending on an expression would
    # otherwise take our IIFE for its argument list — `window.name\n(() => {})()`
    # parses fine and throws "window.name is not a function" at runtime, so
    # only executing it catches this.
    with _core_paths(tmp_path):
        (tmp_path / "mine.js").write_text("const label = window.name", encoding="utf-8")
        ct002_component._final_validate_web_server_link(
            _link_config(),
            {
                ct002_component.CONF_WEB_SERVER: {
                    esphome.const.CONF_JS_INCLUDE: "mine.js"
                }
            },
        )
        anchor = _run_module(_generated_js(), tmp_path)
    assert anchor["href"] == "/astrameter/"


def test_web_server_link_leaves_a_non_utf8_user_file_alone(tmp_path):
    # `cv.file_` only checks that the path exists, so a user's js_include can
    # be any bytes at all. Decoding it is the one step here that fails on a
    # file that is otherwise perfectly fine, and a convenience must not take
    # the build down with it — before this, `esphome config` on such a config
    # succeeded.
    with _core_paths(tmp_path):
        (tmp_path / "latin1.js").write_bytes(b'console.log("caf\xe9");\n')
        web_server = {esphome.const.CONF_JS_INCLUDE: "latin1.js"}
        ct002_component._final_validate_web_server_link(
            _link_config(), {ct002_component.CONF_WEB_SERVER: web_server}
        )
        # Their file stays exactly as they configured it.
        assert web_server[esphome.const.CONF_JS_INCLUDE] == "latin1.js"


def test_web_server_link_does_not_stack_through_an_aliased_path(tmp_path):
    # The self-reference check has to survive a path that reaches our output
    # by another spelling — a symlink, a `..` hop, a bind-mounted config dir.
    # Comparing the configured spelling alone would read the last build's
    # output as "the user's file" and prepend it, growing the page's links
    # and the firmware's flash on every compile.
    with _core_paths(tmp_path):
        generated = ct002_component._web_server_link_path()
        full = {ct002_component.CONF_WEB_SERVER: {}}
        ct002_component._final_validate_web_server_link(_link_config(), full)
        alias = tmp_path / "alias.js"
        alias.symlink_to(generated)
        for _ in range(3):
            ct002_component._final_validate_web_server_link(
                _link_config(),
                {
                    ct002_component.CONF_WEB_SERVER: {
                        esphome.const.CONF_JS_INCLUDE: "alias.js"
                    }
                },
            )
        assert _generated_js().count("document.body.prepend") == 1


def test_web_server_link_does_not_stack_through_a_copy(tmp_path):
    # And a *copy* of our output has neither the same path nor any business
    # being prepended, so the content is checked too.
    with _core_paths(tmp_path):
        ct002_component._final_validate_web_server_link(
            _link_config(), {ct002_component.CONF_WEB_SERVER: {}}
        )
        (tmp_path / "copy.js").write_text(_generated_js(), encoding="utf-8")
        ct002_component._final_validate_web_server_link(
            _link_config(),
            {
                ct002_component.CONF_WEB_SERVER: {
                    esphome.const.CONF_JS_INCLUDE: "copy.js"
                }
            },
        )
        assert _generated_js().count("document.body.prepend") == 1


def test_web_server_link_is_handed_back_as_a_path(tmp_path):
    # `cv.file_` produces a Path, so writing a str back would leave the
    # validated document inconsistent with web_server's own schema.
    with _core_paths(tmp_path):
        web_server = {}
        ct002_component._final_validate_web_server_link(
            _link_config(), {ct002_component.CONF_WEB_SERVER: web_server}
        )
        assert isinstance(web_server[esphome.const.CONF_JS_INCLUDE], Path)


def test_web_server_link_leaves_no_temporary_file_behind(tmp_path):
    with _core_paths(tmp_path):
        ct002_component._final_validate_web_server_link(
            _link_config(), {ct002_component.CONF_WEB_SERVER: {}}
        )
        generated = ct002_component._web_server_link_path()
        assert [p.name for p in generated.parent.iterdir()] == [generated.name]


def test_concurrent_writes_do_not_delete_each_others_temporary_file(
    tmp_path, monkeypatch
):
    # Two validations of the same device inside one process are exactly what
    # the atomic write exists for — the ESPHome dashboard validates in-process
    # as you type. A temporary name keyed only on the pid would hand both
    # writers the same path, and each one's cleanup would then be free to
    # delete the other's file: the loser's os.replace raises FileNotFoundError,
    # the caller swallows it as an OSError, and the link silently goes missing.
    #
    # The barrier is what makes that deterministic rather than a matter of
    # timing: every writer has written its temporary file and none has replaced
    # yet, so a shared temporary name is guaranteed to have exactly one winner
    # and the rest failing. Left to chance, a loaded or single-core runner can
    # serialise the threads and never reproduce it.
    threads_count = 8
    target = tmp_path / "out" / "link.js"
    content = "// snippet\n"
    errors: list[BaseException] = []
    barrier = threading.Barrier(threads_count)
    real_replace = os.replace

    def synchronized_replace(source, destination):
        # Only our own writers wait. Patching os.replace is process-wide, and
        # an unrelated caller landing on the barrier would break it for
        # everyone and fail this test for the wrong reason.
        if Path(destination) == target:
            barrier.wait(timeout=30)
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", synchronized_replace)

    def write():
        try:
            ct002_component._write_atomically(target, content)
        except BaseException as exc:  # reported to the test, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=write) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert target.read_text(encoding="utf-8") == content
    assert [p.name for p in target.parent.iterdir()] == [target.name]
