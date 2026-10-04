#!/usr/bin/env python3
"""Memory benchmark: run AstraMeter for real and sample how much memory it holds.

Starts ``astrameter`` (a local interpreter, or a Docker image with ``--docker``)
against simulated inputs and samples its memory once per sample interval: RSS,
plus PSS and USS from ``/proc/<pid>/smaps_rollup``, and with ``--docker`` the
container's cgroup usage, which is the number ``docker stats`` shows. Linux only.

Scenarios
  ct002-json   three astra-sim batteries polling the CT002 emulator over UDP,
               with astra-sim's own JSON endpoint as the [JSON_HTTP] meter
  ct002-ha     the same batteries, metered by a stand-in Home Assistant whose
               websocket pushes astra-sim's grid power (the add-on default)
  shelly-json  the Shelly Pro 3EM emulator polled over UDP by stand-in
               batteries, with a [JSON_HTTP] meter. It binds UDP port 1010, so
               it needs root and two runs of it cannot overlap.

Every scenario also polls /api/status the way an open dashboard tab does.

Compare two checkouts by running the same scenario with each one's
interpreter, e.g.::

    base/.venv/bin/python tools/membench.py ct002-json --label base
    .venv/bin/python tools/membench.py ct002-json --label head

The interval flags and ``--time-scale`` multiply the traffic, so a few minutes
cover what takes hours at the real rate; that is the run to read the growth
slope from. ``--snap-at`` writes tracemalloc snapshots (under
``tools/membench_traced.py``) to diff with ``tracemalloc.Snapshot.compare_to``.

Prints a JSON summary; the per-sample CSV and every log land in ``--workdir``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

HERE = Path(__file__).resolve().parent
SHELLY_PORT = 1010  # fixed by the shellypro3em device type


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _kib_fields(text: str) -> dict[str, float]:
    """``Key: N kB`` lines as MiB."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if len(parts) == 2 and parts[1] == "kB":
            out[key.strip()] = int(parts[0]) / 1024
        elif len(parts) == 1 and parts[0].isdigit():
            out[key.strip()] = float(parts[0])
    return out


def read_mem(pid: int) -> dict[str, float] | None:
    try:
        rollup = _kib_fields(Path(f"/proc/{pid}/smaps_rollup").read_text())
        status = _kib_fields(Path(f"/proc/{pid}/status").read_text())
    except (FileNotFoundError, ProcessLookupError):
        return None
    return {
        "rss": status.get("VmRSS", 0.0),
        "pss": rollup.get("Pss", 0.0),
        "uss": rollup.get("Private_Clean", 0.0) + rollup.get("Private_Dirty", 0.0),
        "threads": status.get("Threads", 0.0),
    }


def cgroup_mem(container: str) -> float:
    """What ``docker stats`` shows: usage less inactive file cache, in MiB."""
    for base, usage_file, inactive in (
        (
            f"/sys/fs/cgroup/memory/docker/{container}",
            "memory.usage_in_bytes",
            "total_inactive_file",
        ),
        (
            f"/sys/fs/cgroup/memory/system.slice/docker-{container}.scope",
            "memory.usage_in_bytes",
            "total_inactive_file",
        ),
        (
            f"/sys/fs/cgroup/system.slice/docker-{container}.scope",
            "memory.current",
            "inactive_file",
        ),
    ):
        d = Path(base)
        if (d / usage_file).exists():
            stat = dict(
                line.split() for line in (d / "memory.stat").read_text().splitlines()
            )
            usage = int((d / usage_file).read_text()) - int(stat.get(inactive, 0))
            return usage / 2**20
    return 0.0


# -- stand-in power sources and clients ---------------------------------------


class Load:
    """The stand-in meters, dashboard tab and Shelly batteries."""

    def __init__(self, args: argparse.Namespace, sim_power_url: str | None) -> None:
        self.args = args
        self.sim_power_url = sim_power_url
        self.ws_clients: dict[web.WebSocketResponse, tuple[int, list[str]]] = {}
        self.phases = [300.0, 150.0, -80.0]
        self.stats = {"ha_pushes": 0, "dash_polls": 0, "dash_200": 0, "shelly_ok": 0}

    def _walk(self) -> list[float]:
        self.phases = [p + random.uniform(-25, 25) for p in self.phases]
        return [round(p, 1) for p in self.phases]

    async def _grid(self, session: aiohttp.ClientSession) -> list[float]:
        if self.sim_power_url:
            with contextlib.suppress(aiohttp.ClientError, KeyError, ValueError):
                async with session.get(self.sim_power_url) as r:
                    d = await r.json()
                return [d["phase_a"], d["phase_b"], d["phase_c"]]
        return self._walk()

    async def handle_power(self, request: web.Request) -> web.Response:
        a, b, c = self._walk()
        return web.json_response({"phase_a": a, "phase_b": b, "phase_c": c})

    async def handle_state(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "entity_id": request.match_info["eid"],
                "state": "100",
                "attributes": {"unit_of_measurement": "W"},
            }
        )

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.9.0"})
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if data.get("type") == "auth":
                await ws.send_json({"type": "auth_ok", "ha_version": "2026.9.0"})
            elif data.get("type") == "subscribe_entities":
                await ws.send_json(
                    {"id": data["id"], "type": "result", "success": True}
                )
                self.ws_clients[ws] = (data["id"], data["entity_ids"])
        self.ws_clients.pop(ws, None)
        return ws

    async def ha_pusher(self) -> None:
        async with aiohttp.ClientSession() as session:
            while True:
                await asyncio.sleep(self.args.ha_interval)
                grid = await self._grid(session)
                for ws, (sub_id, entities) in list(self.ws_clients.items()):
                    changes = {
                        eid: {"+": {"s": str(grid[i % 3]), "lc": time.time()}}
                        for i, eid in enumerate(entities)
                    }
                    with contextlib.suppress(ConnectionError, RuntimeError):
                        await ws.send_json(
                            {"id": sub_id, "type": "event", "event": {"c": changes}}
                        )
                        self.stats["ha_pushes"] += 1

    async def dashboard_tab(self, url: str) -> None:
        etag = None
        async with aiohttp.ClientSession() as session:
            while True:
                await asyncio.sleep(self.args.dash_interval)
                headers: dict[str, str] = {"If-None-Match": etag} if etag else {}
                with contextlib.suppress(aiohttp.ClientError):
                    async with session.get(url, headers=headers) as r:
                        self.stats["dash_polls"] += 1
                        if r.status == 200:
                            await r.read()
                            etag = r.headers.get("ETag")
                            self.stats["dash_200"] += 1

    async def shelly_battery(self, idx: int) -> None:
        loop = asyncio.get_running_loop()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # One address per battery: the emulator tells batteries apart by IP.
            sock.bind((f"127.0.0.{idx + 2}", 0))
            sock.setblocking(False)
            for rid in range(1, sys.maxsize):
                await asyncio.sleep(self.args.shelly_interval)
                req = {"id": rid, "method": "EM.GetStatus", "params": {"id": 0}}
                sock.sendto(json.dumps(req).encode(), ("127.0.0.1", SHELLY_PORT))
                with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                    await asyncio.wait_for(loop.sock_recv(sock, 4096), timeout=3)
                    self.stats["shelly_ok"] += 1


# -- orchestration --------------------------------------------------------------


def write_config(path: Path, scenario: str, ports: dict[str, int]) -> None:
    lines = ["[GENERAL]", f"WEB_SERVER_PORT = {ports['web']}"]
    if scenario.startswith("ct002"):
        lines += ["DEVICE_TYPE = ct002", "", "[CT002]", f"UDP_PORT = {ports['ct']}"]
    else:
        lines += ["DEVICE_TYPE = shellypro3em"]
    if scenario.endswith("-ha"):
        lines += [
            "",
            "[HOMEASSISTANT]",
            "IP = 127.0.0.1",
            f"PORT = {ports['fake']}",
            "ACCESSTOKEN = token",
            "CURRENT_POWER_ENTITY = sensor.l1,sensor.l2,sensor.l3",
        ]
    else:
        source = "sim" if scenario.startswith("ct002") else "fake"
        lines += [
            "",
            "[JSON_HTTP]",
            f"URL = http://127.0.0.1:{ports[source]}/power",
            "JSON_PATHS = $.phase_a,$.phase_b,$.phase_c",
        ]
    path.write_text("\n".join(lines) + "\n")


async def wait_http(url: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            with contextlib.suppress(aiohttp.ClientError):
                async with session.get(url) as r:
                    if r.status < 500:
                        return
            await asyncio.sleep(0.2)
    raise RuntimeError(f"{url} never came up")


def summarize(samples: list[tuple[float, dict[str, float]]], warmup: float) -> dict:
    steady = [(t, m) for t, m in samples if t >= warmup] or samples
    out: dict[str, Any] = {}
    for key in samples[0][1]:
        if key == "threads":
            continue
        xs = [t for t, _ in steady]
        ys = [m[key] for _, m in steady]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
        slope = cov / den if den else 0.0
        out[key] = {
            "warm": round(ys[0], 1),
            "end": round(ys[-1], 1),
            "max": round(max(m[key] for _, m in samples), 1),
            "slope_mib_per_h": round(slope * 3600, 2),
        }
    out["threads"] = samples[-1][1]["threads"]
    return out


class Target:
    """The AstraMeter process under measurement, local or containerised."""

    def __init__(self, args: argparse.Namespace, cfg: Path, log: Path) -> None:
        self.container: str | None = None
        self.log = log
        argv = ["-c", str(cfg), "--loglevel", args.loglevel]
        if args.docker:
            os.chmod(cfg.parent, 0o755)
            os.chmod(cfg, 0o644)
            self.container = subprocess.check_output(
                [
                    "docker", "run", "-d", "--network", "host",
                    "-v", f"{cfg.parent}:/work",
                    *(f"-e{kv}" for kv in args.env),
                    args.docker,
                    "astrameter", "-c", f"/work/{cfg.name}",
                    "--loglevel", args.loglevel,
                ],
                text=True,
            ).strip()  # fmt: skip
            self.proc = None
            try:
                self.pid = int(
                    subprocess.check_output(
                        ["docker", "inspect", "-f", "{{.State.Pid}}", self.container],
                        text=True,
                    )
                )
            except BaseException:
                # Not yet registered for cleanup: remove the container here.
                subprocess.run(
                    ["docker", "rm", "-f", self.container], capture_output=True
                )
                raise
        else:
            wrapper = [str(HERE / "membench_traced.py")] if args.snap_at else []
            env = {**os.environ, **dict(kv.split("=", 1) for kv in args.env)}
            if args.snap_at:
                env["TRACE_DIR"] = str(cfg.parent)
            with open(log, "w") as out:
                self.proc = subprocess.Popen(
                    [args.python, *(wrapper or ["-m", "astrameter.main"]), *argv],
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    cwd=cfg.parent,
                    env=env,
                )
            self.pid = self.proc.pid

    def sample(self) -> dict[str, float] | None:
        mem = read_mem(self.pid)
        if mem is not None and self.container:
            mem["cgroup"] = cgroup_mem(self.container)
        return mem

    def snapshot(self) -> None:
        os.kill(self.pid, signal.SIGUSR2)

    def stop(self) -> None:
        if self.container:
            with open(self.log, "w") as out:
                subprocess.run(
                    ["docker", "logs", self.container], stdout=out, stderr=out
                )
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)
        elif self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


async def run(args: argparse.Namespace) -> dict:
    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    ports = {
        "web": free_port(),
        "fake": free_port(),
        "sim": free_port(),
        "ct": free_port(socket.SOCK_DGRAM),
    }
    with contextlib.ExitStack() as stack:
        sim_url = None
        if args.scenario.startswith("ct002"):
            sim_log = stack.enter_context(open(work / "sim.log", "w"))
            sim = subprocess.Popen(
                [
                    str(Path(args.python).parent / "astra-sim"),
                    "run", "--no-tui",
                    "--batteries", str(args.batteries),
                    "--phases", "3",
                    "--http-port", str(ports["sim"]),
                    "--ct-port", str(ports["ct"]),
                    "--time-scale", str(args.time_scale),
                ],
                stdout=sim_log,
                stderr=subprocess.STDOUT,
                cwd=work,
            )  # fmt: skip
            stack.callback(sim.terminate)
            sim_url = f"http://127.0.0.1:{ports['sim']}/power"
            await wait_http(sim_url)

        load = Load(args, sim_url)
        app = web.Application()
        app.router.add_get("/power", load.handle_power)
        app.router.add_get("/api/states/{eid}", load.handle_state)
        app.router.add_get("/api/websocket", load.handle_ws)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", ports["fake"]).start()

        cfg = work / "config.ini"
        write_config(cfg, args.scenario, ports)
        target = Target(args, cfg, work / "astrameter.log")
        stack.callback(target.stop)
        await wait_http(f"http://127.0.0.1:{ports['web']}/health")

        tasks = [
            asyncio.create_task(load.ha_pusher()),
            asyncio.create_task(
                load.dashboard_tab(f"http://127.0.0.1:{ports['web']}/api/status")
            ),
        ]
        if args.scenario.startswith("shelly"):
            tasks += [
                asyncio.create_task(load.shelly_battery(i))
                for i in range(args.batteries)
            ]

        samples: list[tuple[float, dict[str, float]]] = []
        snaps = sorted(args.snap_at)
        t0 = time.monotonic()
        with open(work / "samples.csv", "w") as csv:
            while (t := time.monotonic() - t0) < args.duration:
                mem = target.sample()
                if mem is None:
                    raise RuntimeError(f"astrameter exited; see {target.log}")
                if not samples:
                    csv.write("t," + ",".join(mem) + "\n")
                samples.append((t, mem))
                csv.write(f"{t:.1f}," + ",".join(f"{v:.2f}" for v in mem.values()))
                csv.write("\n")
                if snaps and t >= snaps[0]:
                    snaps.pop(0)
                    target.snapshot()
                await asyncio.sleep(args.sample_interval)
        for task in tasks:
            task.cancel()
        await runner.cleanup()
    return {
        "label": args.label,
        "scenario": args.scenario,
        "duration": args.duration,
        **summarize(samples, args.warmup),
        "traffic": load.stats,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("scenario", choices=["ct002-json", "ct002-ha", "shelly-json"])
    ap.add_argument("--label", default="run", help="names the run's work directory")
    ap.add_argument(
        "--python",
        default=sys.executable,
        help="interpreter whose astrameter (and astra-sim) to run",
    )
    ap.add_argument("--docker", help="measure this image instead of --python")
    ap.add_argument("--duration", type=float, default=300, help="seconds")
    ap.add_argument(
        "--warmup", type=float, default=60, help="seconds before the slope starts"
    )
    ap.add_argument("--sample-interval", type=float, default=1.0)
    ap.add_argument("--batteries", type=int, default=3)
    ap.add_argument("--time-scale", type=float, default=1.0, help="astra-sim speed")
    ap.add_argument("--ha-interval", type=float, default=1.0)
    ap.add_argument("--dash-interval", type=float, default=1.0)
    ap.add_argument("--shelly-interval", type=float, default=1.0)
    ap.add_argument("--loglevel", default="warning")
    ap.add_argument("--env", nargs="*", default=[], help="KEY=VALUE for astrameter")
    ap.add_argument(
        "--snap-at",
        nargs="*",
        type=float,
        default=[],
        help="seconds at which to write a tracemalloc snapshot (local runs only)",
    )
    ap.add_argument("--workdir", default=str(HERE / ".membench"))
    args = ap.parse_args()
    if args.docker and args.snap_at:
        # The image runs plain astrameter, which has no SIGUSR2 handler: the
        # first snapshot would kill it.
        ap.error("--snap-at works only for local runs, not with --docker")
    args.workdir = str(Path(args.workdir) / f"{args.label}-{args.scenario}")
    print(json.dumps(asyncio.run(run(args))))


if __name__ == "__main__":
    main()
