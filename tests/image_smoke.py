"""Runs *inside* a built image: does everything the code uses exist there?

The images run on a hand-assembled root filesystem (see the Dockerfile), not on
a distribution, so a change can pass every unit test and still fail in the
image: an import of a standard-library module the image leaves out, a compiled
dependency linking a library it never copied, a script tool that isn't there.
``tests/test_image.py`` mounts this file into the image and runs it; it uses
the standard library and the installed app only.

Prints one line per check and exits non-zero if any fails.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import os
import pkgutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

failures: list[str] = []


def check(name: str, fn: Callable[[], object]) -> None:
    try:
        print(f"ok   {name}: {fn()}")
    except Exception as exc:  # report every failure, not just the first
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")


def every_import_in_the_app() -> str:
    """Import every module the app's source names, wherever the import sits.

    Backends and optional features import their dependencies inside
    functions, so importing the app's modules alone would miss them.
    """
    import astrameter

    root = Path(astrameter.__file__).parent
    wanted: set[str] = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                wanted.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                wanted.add(node.module)
    missing = []
    for name in sorted(wanted):
        try:
            importlib.import_module(name)
        except ImportError as exc:
            missing.append(f"{name} ({exc})")
    if missing:
        raise ImportError("; ".join(missing))
    return f"{len(wanted)} modules named by {sum(1 for _ in root.rglob('*.py'))} files"


def every_app_module() -> str:
    import astrameter
    import astrameter.powermeter as powermeter

    names = [m.name for m in pkgutil.walk_packages(astrameter.__path__, "astrameter.")]
    for name in names:
        importlib.import_module(name)
    for attr in powermeter.__all__:  # the lazily loaded backends
        getattr(powermeter, attr)
    return f"{len(names)} modules, {len(powermeter.__all__)} power sources"


def tls() -> str:
    ctx = ssl.create_default_context()
    count = len(ctx.get_ca_certs())
    assert count > 50, f"only {count} CA certificates"
    return f"{count} CA certificates, {ssl.OPENSSL_VERSION}"


def local_time() -> str:
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    zone = time.strftime("%Z", time.localtime(1_700_000_000))
    assert zone == "CET", zone
    from zoneinfo import ZoneInfo

    ZoneInfo("America/New_York")
    return zone


def name_resolution() -> object:
    return socket.getaddrinfo("localhost", 80, type=socket.SOCK_STREAM)[0][4]


def network_interfaces() -> object:
    import ifaddr  # zeroconf (ESPHome native) enumerates interfaces with it

    return [adapter.name for adapter in ifaddr.get_adapters()]


def native_libraries() -> str:
    import ctypes.util

    libc = ctypes.util.find_library("c")
    assert libc, "ctypes.util.find_library('c') found nothing"
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    key = ChaCha20Poly1305.generate_key()
    ChaCha20Poly1305(key).encrypt(b"\0" * 12, b"x", None)
    with tempfile.NamedTemporaryFile() as handle:
        handle.write(b"x")
    home = Path.home()
    assert home.is_dir(), f"home directory {home} is missing"
    return f"libc={libc}, ChaCha20Poly1305, tempfile, home {home}"


def script_tools() -> str:
    """[SCRIPT] power sources run user commands through the shell."""
    versions = []
    for argv in (["bash", "--version"], ["curl", "--version"], ["jq", "--version"]):
        out = subprocess.run(argv, capture_output=True, text=True, check=True)
        versions.append(out.stdout.splitlines()[0].split(" (")[0])
    from astrameter.powermeter.script import Script

    reading = asyncio.run(Script("bash -c 'jq -n 40+2'").get_powermeter_watts())
    assert reading == [42], reading
    return "; ".join(versions)


for name, fn in [
    ("every import in the app", every_import_in_the_app),
    ("every app module", every_app_module),
    ("tls", tls),
    ("local time", local_time),
    ("name resolution", name_resolution),
    ("network interfaces", network_interfaces),
    ("native libraries", native_libraries),
    ("script tools", script_tools),
]:
    check(name, fn)

print(f"python {sys.version.split()[0]}, uid {os.getuid()}")
sys.exit(1 if failures else 0)
