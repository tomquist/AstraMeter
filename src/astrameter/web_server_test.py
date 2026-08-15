"""Tests for the dashboard's HTTP surface: the access gate, the routes and
the control-write validation."""

import asyncio
import json
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from astrameter.ct002 import CT002
from astrameter.status import StatusRegistry
from astrameter.status.secrets import SENTINEL
from astrameter.web_server import WebServer


def _registry(tmp_path, **kwargs) -> StatusRegistry:
    kwargs.setdefault("config_path", str(tmp_path / "config.ini"))
    kwargs.setdefault("log_level", "info")
    kwargs.setdefault("version", "9.9.9")
    kwargs.setdefault("git_commit", "")
    kwargs.setdefault("dashboard_enabled", True)
    return StatusRegistry(**kwargs)


def _device() -> CT002:
    device = CT002(device_id="ct-1")
    device._update_consumer_report(
        "02b250000001", "A", -120, "HMK-2", source_ip="10.0.0.5"
    )
    device._last_grid_values = [12.0, -30.0, 5.0]
    device._last_grid_at = 1_770_000_000.0
    device._last_smooth_target = -13.0
    return device


async def _client(registry, **kwargs) -> TestClient:
    """A test client. Requests appear to come from 127.0.0.1, i.e. NOT ingress.

    Uses the production ``build_app()`` so the routes under test are exactly
    the routes that ship.
    """
    server = WebServer(config_path=registry.config_path, status=registry, **kwargs)
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    return client


# -- the access gate --------------------------------------------------


async def test_health_is_always_reachable(tmp_path):
    """The Docker HEALTHCHECK and the add-on watchdog both probe /health, so
    it must answer even when everything else is gated off."""
    client = await _client(_registry(tmp_path, direct_access=False))
    assert (await client.get("/health")).status == 200
    await client.close()


@pytest.mark.parametrize("path", ["/", "/api/status", "/api/config"])
async def test_direct_access_is_refused_by_default_under_the_addon(tmp_path, path):
    """Under host networking the port is on the LAN with no authentication,
    so anything sensitive must fail closed."""
    client = await _client(
        _registry(tmp_path, direct_access=False, config_mode="ha_advanced")
    )
    assert (await client.get(path)).status == 403
    await client.close()


class _FakeSupervisor:
    """Stands in for SupervisorClient, answering one canned /addons/self/info."""

    def __init__(self, info):
        self._info = info
        self.written = None
        self.restarts = 0
        self.restart_error: Exception | None = None

    def available(self):
        return True

    async def get_info(self):
        return self._info

    async def set_options(self, options):
        self.written = options

    async def restart(self):
        self.restarts += 1
        if self.restart_error is not None:
            raise self.restart_error


async def _addon_options(monkeypatch, tmp_path, info, supervisor=None):
    """Drive the real route with a canned Supervisor response.

    One stand-in for every call, so a test can assert on what the route asked
    it to do.
    """
    import astrameter.addon_client as addon_client

    fake = supervisor if supervisor is not None else _FakeSupervisor(info)
    monkeypatch.setattr(addon_client, "SupervisorClient", lambda *a, **k: fake)
    registry = _registry(
        tmp_path, allow_write=True, direct_access=True, config_mode="ha_simple"
    )
    return await _client(registry)


#: What Supervisor puts on the wire: the `name: validator` mapping an add-on
#: declares is rendered into a list of field descriptors before it is served.
SUPERVISOR_SCHEMA = [
    {"name": "ct_mac", "optional": True, "type": "string"},
    {
        "name": "grid_predict_trust",
        "lengthMin": 0,
        "lengthMax": 1,
        "optional": True,
        "type": "float",
    },
]


async def test_supervisors_own_schema_shape_is_served_and_not_flagged(
    monkeypatch, tmp_path, caplog
):
    """A list of field descriptors is what a real Home Assistant sends, so it
    is the normal case — warning about it would cry wolf on every install."""
    client = await _addon_options(
        monkeypatch,
        tmp_path,
        {"options": {"ct_mac": "AABB"}, "schema": SUPERVISOR_SCHEMA},
    )
    with caplog.at_level("WARNING"):
        response = await client.get("/api/addon/options")
    assert response.status == 200
    assert (await response.json())["schema"] == SUPERVISOR_SCHEMA
    assert caplog.text == ""
    await client.close()


async def test_saving_validates_against_supervisors_schema_shape(monkeypatch, tmp_path):
    """The unknown-key check used to feed the schema straight to `set()`,
    which a list of descriptors cannot go through — so every save against a
    real Supervisor died with a 500 before it reached the write."""
    client = await _addon_options(
        monkeypatch, tmp_path, {"options": {}, "schema": SUPERVISOR_SCHEMA}
    )
    response = await client.post(
        "/api/addon/options", json={"options": {"grid_predict_trust": 0.5}}
    )
    assert response.status == 200, await response.text()
    assert (await response.json())["saved"] is True

    refused = await client.post("/api/addon/options", json={"options": {"nope": 1}})
    assert refused.status == 400
    assert "nope" in (await refused.json())["error"]
    await client.close()


async def test_the_restart_waits_for_the_answer_to_go_out(monkeypatch, tmp_path):
    """Supervisor tears down the container as part of the restart, so awaiting
    it in the handler killed us mid-response: the browser saw a 502 from the
    ingress proxy for a switch that had in fact worked, and the page showed an
    error for it."""
    import astrameter.web_server as web_server

    monkeypatch.setattr(web_server, "RESTART_GRACE_S", 0.01)
    fake = _FakeSupervisor({"options": {}, "schema": SUPERVISOR_SCHEMA})
    client = await _addon_options(monkeypatch, tmp_path, None, supervisor=fake)

    response = await client.post(
        "/api/addon/options", json={"options": {}, "restart": 1}
    )
    assert response.status == 200
    assert (await response.json())["restart"] == "supervisor"
    # The point of the fix: the answer is complete and the restart has not
    # started yet.
    assert fake.restarts == 0

    await asyncio.sleep(0.1)
    assert fake.restarts == 1
    await client.close()


async def test_a_failed_deferred_restart_is_logged_not_lost(
    monkeypatch, tmp_path, caplog
):
    """Nothing is left to answer to by then, so the log is the only place it
    can surface."""
    import astrameter.web_server as web_server

    monkeypatch.setattr(web_server, "RESTART_GRACE_S", 0.01)
    fake = _FakeSupervisor({"options": {}, "schema": SUPERVISOR_SCHEMA})
    fake.restart_error = RuntimeError("supervisor said no")
    client = await _addon_options(monkeypatch, tmp_path, None, supervisor=fake)

    with caplog.at_level("ERROR"):
        response = await client.post(
            "/api/addon/options", json={"options": {}, "restart": 1}
        )
        assert response.status == 200
        await asyncio.sleep(0.1)
    assert "supervisor said no" in caplog.text
    await client.close()


async def test_an_unrenderable_option_is_named_in_the_log(
    monkeypatch, tmp_path, caplog
):
    """The guided form shows such an option read-only, which on its own is a
    greyed-out box with no explanation anywhere an operator looks."""
    repeated = {"name": "extra_hosts", "multiple": True, "type": "string"}
    client = await _addon_options(
        monkeypatch,
        tmp_path,
        {
            "options": {"ct_mac": "AABB"},
            "schema": [SUPERVISOR_SCHEMA[0], repeated],
        },
    )
    with caplog.at_level("WARNING"):
        response = await client.get("/api/addon/options")
    assert response.status == 200
    # The schema still reaches the browser; the log is an addition, not a filter.
    assert repeated in (await response.json())["schema"]
    assert "extra_hosts (repeated string)" in caplog.text
    assert "ct_mac (" not in caplog.text, "a normal option is not reported"

    # Said once: the route is hit on every visit to the Configuration tab.
    caplog.clear()
    with caplog.at_level("WARNING"):
        await client.get("/api/addon/options")
    assert caplog.text == ""
    await client.close()


async def test_the_add_ons_own_declared_schema_is_still_understood(
    monkeypatch, tmp_path, caplog
):
    """config.yaml's mapping form, in case a Supervisor ever serves it raw."""
    client = await _addon_options(
        monkeypatch,
        tmp_path,
        {"options": {"a": 1}, "schema": {"a": "int?", "extra_hosts": ["str"]}},
    )
    with caplog.at_level("WARNING"):
        response = await client.get("/api/addon/options")
    assert (await response.json())["schema"]["extra_hosts"] == ["str"]
    assert "extra_hosts (list)" in caplog.text
    assert "a (" not in caplog.text
    await client.close()


async def test_a_schema_that_is_neither_shape_survives_to_be_diagnosed(
    monkeypatch, tmp_path, caplog
):
    """`or {}` would turn this into an empty object before anything looked at
    it — flattening precisely the malformed shape worth reporting, and hiding
    from the browser what Supervisor actually sent."""
    client = await _addon_options(
        monkeypatch, tmp_path, {"options": {}, "schema": "str"}
    )
    with caplog.at_level("WARNING"):
        response = await client.get("/api/addon/options")
    assert (await response.json())["schema"] == "str"
    assert "as str, which is neither" in caplog.text
    await client.close()


async def test_a_null_schema_is_not_reported_as_malformed(
    monkeypatch, tmp_path, caplog
):
    """Supervisor documents `schema: null` for an add-on that declares none.
    The form says so on its own; a warning would be crying wolf."""
    client = await _addon_options(
        monkeypatch, tmp_path, {"options": {}, "schema": None}
    )
    with caplog.at_level("WARNING"):
        response = await client.get("/api/addon/options")
    assert (await response.json())["schema"] == {}
    assert caplog.text == ""
    await client.close()


async def test_a_refused_page_explains_itself_in_prose(tmp_path):
    """Someone typed the address into a browser. A raw JSON error tells them
    nothing about the sidebar or the option that would let this port work."""
    client = await _client(
        _registry(tmp_path, direct_access=False, config_mode="ha_advanced")
    )
    response = await client.get("/")
    assert response.status == 403
    assert response.content_type == "text/html"
    body = await response.text()
    assert "sidebar" in body
    assert "dashboard_direct_access" in body
    await client.close()


@pytest.mark.parametrize("path", ["/", "/api/status"])
async def test_standalone_serves_without_a_second_opt_in(tmp_path, path):
    """Outside the add-on there is no ingress peer, so the plain port is the
    only way in. Gating it behind a further flag would make DASHBOARD_ENABLED
    on its own do nothing at all — which is what a standalone user sets."""
    client = await _client(
        _registry(tmp_path, direct_access=False, config_mode="standalone")
    )
    assert (await client.get(path)).status == 200
    await client.close()


async def test_forged_ingress_headers_do_not_grant_access(tmp_path):
    """The gate is the peer address precisely because a LAN client can set
    any header it likes."""
    client = await _client(
        _registry(tmp_path, direct_access=False, config_mode="ha_advanced")
    )
    response = await client.get(
        "/api/status",
        headers={
            "X-Ingress-Path": "/api/hassio_ingress/x",
            "X-Hass-Source": "core.ingress",
        },
    )
    assert response.status == 403
    await client.close()


async def test_direct_access_opt_in_allows_reads(tmp_path):
    client = await _client(_registry(tmp_path, direct_access=True))
    assert (await client.get("/api/status")).status == 200
    await client.close()


async def test_writes_need_both_trust_and_the_write_flag(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=False)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)
    response = await client.post(
        "/api/control/consumer",
        json={
            "device_id": "ct-1",
            "consumer_id": "02b250000001",
            "field": "active",
            "value": False,
        },
    )
    assert response.status == 403
    await client.close()


# -- the cross-origin guard -------------------------------------------

#: What a browser sends cross-origin *without* asking permission first. Anything
#: else triggers a preflight, which no route here answers.
#:
#: The last three carry ``application/json`` as a *parameter*. Only the essence
#: — the part before the first ``;`` — decides whether a browser preflights, so
#: these travel cross-origin freely while still containing the string a naive
#: substring check looks for. They are the regression guard for exactly that.
_SIMPLE_CONTENT_TYPES = (
    "text/plain;charset=UTF-8",
    "application/x-www-form-urlencoded",
    "multipart/form-data; boundary=x",
    "text/plain; x=application/json",
    "text/plain; application/json",
    "multipart/form-data; boundary=application/json",
)

#: Every mutating route, with a body each one would otherwise act on.
_WRITE_ROUTES: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "/api/control/consumer",
        {
            "device_id": "ct-1",
            "consumer_id": "02b250000001",
            "field": "manual_target",
            "value": 800,
        },
    ),
    ("/api/control/device", {"device_id": "ct-1", "field": "active_control"}),
    ("/api/config", {"sections": {"GENERAL": {"DEVICE_TYPE": "ct002"}}}),
    ("/api/addon/options", {"options": {}}),
    ("/api/addon/restart", {}),
    ("/api/config-mode", {"mode": "file"}),
    ("/api/restart", {}),
)


@pytest.mark.parametrize("path,body", _WRITE_ROUTES)
@pytest.mark.parametrize("content_type", _SIMPLE_CONTENT_TYPES)
async def test_writes_refuse_a_browser_simple_content_type(
    tmp_path, path, body, content_type
):
    """A page the operator happens to visit must not be able to drive this.

    The trust gate cannot help: a request that website makes through their
    browser reaches us from the very address that gate trusts, and the write
    lands whether or not the reply can be read. Refusing every content type a
    browser will send cross-origin unasked is what stops it — the rest need a
    preflight, and nothing here answers one.
    """
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)
    response = await client.post(
        path, data=json.dumps(body), headers={"Content-Type": content_type}
    )
    assert response.status == 415, path
    assert "Content-Type" in (await response.json())["error"]
    await client.close()


async def test_a_declared_json_write_still_goes_through(tmp_path):
    """The guard must refuse the browser's unasked-for encodings and nothing
    else, or it would take the dashboard's own controls down with them."""
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    device = _device()
    registry.register_device("ct-1", "ct002", device)
    client = await _client(registry)
    response = await client.post(
        "/api/control/consumer",
        data=json.dumps(
            {
                "device_id": "ct-1",
                "consumer_id": "02b250000001",
                "field": "manual_target",
                "value": 800,
            }
        ),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert response.status == 200
    assert (await response.json())["applied"] is True
    await client.close()


async def test_the_guard_covers_every_post_route(tmp_path):
    """A route added later must not be able to forget the guard.

    Driven off the shipped route table rather than the list above, so a new
    write endpoint is caught here instead of shipping unguarded — and both the
    slashed and unslashed registration of each is probed.
    """
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    registry.register_device("ct-1", "ct002", _device())
    server = WebServer(config_path=registry.config_path, status=registry)
    app = server.build_app()
    posts = sorted(
        resource.canonical
        for resource in app.router.resources()
        for route in resource
        # The catch-all 404 handler registers as "*", not POST, and changes
        # nothing — only real endpoints are in scope here.
        if route.method == "POST"
    )
    assert {path.rstrip("/") or "/" for path in posts} == {
        path for path, _ in _WRITE_ROUTES
    }

    client = TestClient(TestServer(app))
    await client.start_server()
    for path in posts:
        response = await client.post(
            path, data="{}", headers={"Content-Type": "text/plain"}
        )
        assert response.status == 415, path
    await client.close()


# -- the host guard (DNS rebinding) -----------------------------------


#: Names an outside site could point at this port. The browser resolves them
#: through the attacker's nameserver, so the second answer can be a LAN
#: address — at which point the page is *same-origin* with us and the
#: content-type guard above has nothing left to refuse.
_REBOUND_HOSTS = (
    "evil.example",
    "evil.example:52500",
    "astrameter.evil.example:52500",
    # A name that merely ends in an allowed label is still a name the
    # attacker's nameserver answers for.
    "local:52500",
    "notlocalhost",
    "localhost.evil.example",
    ".local.evil.example",
    "home.arpa.evil.example",
    # Common router-assigned suffixes that are *not* reserved: `.box` is a
    # real gTLD and `.lan` an ordinary label, so a nameserver can answer for
    # either. They are DASHBOARD_ALLOWED_HOSTS material, not built in.
    "astrameter.fritz.box",
    "nas.lan:52500",
)


@pytest.mark.parametrize("host", _REBOUND_HOSTS)
async def test_a_rebound_name_cannot_read(tmp_path, host):
    """The page has no login, so a name the attacker controls must not reach
    it — the reply to a same-origin read is theirs to keep."""
    client = await _client(_registry(tmp_path, direct_access=True))
    response = await client.get("/api/status", headers={"Host": host})
    assert response.status == 403, host
    await client.close()


@pytest.mark.parametrize("path,body", _WRITE_ROUTES)
async def test_a_rebound_name_cannot_write(tmp_path, path, body):
    """Rebinding defeats the content-type guard — the request is same-origin,
    so `application/json` needs no preflight. This is what stops the write."""
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)
    response = await client.post(
        path,
        data=json.dumps(body),
        headers={"Content-Type": "application/json", "Host": "evil.example"},
    )
    assert response.status == 403, path
    await client.close()


@pytest.mark.parametrize(
    "host",
    [
        "192.168.1.50:52500",
        "10.0.0.5",
        "[fd00::1]:52500",
        "[::1]",
        "localhost:52500",
        "astrameter.local:52500",
        # RFC 8375 reserves this one for home networks: the root will not
        # delegate it, so there is no outside nameserver to poison.
        "astrameter.home.arpa",
        "NAS.Home.Arpa:52500",
        # A resolver ignores the root label, and the header may carry it.
        "LOCALHOST.:52500",
        "Homeassistant.LOCAL",
    ],
)
async def test_an_unrebindable_address_is_served(tmp_path, host):
    """An IP literal needs no lookup, and localhost/.local/.home.arpa do not
    reach a nameserver an outsider can answer for. None can be aimed here."""
    client = await _client(_registry(tmp_path, direct_access=True))
    response = await client.get("/api/status", headers={"Host": host})
    assert response.status == 200, host
    await client.close()


async def test_a_named_host_is_served_once_configured(tmp_path):
    """A reverse proxy or a private DNS entry is a real setup; the operator
    says so and it works, without opening the port to every other name."""
    client = await _client(
        _registry(tmp_path, direct_access=True),
        allowed_hosts="astra.example.lan, PROXY.example.lan.",
    )
    for host in ("astra.example.lan", "astra.example.lan:52500", "proxy.example.lan"):
        assert (await client.get("/api/status", headers={"Host": host})).status == 200
    assert (
        await client.get("/api/status", headers={"Host": "other.example.lan"})
    ).status == 403
    await client.close()


async def test_the_health_check_answers_under_any_name(tmp_path):
    """Docker's HEALTHCHECK and the watchdog reach it under whatever name the
    operator's monitoring uses, and it exposes nothing worth rebinding for."""
    client = await _client(_registry(tmp_path, direct_access=True))
    for path in ("/health", "/api"):
        response = await client.get(path, headers={"Host": "anything.example"})
        assert response.status == 200, path
    await client.close()


async def test_ingress_is_not_subject_to_the_host_guard(tmp_path, monkeypatch):
    """Home Assistant is reached under a name we cannot know and has already
    authenticated the user; the peer address is what proves the hop."""
    import astrameter.web_server as web_server_module

    client = await _client(
        _registry(tmp_path, direct_access=False, config_mode="ha_advanced")
    )
    monkeypatch.setattr(web_server_module, "INGRESS_PEER", "127.0.0.1")
    response = await client.get(
        "/api/status", headers={"Host": "homeassistant.example.com:8123"}
    )
    assert response.status == 200
    await client.close()


async def test_a_refused_host_explains_itself_in_prose(tmp_path):
    """Most refusals are the operator's own hostname, not an attack, so the
    page has to say which option fixes it."""
    client = await _client(_registry(tmp_path, direct_access=True))
    response = await client.get("/", headers={"Host": "astra.example.lan"})
    assert response.status == 403
    assert response.content_type == "text/html"
    body = await response.text()
    assert "astra.example.lan" in body
    assert "DASHBOARD_ALLOWED_HOSTS" in body
    await client.close()


def test_is_allowed_host_reads_the_header_the_way_a_browser_writes_it():
    """Unit-level edges the route tests would need a server to reach."""
    from astrameter.web_server import is_allowed_host, parse_allowed_hosts

    # An unbracketed IPv6 literal is malformed in a Host header, but it is
    # still an address and still cannot be rebound.
    assert is_allowed_host("fd00::1")
    # No Host header at all: not something a browser sends, and not a request
    # this surface needs to answer.
    assert not is_allowed_host("")
    assert not is_allowed_host("   ")
    # The port is not part of the name.
    assert is_allowed_host("192.168.1.5:52500")
    assert not is_allowed_host("evil.example:80")
    # Configured names are compared case- and root-label-insensitively.
    allowed = parse_allowed_hosts(" Astra.Example.LAN. , ,proxy.lan ")
    assert allowed == ("astra.example.lan", "proxy.lan")
    assert is_allowed_host("ASTRA.example.lan:52500", allowed)
    assert is_allowed_host("proxy.lan.", allowed)
    assert not is_allowed_host("evil.example", allowed)
    # A list may also arrive already split.
    assert parse_allowed_hosts(["a.lan", " b.lan "]) == ("a.lan", "b.lan")
    assert parse_allowed_hosts(None) == ()


#: Colon-bearing values that are NOT addresses, and the IPv6 forms that are.
#: The C++ mirror has to agree on every one of them — it hand-rolls what
#: `ipaddress` does here, so this is the list `host_controls_test.cpp` pins
#: too (`RefusesAColonBearingNameThatIsNotAnAddress` /
#: `AcceptsTheIPv6FormsPythonAccepts`).
_NOT_ADDRESSES = (
    "evil::example",
    "1:2:3:4:5:6:7:8:9",
    "1:2:3:4:5:6:7",
    "fd00::1::2",
    "fd00:::1",
    "fd00::12345",
    "fd00::zz",
    ":1",
    "192.168.1.5::1",
    # A scoped address: `ipaddress` takes it, we do not — see is_allowed_host.
    "fd00::1%eth0",
)

_ADDRESSES = (
    "1:2:3:4:5:6:7:8",
    "fd00::",
    "::",
    "::1",
    "fd00::1",
    "::ffff:192.168.1.5",
)


@pytest.mark.parametrize("host", _NOT_ADDRESSES)
def test_a_colon_does_not_make_something_an_address(host):
    from astrameter.web_server import is_allowed_host

    assert not is_allowed_host(host)


@pytest.mark.parametrize("host", _ADDRESSES)
def test_the_real_ipv6_forms_are_addresses(host):
    from astrameter.web_server import is_allowed_host

    assert is_allowed_host(host)
    assert is_allowed_host(f"[{host}]:52500")


async def test_dashboard_off_serves_no_routes(tmp_path):
    client = await _client(_registry(tmp_path, dashboard_enabled=False))
    assert (await client.get("/api/status")).status == 404
    assert (await client.get("/health")).status == 200
    await client.close()


# -- status -----------------------------------------------------------


async def test_status_returns_a_snapshot_and_revalidates(tmp_path):
    registry = _registry(tmp_path, direct_access=True)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)

    response = await client.get("/api/status")
    assert response.status == 200
    etag = response.headers["ETag"]
    body = await response.json()
    assert body["schema_version"] == 1
    assert body["devices"][0]["consumers"][0]["consumer_id"] == "02b250000001"

    again = await client.get("/api/status", headers={"If-None-Match": etag})
    assert again.status == 304
    assert await again.text() == ""
    await client.close()


async def test_no_handler_emits_a_location_header(tmp_path):
    """Both ingress hops copy Location verbatim, so a redirect would navigate
    the user straight out of the panel."""
    registry = _registry(tmp_path, direct_access=True)
    client = await _client(registry)
    for path in ("/health", "/api/status", "/api/status/", "/nope"):
        response = await client.get(path, allow_redirects=False)
        assert "Location" not in response.headers, path
        assert response.status != 301 and response.status != 302
    await client.close()


async def test_trailing_slash_variants_are_registered(tmp_path):
    client = await _client(_registry(tmp_path, direct_access=True))
    assert (await client.get("/api/status/")).status == 200
    await client.close()


# -- control writes ---------------------------------------------------


async def test_control_write_applies_through_the_validated_setter(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    device = _device()
    registry.register_device("ct-1", "ct002", device)
    client = await _client(registry)

    response = await client.post(
        "/api/control/consumer",
        json={
            "device_id": "ct-1",
            "consumer_id": "02b250000001",
            "field": "active",
            "value": False,
        },
    )
    assert response.status == 200
    assert (await response.json())["applied"] is True
    assert device.is_consumer_active("02b250000001") is False
    await client.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manual_target", 99999),
        ("manual_target", -99999),
        ("manual_target", "abc"),
        ("distribution_weight", 50),
        ("efficiency_window_weight", 101),
        ("min_dc_output", 5000),
        ("active", "yes"),
    ],
)
async def test_out_of_range_control_writes_are_rejected(tmp_path, field, value):
    """The CT002 setters do not bound their inputs — the ranges live in the
    MQTT handlers. A value MQTT would reject must be rejected here too, or the
    broker's retained state would silently revert it on the next reconnect."""
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)
    response = await client.post(
        "/api/control/consumer",
        json={
            "device_id": "ct-1",
            "consumer_id": "02b250000001",
            "field": field,
            "value": value,
        },
    )
    assert response.status == 400
    await client.close()


async def test_unknown_device_or_field_is_a_404(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)
    for body in (
        {"device_id": "nope", "consumer_id": "x", "field": "active", "value": True},
        {"device_id": "ct-1", "consumer_id": "x", "field": "bogus", "value": True},
    ):
        assert (await client.post("/api/control/consumer", json=body)).status == 404
    await client.close()


# -- config -----------------------------------------------------------


async def test_config_get_redacts_and_post_restores_secrets(tmp_path):
    config = tmp_path / "config.ini"
    config.write_text(
        "[GENERAL]\nDEVICE_TYPE = ct002\n\n[MQTT_INSIGHTS]\nBROKER = 10.0.0.2\nPASSWORD = hunter2\n"
    )
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    client = await _client(registry)

    body = await (await client.get("/api/config")).json()
    assert body["sections"]["MQTT_INSIGHTS"]["PASSWORD"] == SENTINEL
    assert "hunter2" not in json.dumps(body)

    # Echoing the sentinel back must keep the stored password, not blank it.
    body["sections"]["MQTT_INSIGHTS"]["BROKER"] = "10.0.0.9"
    saved = await client.post("/api/config", json=body)
    assert saved.status == 200
    text = config.read_text()
    assert "PASSWORD = hunter2" in text
    assert "BROKER = 10.0.0.9" in text
    await client.close()


async def test_simple_mode_refuses_config_writes(tmp_path):
    """The add-on regenerates config.ini on every start, so a write here
    would vanish at the next restart."""
    config = tmp_path / "config.ini"
    config.write_text("[GENERAL]\nDEVICE_TYPE = ct002\n")
    registry = _registry(
        tmp_path, direct_access=True, allow_write=True, config_mode="ha_simple"
    )
    client = await _client(registry)
    response = await client.post(
        "/api/config",
        json={"sections": {"GENERAL": {"DEVICE_TYPE": "ct003"}}, "order": ["GENERAL"]},
    )
    assert response.status == 403
    assert "add-on options" in (await response.json())["error"]
    await client.close()


#: What the config editor answers to a GET. These plus the two writes below
#: (POST /api/config and the restart that would run a `[SCRIPT]` section it
#: just saved) are the whole surface, and all of it goes when it is off.
_EDITOR_ROUTES = ("/config", "/api/config", "/api/key-types")


async def test_web_config_off_closes_the_editor_the_dashboard_would_open(tmp_path):
    """`WEB_CONFIG_ENABLED = False` is someone's explicit answer, given when
    the editor was the only thing on this port. The dashboard now defaults on
    and writable, so ignoring it would reopen, on upgrade, exactly what they
    went looking for the switch to close."""
    (tmp_path / "config.ini").write_text("[GENERAL]\nDEVICE_TYPE = ct002\n")
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    client = await _client(registry, enable_web_config=False)

    for path in _EDITOR_ROUTES:
        assert (await client.get(path)).status == 404, path
    assert (
        await client.post(
            "/api/config",
            json={"sections": {"GENERAL": {"DEVICE_TYPE": "ct003"}}, "order": []},
        )
    ).status == 404
    assert (await client.post("/api/restart", json={})).status == 404

    # The rest of the dashboard is untouched — this is not a way to turn the
    # page off, only its Configuration tab.
    assert (await client.get("/api/status")).status == 200
    await client.close()


async def test_no_config_surface_is_announced_when_the_editor_is_off(tmp_path):
    """The page hides its Configuration tab when the backend names no
    config_mode — the same signal an ESPHome device sends. Without it the tab
    would stay, linking to routes that are no longer there."""
    (tmp_path / "config.ini").write_text("[GENERAL]\nDEVICE_TYPE = ct002\n")
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    client = await _client(registry, enable_web_config=False)

    caps = (await (await client.get("/api/status")).json())["capabilities"]
    assert "config_mode" not in caps
    assert caps["config_writable"] is False
    assert caps["ha_options"] is False
    # Steering batteries is a separate permission and stays granted.
    assert caps["controls"] is True
    await client.close()


@pytest.mark.parametrize("path", _EDITOR_ROUTES)
async def test_the_editor_follows_the_dashboard_when_unset(tmp_path, path):
    """Unset is the default, and a new user should not have to find a flag to
    get the Configuration tab the page advertises."""
    (tmp_path / "config.ini").write_text("[GENERAL]\nDEVICE_TYPE = ct002\n")
    client = await _client(_registry(tmp_path, direct_access=True, allow_write=True))
    assert (await client.get(path)).status == 200, path
    await client.close()


async def test_web_config_on_still_serves_the_editor_without_a_dashboard(tmp_path):
    """What the flag meant before there was a dashboard, unchanged."""
    (tmp_path / "config.ini").write_text("[GENERAL]\nDEVICE_TYPE = ct002\n")
    registry = _registry(tmp_path, dashboard_enabled=False)
    client = await _client(registry, enable_web_config=True)
    assert (await client.get("/config")).status == 200
    assert (await client.get("/api/status")).status == 404
    await client.close()


# -- device-wide controls (the MQTT switch + button) ------------------


async def test_active_control_can_be_toggled(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    device = _device()
    registry.register_device("ct-1", "ct002", device)
    client = await _client(registry)
    response = await client.post(
        "/api/control/device",
        json={"device_id": "ct-1", "field": "active_control", "value": False},
    )
    assert response.status == 200
    assert device.active_control is False
    await client.close()


async def test_force_rotation_is_accepted(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)
    response = await client.post(
        "/api/control/device",
        json={"device_id": "ct-1", "field": "force_rotation", "value": True},
    )
    assert response.status == 200
    assert (await response.json())["applied"] is True
    await client.close()


async def test_unknown_device_field_is_404(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)
    assert (
        await client.post(
            "/api/control/device",
            json={"device_id": "ct-1", "field": "nope", "value": True},
        )
    ).status == 404
    await client.close()


async def test_device_control_needs_the_write_flag(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=False)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)
    assert (
        await client.post(
            "/api/control/device",
            json={"device_id": "ct-1", "field": "force_rotation", "value": True},
        )
    ).status == 403
    await client.close()


# -- units match the MQTT entity, not the setter ----------------------


async def test_efficiency_window_weight_is_a_percentage_on_the_wire(tmp_path):
    """The MQTT entity is a 0-100 percentage while the setter takes a 0-1
    fraction, and the two must not disagree: 50 over HTTP has to mean the
    same thing as 50 over MQTT."""
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    device = _device()
    registry.register_device("ct-1", "ct002", device)

    mirrored: list[tuple] = []

    class _Insights:
        async def publish_consumer_command(self, device_id, consumer_id, field, value):
            mirrored.append((device_id, consumer_id, field, value))

    registry.insights = _Insights()
    client = await _client(registry)

    response = await client.post(
        "/api/control/consumer",
        json={
            "device_id": "ct-1",
            "consumer_id": "02b250000001",
            "field": "efficiency_window_weight",
            "value": 50,
        },
    )
    assert response.status == 200
    consumer = device.snapshot_consumer("02b250000001")
    assert consumer is not None
    assert consumer.efficiency_window_weight == pytest.approx(0.5)

    # ...and it reads back in the same unit it was written in.
    body = await (await client.get("/api/status")).json()
    row = body["devices"][0]["consumers"][0]
    assert row["efficiency_window_weight_pct"] == pytest.approx(50.0)

    # The retained MQTT command is replayed into the handler that reads the
    # *entity* unit, so mirroring the scaled 0.5 would reapply it as 0.5 %
    # on the next reconnect — the exact revert this mirror exists to stop.
    assert mirrored == [("ct-1", "02b250000001", "efficiency_window_weight", 50)]
    await client.close()


async def test_full_efficiency_window_is_accepted(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    device = _device()
    registry.register_device("ct-1", "ct002", device)
    client = await _client(registry)
    assert (
        await client.post(
            "/api/control/consumer",
            json={
                "device_id": "ct-1",
                "consumer_id": "02b250000001",
                "field": "efficiency_window_weight",
                "value": 100,
            },
        )
    ).status == 200
    consumer = device.snapshot_consumer("02b250000001")
    assert consumer is not None
    assert consumer.efficiency_window_weight == pytest.approx(1.0)
    await client.close()
