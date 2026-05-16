"""CLI entry point for astra-sim.

Supports running the daemon, attaching a TUI, and sending control
commands to a running daemon.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import SimulationRunner

PID_FILE = Path.home() / ".astra-sim.pid"
LOG_FILE = Path.home() / ".astra-sim.log"
DEFAULT_HTTP_PORT = 8080


# -- HTTP client helpers ---------------------------------------------------


def _api_url(port: int, path: str) -> str:
    return f"http://localhost:{port}{path}"


def _http_get(port: int, path: str) -> dict:
    import urllib.request

    url = _api_url(port, path)
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def _http_post(port: int, path: str, body: dict | None = None) -> dict:
    import urllib.request

    url = _api_url(port, path)
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


# -- subcommands -----------------------------------------------------------


def _apply_power_update_delay_override(cfg, ticks: int | None) -> None:
    """If *ticks* is set, apply the same delay to every battery."""
    if ticks is None:
        return
    cfg.power_update_delay_ticks = ticks
    for bc in cfg.batteries:
        bc.power_update_delay_ticks = ticks


def cmd_run(args: argparse.Namespace) -> None:
    from .runner import (
        SimulationRunner,
        parse_config,
        quick_config,
        validate_config,
    )

    _setup_logging(verbose=args.verbose, tui=not args.no_tui)

    if args.config:
        data = json.loads(Path(args.config).read_text())
        cfg = parse_config(data)
        if args.http_port:
            cfg.http_port = args.http_port
        if args.ct_port:
            cfg.ct_port = args.ct_port
        if args.ct_host:
            cfg.ct_host = args.ct_host
    else:
        base = [float(x) for x in args.base_load.split(",")] if args.base_load else None
        cfg = quick_config(
            num_batteries=args.batteries,
            num_phases=args.phases,
            base_load=base,
            initial_soc=args.soc,
            ct_host=args.ct_host or "127.0.0.1",
            ct_port=args.ct_port or 12345,
            http_port=args.http_port or DEFAULT_HTTP_PORT,
        )

    if args.time_scale != 1.0:
        cfg.time_scale = args.time_scale
    _apply_power_update_delay_override(cfg, args.power_update_delay)
    validate_config(cfg)
    runner = SimulationRunner(cfg)

    if args.no_tui:
        asyncio.run(runner.run_headless())
    else:
        _run_with_tui(runner)


def cmd_start(args: argparse.Namespace) -> None:
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, 0)
            print(f"Daemon already running (PID {pid})")
            sys.exit(1)
        except OSError:
            PID_FILE.unlink(missing_ok=True)

    pid = os.fork()
    if pid > 0:
        PID_FILE.write_text(str(pid))
        print(f"Daemon started (PID {pid})")
        return

    # Child -- redirect output, run headless
    os.setsid()
    with open(LOG_FILE, "a") as log_fd:
        os.dup2(log_fd.fileno(), sys.stdout.fileno())
        os.dup2(log_fd.fileno(), sys.stderr.fileno())

    from .runner import SimulationRunner, parse_config, quick_config, validate_config

    _setup_logging(verbose=True)

    if args.config:
        data = json.loads(Path(args.config).read_text())
        cfg = parse_config(data)
    else:
        cfg = quick_config(
            http_port=args.http_port or DEFAULT_HTTP_PORT,
            ct_port=args.ct_port or 12345,
        )

    if args.http_port:
        cfg.http_port = args.http_port
    if args.ct_port:
        cfg.ct_port = args.ct_port
    _apply_power_update_delay_override(cfg, args.power_update_delay)
    validate_config(cfg)
    runner = SimulationRunner(cfg)
    asyncio.run(runner.run_headless())


def cmd_stop(args: argparse.Namespace) -> None:
    port = args.http_port or DEFAULT_HTTP_PORT
    try:
        result = _http_post(port, "/shutdown")
        print(json.dumps(result, indent=2))
    except Exception:
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text().strip())
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"Sent SIGTERM to PID {pid}")
            except OSError as exc:
                print(f"Failed to stop daemon: {exc}")
        else:
            print("Daemon not running (no PID file)")
            sys.exit(1)
    PID_FILE.unlink(missing_ok=True)


def cmd_attach(args: argparse.Namespace) -> None:
    port = args.http_port or DEFAULT_HTTP_PORT
    try:
        _http_get(port, "/status")
    except Exception:
        print(f"Cannot connect to daemon on port {port}")
        sys.exit(1)
    _attach_tui(port)


def cmd_status(args: argparse.Namespace) -> None:
    port = args.http_port or DEFAULT_HTTP_PORT
    try:
        result = _http_get(port, "/status")
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(f"Cannot connect to daemon: {exc}")
        sys.exit(1)


def cmd_load(args: argparse.Namespace) -> None:
    port = args.http_port or DEFAULT_HTTP_PORT
    result = _http_post(port, f"/loads/{args.index}/toggle")
    print(json.dumps(result, indent=2))


def cmd_solar(args: argparse.Namespace) -> None:
    port = args.http_port or DEFAULT_HTTP_PORT
    value = args.value
    if value == "off":
        body: dict = {"watts": "off"}
    elif value == "max":
        body = {"watts": "max"}
    else:
        body = {"watts": float(value)}
    result = _http_post(port, "/solar", body)
    print(json.dumps(result, indent=2))


def cmd_battery(args: argparse.Namespace) -> None:
    port = args.http_port or DEFAULT_HTTP_PORT
    if args.action == "soc":
        if len(args.params) != 1:
            print("Usage: astra-sim battery <mac> soc <value>")
            sys.exit(1)
        result = _http_post(
            port, f"/batteries/{args.mac}/soc", {"soc": float(args.params[0])}
        )
    elif args.action == "max-power":
        if len(args.params) != 2:
            print("Usage: astra-sim battery <mac> max-power <charge> <discharge>")
            sys.exit(1)
        result = _http_post(
            port,
            f"/batteries/{args.mac}/max_power",
            {"charge": int(args.params[0]), "discharge": int(args.params[1])},
        )
    else:
        print(f"Unknown action: {args.action}")
        sys.exit(1)
    print(json.dumps(result, indent=2))


def cmd_auto(args: argparse.Namespace) -> None:
    port = args.http_port or DEFAULT_HTTP_PORT
    enabled = args.state.lower() in ("on", "true", "1")
    result = _http_post(port, "/auto", {"enabled": enabled})
    print(json.dumps(result, indent=2))


def cmd_config(args: argparse.Namespace) -> None:
    port = args.http_port or DEFAULT_HTTP_PORT
    ct_port = args.ct_port or 12345
    phases = getattr(args, "phases", 3) or 3
    json_paths = "$.phase_a" if phases == 1 else "$.phase_a,$.phase_b,$.phase_c"
    print(f"""\
[GENERAL]
DEVICE_TYPE = ct002

[CT002]
UDP_PORT = {ct_port}
ACTIVE_CONTROL = True

[JSON_HTTP]
URL = http://localhost:{port}/power
JSON_PATHS = {json_paths}""")


# -- TUI -------------------------------------------------------------------


def _run_with_tui(runner: SimulationRunner) -> None:
    """Start daemon in-process, then attach TUI in the same event loop."""
    try:
        from .tui import SimulatorApp
    except ImportError:
        print(
            "Textual is required for TUI mode. "
            "Install with: pip install 'astrameter[sim]'",
            file=sys.stderr,
        )
        print("Falling back to headless mode.")
        asyncio.run(runner.run_headless())
        return

    app = SimulatorApp(runner)
    app.run()


def _attach_tui(port: int) -> None:
    try:
        from .tui import SimulatorApp
    except ImportError:
        print(
            "Textual is required for TUI mode. "
            "Install with: pip install 'astrameter[sim]'",
            file=sys.stderr,
        )
        sys.exit(1)

    app = SimulatorApp.attach_to_daemon(port)
    app.run()


# -- logging ---------------------------------------------------------------


def _setup_logging(verbose: bool = False, *, tui: bool = False) -> None:
    """Configure root logging.

    In TUI mode, default level is WARNING so INFO lines (HTTP listen, battery
    startup, etc.) do not corrupt the terminal. Use ``--verbose`` for DEBUG.
    Headless mode uses INFO unless ``--verbose`` (then DEBUG).
    """
    if verbose:
        level = logging.DEBUG
    elif tui:
        level = logging.WARNING
    else:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


# -- argument parser -------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astra-sim",
        description="Marstek battery & powermeter simulator",
    )
    sub = parser.add_subparsers(dest="command")

    # -- run ---------------------------------------------------------------
    p_run = sub.add_parser("run", help="Start simulator (daemon + TUI)")
    p_run.add_argument("-c", "--config", help="JSON config file")
    p_run.add_argument("--batteries", type=int, default=1, help="Number of batteries")
    p_run.add_argument("--phases", type=int, default=1, choices=[1, 3])
    p_run.add_argument("--base-load", help="Comma-separated base load per phase")
    p_run.add_argument("--soc", type=float, default=0.5, help="Initial SOC (0.0-1.0)")
    p_run.add_argument("--ct-host", help="CT002 host")
    p_run.add_argument("--ct-port", type=int, help="CT002 UDP port")
    p_run.add_argument("--http-port", type=int, help="HTTP API port")
    p_run.add_argument("--no-tui", action="store_true", help="Headless mode")
    p_run.add_argument(
        "--time-scale",
        type=float,
        default=1.0,
        help="Speed up simulation time (e.g. 10 = 10x faster)",
    )
    p_run.add_argument(
        "--power-update-delay",
        type=int,
        default=None,
        metavar="N",
        help="Delay CT-derived power targets by N simulator ticks (0 = immediate)",
    )
    p_run.add_argument("-v", "--verbose", action="store_true")

    # -- start (daemon) ----------------------------------------------------
    p_start = sub.add_parser("start", help="Start daemon in background")
    p_start.add_argument("-c", "--config", help="JSON config file")
    p_start.add_argument("--http-port", type=int)
    p_start.add_argument("--ct-port", type=int)
    p_start.add_argument(
        "--power-update-delay",
        type=int,
        default=None,
        metavar="N",
        help="Delay CT-derived power targets by N simulator ticks (0 = immediate)",
    )

    # -- stop --------------------------------------------------------------
    p_stop = sub.add_parser("stop", help="Stop running daemon")
    p_stop.add_argument("--http-port", type=int)

    # -- attach ------------------------------------------------------------
    p_attach = sub.add_parser("attach", help="Attach TUI to running daemon")
    p_attach.add_argument("--http-port", type=int)

    # -- status ------------------------------------------------------------
    p_status = sub.add_parser("status", help="Show simulator status")
    p_status.add_argument("--http-port", type=int)

    # -- load --------------------------------------------------------------
    p_load = sub.add_parser("load", help="Control loads")
    p_load.add_argument("load_action", choices=["toggle"])
    p_load.add_argument("index", type=int, help="Load index (1-based)")
    p_load.add_argument("--http-port", type=int)

    # -- solar -------------------------------------------------------------
    p_solar = sub.add_parser("solar", help="Control solar")
    p_solar.add_argument("solar_action", choices=["set"])
    p_solar.add_argument("value", help="Watts, 'off', or 'max'")
    p_solar.add_argument("--http-port", type=int)

    # -- battery -----------------------------------------------------------
    p_bat = sub.add_parser("battery", help="Control a battery")
    p_bat.add_argument("mac", help="Battery MAC address")
    p_bat.add_argument("action", choices=["soc", "max-power"])
    p_bat.add_argument("params", nargs="+")
    p_bat.add_argument("--http-port", type=int)

    # -- auto --------------------------------------------------------------
    p_auto = sub.add_parser("auto", help="Toggle auto mode")
    p_auto.add_argument("state", choices=["on", "off"])
    p_auto.add_argument("--http-port", type=int)

    # -- config ------------------------------------------------------------
    p_cfg = sub.add_parser("config", help="Output astrameter config.ini snippet")
    p_cfg.add_argument("--http-port", type=int)
    p_cfg.add_argument("--ct-port", type=int)
    p_cfg.add_argument("--phases", type=int, choices=[1, 3], default=3)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    handlers = {
        "run": cmd_run,
        "start": cmd_start,
        "stop": cmd_stop,
        "attach": cmd_attach,
        "status": cmd_status,
        "load": cmd_load,
        "solar": cmd_solar,
        "battery": cmd_battery,
        "auto": cmd_auto,
        "config": cmd_config,
    }

    cmd = args.command
    if cmd is None:
        # Default: run
        args.command = "run"
        args.config = None
        args.batteries = 1
        args.phases = 1
        args.base_load = None
        args.soc = 0.5
        args.ct_host = None
        args.ct_port = None
        args.http_port = None
        args.no_tui = False
        args.time_scale = 1.0
        args.power_update_delay = None
        args.verbose = False
        cmd = "run"

    handler = handlers.get(cmd)
    if handler:
        handler(args)
    else:
        parser.print_help()
