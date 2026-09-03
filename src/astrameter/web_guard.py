"""
Request-authorization layer for the embedded web server.

The dashboard has no login, so what it is willing to answer at all is decided
here rather than inside the handlers: which ``Host`` a request may name (DNS
rebinding), which ``Content-Type`` a write must declare (cross-origin writes),
and the two prose pages a refusal renders.  ``web_server.py`` wires these into
the middleware and the route registration; the rules themselves live here so
there is one place to read them — and, for :func:`is_allowed_host`, one place
that has to stay in step with its C++ mirror.
"""

from __future__ import annotations

import html
import ipaddress
import json
from collections.abc import Awaitable, Callable, Collection, Iterable
from typing import Any

from aiohttp import web

from astrameter.config.logger import logger

#: An aiohttp route handler, as the wrappers below take and return one.
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

#: How many distinct refused host names to remember for log deduplication.
#: The name comes from the request, so this is a bound on what a caller can
#: make the process hold.
_REFUSED_HOST_LOG_CAP = 64


def json_response(
    payload: Any, status: int = 200, cache: str = "no-store", **headers: str
) -> web.Response:
    """JSON response, ``Cache-Control: no-store`` unless *cache* says otherwise."""
    headers.setdefault("Cache-Control", cache)
    return web.Response(
        body=json.dumps(payload).encode("utf-8"),
        status=status,
        content_type="application/json",
        headers=headers,
    )


def error_response(message: str, status: int) -> web.Response:
    """The ``{"error": ...}`` shape every failing route answers with."""
    return json_response({"error": message}, status=status)


def forbidden() -> web.Response:
    return error_response("Forbidden", status=403)


class ApiError(Exception):
    """A refusal a route states in passing, instead of unwinding to a 500.

    Raised wherever the reason is known — a malformed body, a Supervisor that
    answered with an error — and turned into the response by
    :func:`answers_api_errors`, which every API route is wrapped in. Handlers
    are then free of the ``return error(...)`` plumbing between the check and
    the work.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def answers_api_errors(handler: Handler) -> Handler:
    """Wrap *handler* so an :class:`ApiError` it raises becomes its response."""

    async def guarded(request: web.Request) -> web.StreamResponse:
        try:
            return await handler(request)
        except ApiError as exc:
            return error_response(exc.message, status=exc.status)

    return guarded


def _refusal_page(title: str, heading: str, body: str) -> str:
    """Wrap *body* in the shell both refusal pages share.

    Kept to plain inline markup with no asset of its own: the bundle is
    exactly what these responses are refusing to hand out.
    """
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AstraMeter — {title}</title>
<style>
body{{font:16px/1.6 system-ui,sans-serif;max-width:34rem;margin:12vh auto;padding:0 1.5rem}}
code{{background:#8883;padding:.1em .35em;border-radius:.25em}}
</style>
<h1>{heading}</h1>
{body}
"""


# Only reachable in the add-on, where ingress is the intended way in.
REFUSED_HTML = _refusal_page(
    "not reachable from here",
    "Not reachable from here",
    """<p>The AstraMeter dashboard opens from the <strong>Home Assistant sidebar</strong>,
which is what authenticates you. This port has no login of its own, so it is
refused by default.</p>
<p>To use this address instead, turn on <code>dashboard_direct_access</code> in the
add-on's configuration &mdash; understanding that anyone on your network can then
open it.</p>""",
)

# Shown when the Host header names something the guard does not recognise.
# Most of the time that is the operator reaching the page under a name of
# their own rather than an attack, so it says how to allow it.
_FOREIGN_HOST_BODY = """<p>This page was requested as <code>{host}</code>, which is not an address
AstraMeter answers under. The page has no login, so it only answers under
addresses that cannot be pointed here by someone else &mdash; an IP address,
or a name you have listed yourself.</p>
<p>Open it by IP address instead, or add this name to
<code>DASHBOARD_ALLOWED_HOSTS</code> (<code>dashboard_allowed_hosts</code> in the
Home Assistant add-on) if it is yours.</p>"""


#: Content type every mutating request must declare.
#:
#: This is the dashboard's only defence against a *cross-origin* write. The
#: page has no login, so the gate in :meth:`WebServer._trusted` can only ask
#: where a request came from — and a request a website makes through the
#: operator's own browser comes from exactly the right place. Nothing about
#: the reply reaching that page matters: the write has already landed.
#:
#: A browser will not send this header cross-origin without a preflight first,
#: and no route answers one, so the write never leaves the browser. The form
#: encodings it *will* send without asking (``text/plain``,
#: ``application/x-www-form-urlencoded``, ``multipart/form-data``) are exactly
#: what this refuses — note that ``aiohttp``'s ``request.json()`` parses a body
#: whatever its declared type, so the check has to be explicit.
#:
#: It must compare the parsed **media type**, never search the raw header. What
#: makes a request preflight-free is the *essence* of its content type — the
#: part before the first ``;`` — so ``text/plain; x=application/json`` is sent
#: cross-origin with no preflight while still containing this string. A
#: substring test lets that through, and the bodiless restart routes do not even
#: need the body to parse afterwards. ``request.content_type`` is already the
#: essence, lowercased, and is what the comparison below uses.
#:
#: ``esphome/components/ct002/dashboard.cpp`` enforces the same header, for the
#: same reason — the write path has parity (see the ``check-ct002-parity`` notes under
#: ``.agents/skills/``). The two must not diverge: a request one stack accepts
#: and the other refuses means the risk is real on whichever half forgot, which
#: is why that side parses the essence too rather than calling ``find()`` on the
#: header.
JSON_CONTENT_TYPE = "application/json"


def requires_json_content_type(handler: Handler) -> Handler:
    """Wrap *handler* so a request not declared as JSON is refused."""

    async def guarded(request: web.Request) -> web.StreamResponse:
        if request.content_type.casefold() != JSON_CONTENT_TYPE:
            return error_response(
                f"Content-Type must be {JSON_CONTENT_TYPE}", status=415
            )
        return await handler(request)

    return guarded


#: Host names always accepted, beyond IP literals and the operator's own list.
#:
#: ``localhost`` resolves to the loopback address and nowhere else, ``.local``
#: is mDNS (RFC 6762): a browser resolves it by multicast on the link, not
#: through the attacker's nameserver, and ``.home.arpa`` is the name reserved
#: for home networks (RFC 8375) — the DNS root will not delegate it, so no
#: outside nameserver can be asked about a name under it either. None of the
#: three can be pointed at a LAN address from outside. Everything else has to
#: be named explicitly.
#:
#: A router-assigned suffix that is *not* reserved does not belong here however
#: common it is: ``.box`` (AVM's ``fritz.box``) and ``.lan`` are ordinary
#: labels a nameserver can answer for, so they stay a
#: ``DASHBOARD_ALLOWED_HOSTS`` decision the operator makes for their own
#: network rather than one shipped for everyone's.
ALWAYS_ALLOWED_HOST_SUFFIXES = (".localhost", ".local", ".home.arpa")
ALWAYS_ALLOWED_HOSTS = ("localhost",)


def _host_name(host: str) -> str:
    """The name part of a ``Host`` header value, without its port.

    An IPv6 literal is bracketed there (RFC 3986), which is also what keeps
    its colons apart from the port separator.
    """
    host = host.strip()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host[1:]
    # One colon separates a port; several mean an unbracketed IPv6 literal,
    # which is malformed but still parses as an address below.
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host


def parse_allowed_hosts(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Normalise the configured host allowlist to compare against."""
    if not value:
        return ()
    items = value.split(",") if isinstance(value, str) else value
    return tuple(name for name in (_normalise_host(item) for item in items) if name)


def _normalise_host(name: str) -> str:
    """Casefold a host name and drop the root label a resolver ignores."""
    return name.strip().rstrip(".").casefold()


def is_allowed_host(host: str, allowed: Collection[str] = ()) -> bool:
    """True when *host* is one this server may answer under.

    This is the defence against **DNS rebinding**, which the content-type
    guard above cannot cover. There, the attacker's page is refused because a
    browser will not send ``application/json`` across origins without a
    preflight. Rebinding removes the cross-origin part entirely: the attacker
    serves a page from a name they control, answers the second lookup for that
    name with the victim's LAN address, and the browser then treats
    ``http://evil.example:52500/`` as *same-origin* with the page. Any content
    type is fair game, and — unlike a blind cross-origin write — the reply is
    readable, so ``/api/config`` hands back the configuration and
    ``/api/status`` the state of the house. The write side is worse: a
    ``[SCRIPT]`` section is a shell command the loader runs, and ``/api/restart``
    asks for exactly that.

    What the attack cannot do is control the ``Host`` header, because the
    browser fills it in from the name in the URL — and the name has to be one
    they own for their nameserver to be asked in the first place. So an
    address that is not a name at all (the IP literal a LAN user types, which
    needs no lookup and cannot be rebound), the names that resolve without a
    nameserver, and whatever the operator adds for their own setup, are the
    complete allowlist. Requests under any other name are refused.

    Mirrored by ``controls::is_allowed_host`` in
    ``esphome/components/ct002/controls.cpp`` — the firmware's dashboard has no
    login either: the write path has parity, and the bounds must match (see the
    ``check-ct002-parity`` notes in ``.agents/skills/``).
    """
    name = _normalise_host(_host_name(host))
    if not name:
        # HTTP/1.1 requires a Host header and every browser sends one, so its
        # absence is not a request this surface needs to answer.
        return False
    if name in allowed:
        return True
    # A scoped address (`fd00::1%eth0`) names a local interface. `ip_address`
    # accepts one, but a browser cannot put it in a URL's host, and the C++
    # mirror would have to grow a parser for it — so neither side takes it.
    if "%" not in name:
        try:
            ipaddress.ip_address(name)
        except ValueError:
            pass
        else:
            return True
    return name in ALWAYS_ALLOWED_HOSTS or name.endswith(ALWAYS_ALLOWED_HOST_SUFFIXES)


class RefusedHostLog:
    """Reports each refused host name once, up to a cap.

    A page reloading behind a misconfigured name would otherwise log on every
    poll — but the name is attacker-chosen, and a page sweeping unlimited
    subdomains would grow this set (and the log) without bound. Past the cap,
    say so once and go quiet: the operator's own misconfigured hostname is one
    of the first few, not the thousandth.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._cap_hit = False

    def log(self, shown: str, path: str) -> None:
        if shown in self._seen:
            return
        if len(self._seen) < _REFUSED_HOST_LOG_CAP:
            self._seen.add(shown)
            logger.warning(
                "Refused a request for %s: %s is not an address this server "
                "answers under. Reach it by IP, or add the name to "
                "DASHBOARD_ALLOWED_HOSTS (dashboard_allowed_hosts in the "
                "add-on) if it is yours.",
                path,
                shown,
            )
        elif not self._cap_hit:
            self._cap_hit = True
            logger.warning(
                "Refused requests for %d different host names; not logging "
                "further ones. A page sweeping many names is what this "
                "guard exists to stop.",
                _REFUSED_HOST_LOG_CAP,
            )


def foreign_host_response(request: web.Request, shown: str) -> web.StreamResponse:
    """The 403 for a request whose ``Host`` the guard does not recognise.

    Prose for a page a person navigated to, JSON for the API the page polls.
    """
    message = (
        f"{shown} is not an address AstraMeter answers under. Reach it by "
        "IP address, or add the name to DASHBOARD_ALLOWED_HOSTS."
    )
    if request.path.startswith("/api/"):
        return error_response(message, status=403)
    return web.Response(
        status=403,
        text=_refusal_page(
            "unrecognised address",
            "Unrecognised address",
            _FOREIGN_HOST_BODY.format(host=html.escape(shown)),
        ),
        content_type="text/html",
    )
