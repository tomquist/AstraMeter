"""Unit tests for simulator/powermeter_sim.py — HTTP handler coverage."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from astrameter.simulator.battery import BatterySimulator
from astrameter.simulator.load_model import Load, LoadModel
from astrameter.simulator.powermeter_sim import PowermeterSimulator

# -- fixtures --------------------------------------------------------------


def _make_battery(mac="AABBCCDDEE01", phase="A", **kw) -> BatterySimulator:
    defaults = dict(
        ct_mac="112233445566",
        ct_host="127.0.0.1",
        ct_port=12345,
        max_charge_power=800,
        max_discharge_power=800,
        capacity_wh=2560.0,
        initial_soc=0.5,
        ramp_rate=200.0,
        poll_interval=1.0,
        time_scale=1.0,
    )
    defaults.update(kw)
    return BatterySimulator(mac=mac, phase=phase, **defaults)


def _make_load_model(**kw) -> LoadModel:
    defaults = dict(
        base_load=[100.0, 100.0, 100.0],
        base_noise=0.0,  # deterministic for testing
        loads=[Load("Lamp", 50.0, "A")],
        solar_max=2000.0,
        solar_phases=["A"],
    )
    defaults.update(kw)
    return LoadModel(**defaults)


@pytest.fixture
async def sim_client():
    battery = _make_battery()
    load_model = _make_load_model()
    sim = PowermeterSimulator(
        batteries=[battery],
        load_model=load_model,
        host="127.0.0.1",
        port=0,
    )
    async with TestClient(TestServer(sim._app)) as client:
        yield client, sim, battery, load_model


@pytest.fixture
async def sim_client_dc():
    """Client with a battery that has DC input capability."""
    battery = _make_battery(max_dc_input=500, dc_input_power=0.0)
    load_model = _make_load_model()
    sim = PowermeterSimulator(
        batteries=[battery],
        load_model=load_model,
        host="127.0.0.1",
        port=0,
    )
    async with TestClient(TestServer(sim._app)) as client:
        yield client, sim, battery, load_model


# -- compute_grid ----------------------------------------------------------


def test_compute_grid_basic():
    battery = _make_battery(phase="A")
    battery.current_power = 100.0
    load_model = _make_load_model(base_noise=0.0)
    sim = PowermeterSimulator([battery], load_model)
    grid = sim.compute_grid()
    assert grid["phase_a"] == pytest.approx(100.0 - 100.0, abs=0.2)
    assert grid["phase_b"] == pytest.approx(100.0, abs=0.2)
    assert grid["phase_c"] == pytest.approx(100.0, abs=0.2)


def test_compute_grid_multi_battery():
    b1 = _make_battery(mac="AABBCCDDEE01", phase="A")
    b1.current_power = 50.0
    b2 = _make_battery(mac="AABBCCDDEE02", phase="B")
    b2.current_power = 75.0
    load_model = _make_load_model(base_noise=0.0)
    sim = PowermeterSimulator([b1, b2], load_model)
    grid = sim.compute_grid()
    assert grid["phase_a"] == pytest.approx(100.0 - 50.0, abs=0.2)
    assert grid["phase_b"] == pytest.approx(100.0 - 75.0, abs=0.2)


# -- /power endpoint -------------------------------------------------------


async def test_power_endpoint(sim_client):
    client, _sim, _battery, _ = sim_client
    resp = await client.get("/power")
    assert resp.status == 200
    data = await resp.json()
    assert "phase_a" in data
    assert "phase_b" in data
    assert "phase_c" in data


# -- /status endpoint ------------------------------------------------------


async def test_status_endpoint(sim_client):
    client, *_ = sim_client
    resp = await client.get("/status")
    assert resp.status == 200
    data = await resp.json()
    assert "grid" in data
    assert "batteries" in data


# -- /loads/{index}/toggle -------------------------------------------------


async def test_toggle_load(sim_client):
    client, _sim, _, load_model = sim_client
    assert load_model.loads[0].active is False
    resp = await client.post("/loads/1/toggle")
    assert resp.status == 200
    assert load_model.loads[0].active is True


async def test_toggle_load_invalid_index(sim_client):
    client, *_ = sim_client
    resp = await client.post("/loads/abc/toggle")
    assert resp.status == 400


async def test_toggle_load_out_of_range(sim_client):
    client, *_ = sim_client
    resp = await client.post("/loads/999/toggle")
    assert resp.status == 400


# -- /solar ----------------------------------------------------------------


async def test_set_solar(sim_client):
    client, _, _, load_model = sim_client
    resp = await client.post("/solar", json={"watts": 500.0})
    assert resp.status == 200
    assert load_model.solar_power == pytest.approx(500.0)


async def test_set_solar_max(sim_client):
    client, _, _, load_model = sim_client
    resp = await client.post("/solar", json={"watts": "max"})
    assert resp.status == 200
    assert load_model.solar_power == pytest.approx(load_model.solar_max)


async def test_set_solar_off(sim_client):
    client, _, _, load_model = sim_client
    load_model.solar_power = 500.0
    resp = await client.post("/solar", json={"watts": "off"})
    assert resp.status == 200
    assert load_model.solar_power == pytest.approx(0.0)


async def test_set_solar_invalid_string(sim_client):
    client, *_ = sim_client
    resp = await client.post("/solar", json={"watts": "bogus"})
    assert resp.status == 400


async def test_set_solar_missing_watts(sim_client):
    client, *_ = sim_client
    resp = await client.post("/solar", json={})
    assert resp.status == 400


async def test_set_solar_invalid_json(sim_client):
    client, *_ = sim_client
    resp = await client.post(
        "/solar", data=b"not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status == 400


# -- /batteries/{mac}/soc -------------------------------------------------


async def test_set_battery_soc(sim_client):
    client, _, battery, _ = sim_client
    resp = await client.post(f"/batteries/{battery.mac}/soc", json={"soc": 0.8})
    assert resp.status == 200
    assert battery.soc == pytest.approx(0.8)


async def test_set_battery_soc_invalid(sim_client):
    client, _, battery, _ = sim_client
    resp = await client.post(f"/batteries/{battery.mac}/soc", json={"soc": 1.5})
    assert resp.status == 400


async def test_set_battery_soc_not_found(sim_client):
    client, *_ = sim_client
    resp = await client.post("/batteries/NONEXISTENT/soc", json={"soc": 0.5})
    assert resp.status == 404


async def test_set_battery_soc_missing(sim_client):
    client, _, battery, _ = sim_client
    resp = await client.post(f"/batteries/{battery.mac}/soc", json={})
    assert resp.status == 400


async def test_set_battery_soc_non_numeric(sim_client):
    client, _, battery, _ = sim_client
    resp = await client.post(f"/batteries/{battery.mac}/soc", json={"soc": "abc"})
    assert resp.status == 400


# -- /batteries/{mac}/max_power --------------------------------------------


async def test_set_battery_max_power(sim_client):
    client, _, battery, _ = sim_client
    resp = await client.post(
        f"/batteries/{battery.mac}/max_power",
        json={"charge": 500, "discharge": 600},
    )
    assert resp.status == 200
    assert battery.max_charge_power == 500
    assert battery.max_discharge_power == 600


async def test_set_battery_max_power_negative(sim_client):
    client, _, battery, _ = sim_client
    resp = await client.post(f"/batteries/{battery.mac}/max_power", json={"charge": -1})
    assert resp.status == 400


async def test_set_battery_max_power_invalid_type(sim_client):
    client, _, battery, _ = sim_client
    resp = await client.post(
        f"/batteries/{battery.mac}/max_power", json={"charge": "abc"}
    )
    assert resp.status == 400


async def test_set_battery_max_power_not_found(sim_client):
    client, *_ = sim_client
    resp = await client.post("/batteries/NONEXISTENT/max_power", json={"charge": 500})
    assert resp.status == 404


# -- /batteries/{mac}/dc ---------------------------------------------------


async def test_set_battery_dc(sim_client_dc):
    client, _, battery, _ = sim_client_dc
    resp = await client.post(f"/batteries/{battery.mac}/dc", json={"watts": 200.0})
    assert resp.status == 200
    assert battery.dc_input_power == pytest.approx(200.0)


async def test_set_battery_dc_no_capability(sim_client):
    client, _, battery, _ = sim_client
    resp = await client.post(f"/batteries/{battery.mac}/dc", json={"watts": 100.0})
    assert resp.status == 400


async def test_set_battery_dc_out_of_range(sim_client_dc):
    client, _, battery, _ = sim_client_dc
    resp = await client.post(f"/batteries/{battery.mac}/dc", json={"watts": 999.0})
    assert resp.status == 400


async def test_set_battery_dc_missing_watts(sim_client_dc):
    client, _, battery, _ = sim_client_dc
    resp = await client.post(f"/batteries/{battery.mac}/dc", json={})
    assert resp.status == 400


async def test_set_battery_dc_invalid(sim_client_dc):
    client, _, battery, _ = sim_client_dc
    resp = await client.post(f"/batteries/{battery.mac}/dc", json={"watts": "abc"})
    assert resp.status == 400


async def test_set_battery_dc_infinite(sim_client_dc):
    client, _, battery, _ = sim_client_dc
    resp = await client.post(
        f"/batteries/{battery.mac}/dc", json={"watts": float("inf")}
    )
    assert resp.status == 400


async def test_set_battery_dc_not_found(sim_client_dc):
    client, *_ = sim_client_dc
    resp = await client.post("/batteries/NONEXISTENT/dc", json={"watts": 100})
    assert resp.status == 404


# -- /auto -----------------------------------------------------------------


async def test_set_auto(sim_client):
    client, _, _, load_model = sim_client
    resp = await client.post("/auto", json={"enabled": True})
    assert resp.status == 200
    assert load_model.auto_mode is True


# -- /shutdown -------------------------------------------------------------


async def test_shutdown(sim_client):
    client, sim, _, _ = sim_client
    resp = await client.post("/shutdown")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "shutting_down"
    assert sim._shutdown_event.is_set()


# -- _build_status ---------------------------------------------------------


def test_build_status_structure():
    battery = _make_battery()
    load_model = _make_load_model()
    sim = PowermeterSimulator([battery], load_model)
    status = sim._build_status()
    assert "grid" in status
    assert "total" in status["grid"]
    assert "batteries" in status
    assert "loads" in status
    assert "solar" in status


# -- _find_battery ---------------------------------------------------------


def test_find_battery_found():
    battery = _make_battery(mac="AABBCCDDEE01")
    sim = PowermeterSimulator([battery], _make_load_model())
    assert sim._find_battery("AABBCCDDEE01") is battery


def test_find_battery_not_found():
    battery = _make_battery(mac="AABBCCDDEE01")
    sim = PowermeterSimulator([battery], _make_load_model())
    assert sim._find_battery("NONEXISTENT") is None
