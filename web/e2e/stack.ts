// Boots the real stack a spec needs, and tears it down again.
//
// Nothing is mocked below the HTTP boundary: `astra-sim` speaks the actual
// CT002 UDP protocol to a real AstraMeter, which serves the committed
// dashboard bundle. The only stand-in is the Supervisor, because a real
// Home Assistant cannot be run in CI — and even that serves the repository's
// own `ha_addon/config.yaml`, so the guided form is rendered from the
// add-on's real option schema rather than a fixture.

import { spawn, type ChildProcess } from "node:child_process";
import { createSocket } from "node:dgram";
import { mkdtempSync, openSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO = resolve(HERE, "../..");

export const DASHBOARD_PORT = 52500;
export const SIM_HTTP_PORT = 8188;
export const SIM_CT_PORT = 12345;
export const SUPERVISOR_PORT = 8199;
export const SUPERVISOR_TOKEN = "e2e-token";

/**
 * The UDP port the Shelly EM Gen3 emulator listens on (main.py fixes it per
 * device type). `shellypro3em` — the default — would be closer to what most
 * users run, but its "old" firmware variant binds port 1010, which an
 * unprivileged CI runner cannot have. The emulator itself is the same class.
 */
export const SHELLY_UDP_PORT = 2222;

/**
 * The Home Assistant sensor the add-on reads, served from the simulator.
 * One signed whole-house sensor, which is what most installs have.
 */
export const PHASE_SENSORS = ["sensor.grid_power"];

export const BASE_URL = `http://127.0.0.1:${DASHBOARD_PORT}/`;

const SIM_CONFIG = {
  ct: { mac: "AABBCCDDEEFF", host: "127.0.0.1", port: SIM_CT_PORT },
  http: { host: "127.0.0.1", port: SIM_HTTP_PORT },
  powermeter: {
    base_load: [120, 80, 60],
    base_noise: 10,
    loads: [
      { name: "Kettle", power: 900, phase: "A" },
      { name: "Washing machine", power: 400, phase: "B" },
    ],
    solar_max: 4000,
    solar_phases: ["A", "B", "C"],
  },
  batteries: [
    { mac: "02B250000001", phase: "A", max_charge_power: 800, max_discharge_power: 800, capacity_wh: 2560, initial_soc: 0.6 },
    { mac: "02B250000002", phase: "B", max_charge_power: 800, max_discharge_power: 800, capacity_wh: 2560, initial_soc: 0.5 },
  ],
  auto_mode: false,
  log_interval: 600,
};

function configIni(shelly = false): string {
  if (shelly) {
    return [
      "[GENERAL]",
      "DEVICE_TYPE = shellyemg3",
      "SKIP_POWERMETER_TEST = True",
      "ENABLE_WEB_SERVER = True",
      `WEB_SERVER_PORT = ${DASHBOARD_PORT}`,
      "DASHBOARD_ENABLED = True",
      "DASHBOARD_ALLOW_WRITE = True",
      "",
      "[JSON_HTTP]",
      `URL = http://127.0.0.1:${SIM_HTTP_PORT}/power`,
      "JSON_PATHS = $.phase_a,$.phase_b,$.phase_c",
      "",
    ].join("\n");
  }
  return [
    "[GENERAL]",
    "DEVICE_TYPE = ct002",
    "SKIP_POWERMETER_TEST = True",
    "ENABLE_WEB_SERVER = True",
    `WEB_SERVER_PORT = ${DASHBOARD_PORT}`,
    "DASHBOARD_ENABLED = True",
    "DASHBOARD_ALLOW_WRITE = True",
    // No DASHBOARD_DIRECT_ACCESS: outside the add-on this is the whole
    // opt-in, and the browser suite is what proves it.
    "",
    "[CT002]",
    `UDP_PORT = ${SIM_CT_PORT}`,
    "ACTIVE_CONTROL = True",
    "FAIR_DISTRIBUTION = True",
    // Non-zero so efficiency rotation is on and its controls are published,
    // mirroring when the MQTT entity appears.
    "MIN_EFFICIENT_POWER = 100",
    "",
    "[JSON_HTTP]",
    `URL = http://127.0.0.1:${SIM_HTTP_PORT}/power`,
    "JSON_PATHS = $.phase_a,$.phase_b,$.phase_c",
    "",
  ].join("\n");
}

export interface Stack {
  dir: string;
  configPath: string;
  stop(): Promise<void>;
}

async function waitFor(
  probe: () => Promise<boolean>,
  what: string,
  timeoutMs = 90_000,
  logDir?: string,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await probe().catch(() => false)) return;
    await new Promise((r) => setTimeout(r, 400));
  }
  const where = logDir ? ` — child output is in ${logDir}` : "";
  throw new Error(`timed out waiting for ${what}${where}`);
}

const ok = (url: string, token?: string) => async () => {
  const r = await fetch(url, {
    signal: AbortSignal.timeout(2000),
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  });
  return r.ok;
};

/** Number of batteries the emulator has heard from. */
export async function reportingBatteries(): Promise<number> {
  const r = await fetch(`${BASE_URL}api/status`);
  const body = await r.json();
  return (body.devices?.[0]?.consumers ?? []).length;
}

export async function statusSnapshot(): Promise<any> {
  return (await fetch(`${BASE_URL}api/status`)).json();
}

export async function battery(consumerId: string): Promise<any> {
  const snap = await statusSnapshot();
  return (snap.devices?.[0]?.consumers ?? []).find(
    (c: any) => c.consumer_id === consumerId,
  );
}

export interface StartOptions {
  /** Run as a Home Assistant add-on against the stand-in Supervisor. */
  homeAssistant?: boolean;
  /**
   * Emulate a Shelly meter instead of a CT002.
   *
   * This is the other half of the device matrix: batteries poll the emulator
   * over UDP rather than being steered by it, so the page has no consumers,
   * no balancer and no controls to render. The simulator has no Shelly
   * client, so a spec drives the polling itself (see shelly.spec.ts).
   */
  shelly?: boolean;
}

export async function startStack(options: StartOptions = {}): Promise<Stack> {
  const dir = mkdtempSync(join(tmpdir(), "astrameter-e2e-"));
  const configPath = join(dir, "config.ini");
  writeFileSync(configPath, configIni(options.shelly));
  writeFileSync(join(dir, "sim.json"), JSON.stringify(SIM_CONFIG, null, 2));

  const children: ChildProcess[] = [];
  // Each child's output goes to a file in `dir`. Discarding it makes a CI
  // start-up timeout unexplainable: all the failure says is what was waited
  // for, never why the process it was waiting for did not come up.
  const spawnChild = (cmd: string, args: string[], env: NodeJS.ProcessEnv = {}) => {
    const log = openSync(join(dir, `${cmd}-${children.length}.log`), "a");
    const child = spawn(cmd, args, {
      cwd: REPO,
      env: { ...process.env, ...env },
      stdio: ["ignore", log, log],
      detached: true,
    });
    children.push(child);
    return child;
  };

  if (options.homeAssistant) {
    // What the Supervisor would have written to /data/options.json. The whole
    // add-on path then runs for real: the grid comes from Home Assistant
    // sensors, and the config mode, slug and dashboard settings are the ones
    // production computes.
    writeFileSync(
      join(dir, "options.json"),
      JSON.stringify({
        device_types: "ct002",
        power_input_alias: PHASE_SENSORS.join(","),
        wait_for_next_message: false,
        active_control: true,
        fair_distribution: true,
        // Non-zero so efficiency rotation is on and its controls appear.
        min_efficient_power: 100,
        // A stored credential, so the secret sentinel's round trip through
        // the browser is exercised against a real one.
        marstek_password: "super-secret-pw",
        // No `dashboard` option: the add-on always serves it.
        dashboard_allow_write: true,
        // Requests come from 127.0.0.1, not the ingress peer, so the gate
        // has to be opened explicitly — the same opt-in a LAN user makes.
        dashboard_direct_access: true,
        log_level: "warning",
      }),
    );

    // The same stand-in Supervisor the Python add-on tests use, so there is
    // one of them rather than two that can drift. It serves the repository's
    // real ha_addon/config.yaml, so the guided form is rendered from the
    // options Home Assistant would actually show, and it proxies the
    // simulator as Home Assistant power sensors — which is how the add-on
    // gets its readings, since `--addon` takes no config file.
    spawnChild("uv", [
      "run", "python", join(REPO, "tests", "_fake_supervisor.py"),
      "--host", "127.0.0.1",
      "--port", String(SUPERVISOR_PORT),
      "--token", SUPERVISOR_TOKEN,
      "--power-url", `http://127.0.0.1:${SIM_HTTP_PORT}/power`,
      "--phase-sensors", PHASE_SENSORS.join(","),
      "--options", join(dir, "options.json"),
    ]);
    await waitFor(
      ok(`http://127.0.0.1:${SUPERVISOR_PORT}/core/api/`, SUPERVISOR_TOKEN),
      "the stand-in Supervisor",
      30_000,
      dir,
    );
  }

  spawnChild("uv", ["run", "astra-sim", "run", "--no-tui", "-c", join(dir, "sim.json")]);
  await waitFor(
    ok(`http://127.0.0.1:${SIM_HTTP_PORT}/status`),
    "the simulator",
    90_000,
    dir,
  );

  spawnChild(
    "uv",
    options.homeAssistant
      // `--addon` takes its whole configuration from the add-on options and
      // the Supervisor; a config file is not part of that path.
      ? ["run", "astrameter", "--addon"]
      : ["run", "astrameter", "-c", configPath, "--loglevel", "warning"],
    {
      ...(options.homeAssistant
        ? {
            SUPERVISOR_TOKEN: SUPERVISOR_TOKEN,
            ASTRAMETER_SUPERVISOR_URL: `http://127.0.0.1:${SUPERVISOR_PORT}`,
            ASTRAMETER_ADDON_OPTIONS: join(dir, "options.json"),
            // Keep the mode switch's materialized file inside the test dir.
            ASTRAMETER_ADDON_CONFIG_DIR: dir,
          }
        : {}),
    },
  );
  await waitFor(ok(`${BASE_URL}health`), "AstraMeter", 90_000, dir);
  if (options.shelly) {
    // Nothing polls a Shelly emulator until a spec does, so the readiness
    // signal is the emulator itself being up and in the document.
    await waitFor(
      async () =>
        ((await statusSnapshot()).devices ?? []).some(
          (d: any) => d.kind === "shelly",
        ),
      "the Shelly emulator",
      90_000,
      dir,
    );
  } else {
    // The page is only meaningful once batteries have actually reported.
    await waitFor(
      async () => (await reportingBatteries()) >= 2,
      "battery reports",
      90_000,
      dir,
    );
  }

  return {
    dir,
    configPath,
    async stop() {
      for (const child of children) {
        try {
          // Killing the group also reaps the `uv run` wrapper's child.
          process.kill(-child.pid!, "SIGKILL");
        } catch {
          /* already gone */
        }
      }
      // Give the OS a moment to release the ports for the next spec file.
      await new Promise((r) => setTimeout(r, 1500));
    },
  };
}

/**
 * Poll the Shelly emulator the way a Marstek battery does, and return the
 * reading it answers with.
 *
 * A real datagram over the real socket, so the whole path a Shelly install
 * depends on runs: the emulator registers the caller as a battery, reads the
 * powermeter and replies. Resolves once the reply arrives — the caller can
 * then assert the dashboard reflects it.
 */
export async function pollShelly(timeoutMs = 5000): Promise<any> {
  const socket = createSocket("udp4");
  try {
    return await new Promise((resolve, reject) => {
      // Every exit goes through done(), so no path leaves the timer armed to
      // fire minutes later — and a malformed reply fails the test with its
      // parse error rather than escaping the handler as an uncaught throw.
      const timer = setTimeout(
        () => done(new Error("the Shelly emulator did not answer")),
        timeoutMs,
      );
      const done = (err: unknown, value?: any) => {
        clearTimeout(timer);
        if (err) reject(err);
        else resolve(value);
      };
      socket.on("error", done);
      socket.on("message", (message) => {
        try {
          done(null, JSON.parse(message.toString()));
        } catch (err) {
          done(err);
        }
      });
      socket.send(
        JSON.stringify({
          id: 1,
          src: "e2e-battery",
          method: "EM.GetStatus",
          params: { id: 0 },
        }),
        SHELLY_UDP_PORT,
        "127.0.0.1",
        (err) => err && done(err),
      );
    });
  } finally {
    socket.close();
  }
}

export function readIni(stack: Stack): string {
  return readFileSync(stack.configPath, "utf8");
}

export function readAddonOptions(stack: Stack): Record<string, unknown> {
  return JSON.parse(readFileSync(join(stack.dir, "options.json"), "utf8"));
}
