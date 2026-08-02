// Boots the real stack a spec needs, and tears it down again.
//
// Nothing is mocked below the HTTP boundary: `astra-sim` speaks the actual
// CT002 UDP protocol to a real AstraMeter, which serves the committed
// dashboard bundle. The only stand-in is the Supervisor, because a real
// Home Assistant cannot be run in CI — and even that serves the repository's
// own `ha_addon/config.yaml`, so the guided form is rendered from the
// add-on's real option schema rather than a fixture.

import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO = resolve(HERE, "../..");

export const DASHBOARD_PORT = 52599;
export const SIM_HTTP_PORT = 8188;
export const SIM_CT_PORT = 12399;
export const SUPERVISOR_PORT = 8199;

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

function configIni(): string {
  return [
    "[GENERAL]",
    "DEVICE_TYPE = ct002",
    "SKIP_POWERMETER_TEST = True",
    "ENABLE_WEB_SERVER = True",
    `WEB_SERVER_PORT = ${DASHBOARD_PORT}`,
    "DASHBOARD_ENABLED = True",
    "DASHBOARD_ALLOW_WRITE = True",
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
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await probe().catch(() => false)) return;
    await new Promise((r) => setTimeout(r, 400));
  }
  throw new Error(`timed out waiting for ${what}`);
}

const ok = (url: string) => async () => {
  const r = await fetch(url, { signal: AbortSignal.timeout(2000) });
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
}

export async function startStack(options: StartOptions = {}): Promise<Stack> {
  const dir = mkdtempSync(join(tmpdir(), "astrameter-e2e-"));
  const configPath = join(dir, "config.ini");
  writeFileSync(configPath, configIni());
  writeFileSync(join(dir, "sim.json"), JSON.stringify(SIM_CONFIG, null, 2));

  const children: ChildProcess[] = [];
  const spawnChild = (cmd: string, args: string[], env: NodeJS.ProcessEnv = {}) => {
    const child = spawn(cmd, args, {
      cwd: REPO,
      env: { ...process.env, ...env },
      stdio: "ignore",
      detached: true,
    });
    children.push(child);
    return child;
  };

  if (options.homeAssistant) {
    spawnChild("node", [join(HERE, "fake-supervisor.mjs")], {
      FAKE_SUPERVISOR_PORT: String(SUPERVISOR_PORT),
      FAKE_SUPERVISOR_STATE: join(dir, "addon-options.json"),
      FAKE_SUPERVISOR_CONFIG_DIR: dir,
    });
    await waitFor(
      ok(`http://127.0.0.1:${SUPERVISOR_PORT}/addons/self/info`),
      "the stand-in Supervisor",
      20_000,
    );
  }

  spawnChild("uv", ["run", "astra-sim", "run", "--no-tui", "-c", join(dir, "sim.json")]);
  await waitFor(ok(`http://127.0.0.1:${SIM_HTTP_PORT}/status`), "the simulator");

  if (options.homeAssistant) {
    // What the Supervisor would have written to /data/options.json. The
    // service reads it through the real `--addon` path: the dashboard
    // settings, the add-on slug and the config mode all come from here, not
    // from test-only environment variables.
    writeFileSync(
      join(dir, "options.json"),
      JSON.stringify({
        dashboard: true,
        dashboard_allow_write: true,
        // Requests come from 127.0.0.1, not the ingress peer, so the gate
        // has to be opened explicitly — the same opt-in a LAN user makes.
        dashboard_direct_access: true,
        log_level: "warning",
      }),
    );
  }

  spawnChild(
    "uv",
    [
      "run",
      "astrameter",
      // The add-on options cannot express "read the simulator over HTTP on
      // this port", so the running config stays the hand-written one while
      // everything else goes through the add-on path for real.
      ...(options.homeAssistant ? ["--addon"] : []),
      "-c",
      configPath,
      "--loglevel",
      "warning",
    ],
    {
      ASTRAMETER_DASHBOARD_DIRECT_ACCESS: "1",
      ...(options.homeAssistant
        ? {
            SUPERVISOR_TOKEN: "e2e-token",
            ASTRAMETER_SUPERVISOR_URL: `http://127.0.0.1:${SUPERVISOR_PORT}`,
            ASTRAMETER_ADDON_OPTIONS: join(dir, "options.json"),
            // Keep the mode switch's materialized file inside the test dir.
            ASTRAMETER_ADDON_CONFIG_DIR: dir,
            ASTRAMETER_ADDON_GENERATED_CONFIG: join(dir, "generated.ini"),
          }
        : {}),
    },
  );
  await waitFor(ok(`${BASE_URL}health`), "AstraMeter");
  // The page is only meaningful once batteries have actually reported.
  await waitFor(async () => (await reportingBatteries()) >= 2, "battery reports");

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

export function readIni(stack: Stack): string {
  return readFileSync(stack.configPath, "utf8");
}

export function readAddonOptions(stack: Stack): Record<string, unknown> {
  return JSON.parse(readFileSync(join(stack.dir, "addon-options.json"), "utf8"));
}
