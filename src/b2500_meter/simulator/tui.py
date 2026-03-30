"""Textual TUI for the simulator.

Can run in two modes:
- **In-process**: wraps a ``SimulationRunner`` and manages its async tasks.
- **Attach**: connects to a running daemon via HTTP/SSE.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from typing import TYPE_CHECKING, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Static
from textual_plot import HiResMode, PlotWidget

if TYPE_CHECKING:
    from .runner import SimulationRunner

logger = logging.getLogger("b2500_sim.tui")

# Column widths for the grid table
_COL = 12

# Number of data points kept for the sparkline graph
_GRAPH_HISTORY = 120


class SimulatorApp(App):
    """Main Textual application for the simulator TUI."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #grid-table, #battery-table, #load-panel, #help-bar {
        height: auto;
        padding: 0 1;
    }
    #grid-table { margin-top: 1; }
    #grid-graph {
        height: 12;
        margin: 0 1;
    }
    .section-title {
        text-style: bold;
        color: $accent;
        margin-top: 1;
    }
    .active-load { color: $success; }
    .inactive-load { color: $text-muted; }
    .selected-battery { text-style: bold reverse; }
    .phase-label { width: 12; }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("1", "toggle_load(1)", "Load 1", show=False),
        Binding("2", "toggle_load(2)", "Load 2", show=False),
        Binding("3", "toggle_load(3)", "Load 3", show=False),
        Binding("4", "toggle_load(4)", "Load 4", show=False),
        Binding("5", "toggle_load(5)", "Load 5", show=False),
        Binding("6", "toggle_load(6)", "Load 6", show=False),
        Binding("7", "toggle_load(7)", "Load 7", show=False),
        Binding("8", "toggle_load(8)", "Load 8", show=False),
        Binding("9", "set_soc(1)", "SOC 100%", show=False),
        Binding("up", "solar_adjust(100)", "Solar +100W", show=False),
        Binding("down", "solar_adjust(-100)", "Solar -100W", show=False),
        Binding("s", "solar_max", "Solar Max"),
        Binding("shift+s", "solar_off", "Solar Off"),
        Binding("b", "cycle_battery", "Select Battery"),
        Binding("left", "adjust_soc(-0.1)", "SOC -10%", show=False),
        Binding("right", "adjust_soc(0.1)", "SOC +10%", show=False),
        Binding("0", "set_soc(0)", "SOC 0%", show=False),
        Binding("p", "adjust_max_power(-100)", "Power -100W", show=False),
        Binding("shift+p", "adjust_max_power(100)", "Power +100W", show=False),
        Binding("a", "toggle_auto", "Auto Mode"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        runner: SimulationRunner | None = None,
        daemon_port: int | None = None,
    ) -> None:
        super().__init__()
        self._runner = runner
        self._daemon_port = daemon_port
        self._selected_battery: int = 0
        self._status: dict = {}
        self._auto_task: asyncio.Task | None = None
        self._bg_task: asyncio.Task | None = None
        self._grid_history: deque[float] = deque(maxlen=_GRAPH_HISTORY)
        self.title = "b2500-sim"

    @classmethod
    def attach_to_daemon(cls, port: int) -> SimulatorApp:
        return cls(runner=None, daemon_port=port)

    # -- compose -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static("[b]GRID POWER[/b]", classes="section-title")
            yield Static(id="grid-table")
            yield PlotWidget(id="grid-graph")
            yield Static("[b]BATTERIES[/b]", classes="section-title")
            yield Static(id="battery-table")
            yield Static("[b]LOADS[/b]", classes="section-title")
            yield Static(id="load-panel")
            yield Static(id="solar-bar")
        yield Footer()

    # -- lifecycle ---------------------------------------------------------

    async def on_mount(self) -> None:
        if self._runner:
            # In-process mode: start powermeter + batteries as workers
            self.run_worker(self._run_simulation, exclusive=True, group="sim")  # type: ignore[arg-type]
        elif self._daemon_port:
            # Attach mode: connect SSE
            self.run_worker(self._sse_listener, exclusive=True, group="sse")  # type: ignore[arg-type]
        self.set_interval(1.0, self._refresh_display)

    async def _run_simulation(self) -> None:
        runner = self._runner
        assert runner is not None
        await runner.powermeter.start()
        tasks = [asyncio.create_task(b.run()) for b in runner.batteries]
        if runner.load_model.auto_mode:
            self._auto_task = asyncio.create_task(runner._auto_loop())
            tasks.append(self._auto_task)
        try:
            await runner.powermeter.shutdown_event.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await runner.powermeter.stop()

    async def _sse_listener(self) -> None:
        """Connect to daemon SSE endpoint and update status."""
        import aiohttp

        url = f"http://localhost:{self._daemon_port}/events"
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url, timeout=aiohttp.ClientTimeout(total=None, sock_read=60)
                ) as resp,
            ):
                buffer = ""
                async for chunk_bytes in resp.content.iter_any():
                    buffer += chunk_bytes.decode("utf-8", errors="replace")
                    while "\n\n" in buffer:
                        event, buffer = buffer.split("\n\n", 1)
                        for line in event.split("\n"):
                            if line.startswith("data: "):
                                with contextlib.suppress(json.JSONDecodeError):
                                    self._status = json.loads(line[6:])
        except Exception as exc:
            logger.error("SSE connection lost: %s", exc)

    # -- display refresh ---------------------------------------------------

    def _refresh_display(self) -> None:
        if self._runner:
            self._status = self._runner.powermeter._build_status()

        status = self._status
        if not status:
            return

        # Grid table + graph
        grid = status.get("grid", {})
        grid_text = self._format_grid(grid)
        self.query_one("#grid-table", Static).update(grid_text)

        total = grid.get("total", 0.0)
        self._grid_history.append(total)
        self._update_grid_graph()

        # Batteries
        batteries = status.get("batteries", [])
        bat_text = self._format_batteries(batteries)
        self.query_one("#battery-table", Static).update(bat_text)

        # Loads
        loads = status.get("loads", [])
        load_text = self._format_loads(loads)
        self.query_one("#load-panel", Static).update(load_text)

        # Solar
        solar = status.get("solar", {})
        auto = status.get("auto_mode", False)
        solar_text = self._format_solar(solar, auto)
        self.query_one("#solar-bar", Static).update(solar_text)

    def _format_grid(self, grid: dict) -> str:
        pa = grid.get("phase_a", 0)
        pb = grid.get("phase_b", 0)
        pc = grid.get("phase_c", 0)
        total = grid.get("total", 0)

        def _color(v: float) -> str:
            return "green" if v >= 0 else "red"

        lines = [
            f"  {'':20s} {'Phase A':>{_COL}s} {'Phase B':>{_COL}s} "
            f"{'Phase C':>{_COL}s} {'Total':>{_COL}s}",
            f"  {'Net grid':20s} "
            f"[{_color(pa)}]{pa:>{_COL}.0f}W[/] "
            f"[{_color(pb)}]{pb:>{_COL}.0f}W[/] "
            f"[{_color(pc)}]{pc:>{_COL}.0f}W[/] "
            f"[{_color(total)}]{total:>{_COL}.0f}W[/]",
        ]
        return "\n".join(lines)

    def _format_batteries(self, batteries: list[dict]) -> str:
        if not batteries:
            return "  (none)"
        lines = [
            f"  {'':2s}{'MAC':14s} {'Phase':>6s} {'Power':>8s} "
            f"{'Target':>8s} {'SOC':>6s}  {'':10s}"
        ]
        for i, b in enumerate(batteries):
            sel = "▸ " if i == self._selected_battery else "  "
            soc = b.get("soc", 0)
            bar_len = 8
            filled = int(soc * bar_len)
            bar = "█" * filled + "░" * (bar_len - filled)
            lines.append(
                f"  {sel}{b.get('mac', '?'):14s} "
                f"{b.get('phase', '?'):>6s} "
                f"{b.get('power', 0):>7.0f}W "
                f"{b.get('target', 0):>7.0f}W "
                f"{soc * 100:>5.0f}%  {bar}"
            )
        return "\n".join(lines)

    def _format_loads(self, loads: list[dict]) -> str:
        if not loads:
            return "  (none configured)"
        lines = []
        for i, ld in enumerate(loads):
            key = i + 1
            state = "[green]■ ON [/]" if ld.get("active") else "[dim]  OFF[/]"
            lines.append(
                f"  [{key}] {ld.get('name', '?'):20s} {ld.get('power', 0):>5.0f}W  "
                f"{state}  (phase {ld.get('phase', '?')})"
            )
        return "\n".join(lines)

    def _format_solar(self, solar: dict, auto: bool) -> str:
        current = solar.get("current", 0)
        mx = solar.get("max", 1)
        phases = solar.get("phases", [])
        pct = current / mx if mx > 0 else 0
        bar_len = 20
        filled = int(pct * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        auto_tag = " [yellow][AUTO][/]" if auto else ""
        return (
            f"\n  Solar: {bar} {current:.0f}W / {mx:.0f}W  "
            f"(phases {','.join(phases)})  [↑/↓ adjust]{auto_tag}\n\n"
            f"  [1-9] toggle load  [↑↓] solar  [s/S] solar max/off\n"
            f"  [b] select battery  [←→] SOC ±10%  [0/9] SOC 0%/100%\n"
            f"  [p/P] max power ±100W  [a] auto mode  [q] quit"
        )

    # -- grid graph --------------------------------------------------------

    def _update_grid_graph(self) -> None:
        """Update the PlotWidget with grid power history."""
        data = list(self._grid_history)
        if not data:
            return
        pw = self.query_one("#grid-graph", PlotWidget)
        pw.clear()
        n = len(data)
        xs = [float(-n + 1 + i) for i in range(n)]
        pw.plot(xs, data, line_style="white", hires_mode=HiResMode.BRAILLE)
        pw.set_ylabel("W")
        pw.set_xlabel("seconds ago")

    # -- actions -----------------------------------------------------------

    def action_toggle_load(self, index: int) -> None:
        if self._runner:
            with contextlib.suppress(IndexError):
                self._runner.load_model.toggle_load(index)
        elif self._daemon_port:
            self._post(f"/loads/{index}/toggle")

    def action_solar_adjust(self, delta: int) -> None:
        if self._runner:
            lm = self._runner.load_model
            lm.set_solar(lm.solar_power + delta)
        elif self._daemon_port:
            current = self._status.get("solar", {}).get("current", 0)
            self._post("/solar", {"watts": max(0, current + delta)})

    def action_solar_max(self) -> None:
        if self._runner:
            self._runner.load_model.set_solar(self._runner.load_model.solar_max)
        elif self._daemon_port:
            self._post("/solar", {"watts": "max"})

    def action_solar_off(self) -> None:
        if self._runner:
            self._runner.load_model.set_solar(0)
        elif self._daemon_port:
            self._post("/solar", {"watts": "off"})

    def action_cycle_battery(self) -> None:
        n = len(self._status.get("batteries", []))
        if n > 0:
            self._selected_battery = (self._selected_battery + 1) % n

    def action_adjust_soc(self, delta: float) -> None:
        bat = self._get_selected_battery()
        if bat is None:
            return
        if self._runner:
            bat_obj = self._runner.batteries[self._selected_battery]
            bat_obj.soc = bat_obj.soc + delta
        elif self._daemon_port:
            new_soc = max(0, min(1, bat.get("soc", 0.5) + delta))
            self._post(f"/batteries/{bat['mac']}/soc", {"soc": new_soc})

    def action_set_soc(self, value: float) -> None:
        bat = self._get_selected_battery()
        if bat is None:
            return
        if self._runner:
            self._runner.batteries[self._selected_battery].soc = value
        elif self._daemon_port:
            self._post(f"/batteries/{bat['mac']}/soc", {"soc": value})

    def action_adjust_max_power(self, delta: int) -> None:
        bat = self._get_selected_battery()
        if bat is None:
            return
        if self._runner:
            b = self._runner.batteries[self._selected_battery]
            b.max_charge_power = max(0, b.max_charge_power + delta)
            b.max_discharge_power = max(0, b.max_discharge_power + delta)
        elif self._daemon_port:
            charge = max(0, bat.get("max_charge", 800) + delta)
            discharge = max(0, bat.get("max_discharge", 800) + delta)
            self._post(
                f"/batteries/{bat['mac']}/max_power",
                {"charge": charge, "discharge": discharge},
            )

    def action_toggle_auto(self) -> None:
        if self._runner:
            lm = self._runner.load_model
            lm.auto_mode = not lm.auto_mode
            if lm.auto_mode:
                if self._auto_task is None or self._auto_task.done():
                    self._auto_task = asyncio.ensure_future(self._runner._auto_loop())
            elif self._auto_task is not None and not self._auto_task.done():
                self._auto_task.cancel()
                self._auto_task = None
        elif self._daemon_port:
            current = self._status.get("auto_mode", False)
            self._post("/auto", {"enabled": not current})

    async def action_quit(self) -> None:
        if self._runner:
            self._runner.powermeter.shutdown_event.set()
        self.exit()

    # -- helpers -----------------------------------------------------------

    def _get_selected_battery(self) -> dict | None:
        batteries = self._status.get("batteries", [])
        if 0 <= self._selected_battery < len(batteries):
            return batteries[self._selected_battery]
        return None

    def _post(self, path: str, body: dict | None = None) -> None:
        """Fire-and-forget HTTP POST to daemon (runs in background)."""
        if self._daemon_port:
            self._bg_task = asyncio.create_task(self._async_post(path, body))

    async def _async_post(self, path: str, body: dict | None = None) -> None:
        """Non-blocking HTTP POST to daemon."""
        import aiohttp

        url = f"http://localhost:{self._daemon_port}{path}"
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    url,
                    json=body or {},
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as resp,
            ):
                await resp.read()
        except Exception:
            pass
