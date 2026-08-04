// Regenerate the dashboard screenshots used by the docs and the website.
//
//   cd web && npm run screenshots
//
// Nothing here is staged or mocked. It boots the same stack the browser tests
// use — the battery simulator speaking real CT002 UDP to a real AstraMeter,
// serving the committed dashboard bundle — against a larger house than the
// specs run: three batteries across three phases, five switchable appliances
// and solar. So the numbers in the images are numbers the balancer actually
// produced, and they move when the UI does.
//
// The trend lines are the reason this takes a couple of minutes rather than
// seconds. They are built in the browser from polls the page has already made
// (see ts/dashboard/history.ts), so there is no way to fast-forward them: the
// page has to sit there and watch the house for a while. The script waits for
// real samples rather than sleeping a fixed time, so a slow machine gets the
// same picture, just later.
//
// Both themes come out of one page session on purpose. Flipping `data-theme`
// without reloading keeps the recorded history, so the light and dark variants
// of a shot show the identical moment — which is what lets the docs offer them
// as one image that follows the reader's theme.

import { mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { type Browser, chromium, type Page } from "@playwright/test";
import {
  BASE_URL,
  REPO,
  SIM_HTTP_PORT,
  SIM_CONFIG,
  startStack,
  statusSnapshot,
  type SimConfig,
} from "../e2e/stack.js";

/** Tabs worth showing, in the order a reader meets them. */
const TABS = ["overview", "batteries", "sources", "diagnostics"] as const;
type Tab = (typeof TABS)[number];

const THEMES = ["light", "dark"] as const;
type Theme = (typeof THEMES)[number];

/**
 * A fuller house than the specs run against.
 *
 * Three batteries on three phases is the configuration AstraMeter exists for —
 * a single battery needs no balancing — and the appliances are sized so the
 * fleet is visibly working rather than idling near zero. `auto_mode` switches
 * them and moves the sun on its own, which is what gives the trend lines
 * something to draw.
 */
const SHOWCASE: SimConfig = {
  ct: SIM_CONFIG.ct,
  http: SIM_CONFIG.http,
  powermeter: {
    // Always-on demand — fridge, router, standby — big enough that there is
    // real work to share even with every appliance off. Too small a base and
    // the fleet drops under MIN_EFFICIENT_POWER, concentrates onto one
    // battery and parks the other two at 0 W: correct behaviour, but it
    // documents the efficiency rule rather than the balancing.
    base_load: [300, 260, 240],
    // Meter jitter, not a disturbance. This is re-rolled every read, so the
    // balancer cannot cancel it — whatever is set here is a floor under how
    // close to zero the grid can be held, and at 25 W/phase that floor was
    // ~±75 W on the total. The dashboard then showed a house AstraMeter was
    // visibly failing to zero out.
    base_noise: 6,
    // Sized so the fleet can still cover the house with everything on. A
    // bigger house is not a better advert: it just parks the grid bar at the
    // end of its scale, which is AstraMeter saturated, not AstraMeter working.
    loads: [
      { name: "Oven", power: 1800, phase: "A" },
      { name: "Kettle", power: 1200, phase: "C" },
      { name: "Washing machine", power: 650, phase: "B" },
      { name: "Heat pump", power: 900, phase: "B" },
      { name: "Dishwasher", power: 1100, phase: "C" },
    ],
    // Under the base load plus a typical appliance, so a sunny moment swings
    // the fleet into charging without erasing the house entirely. The capture
    // gate waits those moments out, since the captions describe the fleet
    // supplying the house.
    solar_max: 3000,
    solar_phases: ["A", "B", "C"],
  },
  // A mixed fleet, as most people's is — but sized so that together they can
  // still cover the house at its worst (~6.5 kW against 6.6 kW of inverter).
  // A fleet that saturates has nothing left to steer with, and every shot
  // taken while it is pinned shows the grid bar stuck off to one side.
  batteries: [
    { mac: "02B250000001", phase: "A", max_charge_power: 2500, max_discharge_power: 2500, capacity_wh: 5120, initial_soc: 0.72 },
    { mac: "02B250000002", phase: "B", max_charge_power: 1600, max_discharge_power: 1600, capacity_wh: 2560, initial_soc: 0.54 },
    { mac: "02B250000003", phase: "C", max_charge_power: 2500, max_discharge_power: 2500, capacity_wh: 5120, initial_soc: 0.38 },
  ],
  auto_mode: true,
  // Longer than the loop takes to converge. The steering evaluation puts
  // settling at ~35 s mean and ~62 s p95, so a house that switched every
  // 10-20 s was never settled at all — it was permanently chasing, and the
  // grid sat 70-140 W away from zero in every shot. Events this far apart
  // leave the trend lines a visible step *and* the recovery after it.
  auto_interval: [45, 95],
  log_interval: 600,
} as SimConfig;

interface Options {
  out: string;
  stackDir: string;
  width: number;
  height: number;
  scale: number;
  minPoints: number;
  warmupMs: number;
  settleW: number;
  workingW: number;
  tabs: Tab[];
  themes: Theme[];
}

function parseArgs(argv: string[]): Options {
  const opts: Options = {
    out: join(REPO, "docs", "images"),
    // The Diagnostics tab prints this path, so it is part of the picture: a
    // plain directory reads as an install, `astrameter-e2e-NcTJZb` reads as a
    // test rig. Override it to match wherever you actually run AstraMeter.
    stackDir: join(tmpdir(), "astrameter"),
    width: 1280,
    height: 900,
    // Retina: the docs render these at roughly half their pixel width.
    scale: 2,
    // ~3 minutes of watching at the 2 s poll. Enough to cover a couple of
    // the house's changes and the settling after each, which is what gives a
    // trend line its shape now that events are further apart.
    minPoints: 90,
    warmupMs: 420_000,
    // What "a good moment" means, in watts: the grid genuinely at zero — the
    // balancer's own settle band is ±25 W — and every battery visibly the
    // reason why, each discharging at least this much into the house. A loose
    // gate here is not a small sin: it admits shots of AstraMeter
    // mid-correction and presents them as AstraMeter working.
    settleW: 30,
    workingW: 150,
    tabs: [...TABS],
    themes: [...THEMES],
  };
  for (let i = 0; i < argv.length; i++) {
    const [flag, inline] = argv[i].split("=", 2);
    const value = () => inline ?? argv[++i];
    switch (flag) {
      case "--out": opts.out = value(); break;
      case "--stack-dir": opts.stackDir = value(); break;
      case "--width": opts.width = Number(value()); break;
      case "--height": opts.height = Number(value()); break;
      case "--scale": opts.scale = Number(value()); break;
      case "--points": opts.minPoints = Number(value()); break;
      case "--settle": opts.settleW = Number(value()); break;
      case "--working": opts.workingW = Number(value()); break;
      case "--warmup": opts.warmupMs = Number(value()) * 1000; break;
      case "--tabs": opts.tabs = value().split(",") as Tab[]; break;
      case "--themes": opts.themes = value().split(",") as Theme[]; break;
      case "--help":
        console.log(
          "usage: npm run screenshots -- [--out DIR] [--tabs a,b] [--themes light,dark]\n" +
            "                            [--warmup SECONDS] [--points N] [--width PX] [--scale N]\n" +
            "                            [--stack-dir DIR]",
        );
        process.exit(0);
      default:
        throw new Error(`unknown flag ${flag} (try --help)`);
    }
  }
  const badTab = opts.tabs.find((t) => !TABS.includes(t));
  if (badTab) throw new Error(`unknown tab "${badTab}" (have: ${TABS.join(", ")})`);
  const badTheme = opts.themes.find((t) => !THEMES.includes(t));
  if (badTheme) throw new Error(`unknown theme "${badTheme}" (have: ${THEMES.join(", ")})`);
  return opts;
}

const log = (message: string) => console.log(`• ${message}`);

/** Whether anything is listening there — used to refuse a dirty start. */
async function answers(url: string): Promise<boolean> {
  try {
    return (await fetch(url, { signal: AbortSignal.timeout(2000) })).ok;
  } catch {
    return false;
  }
}

/** How many points each trend line on the page currently has. */
function sparkPointCounts(page: Page): Promise<number[]> {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll(".spark-line")).map(
      (el) => (el.getAttribute("points") || "").split(" ").filter(Boolean).length,
    ),
  );
}

/**
 * Watch the house until every trend line has a shape to it.
 *
 * Waits on the samples themselves rather than on the clock: the page records
 * one per *changed* snapshot, so how long that takes depends on how busy the
 * simulated house is and how fast the machine runs it.
 */
async function warmUp(page: Page, opts: Options): Promise<void> {
  const deadline = Date.now() + opts.warmupMs;
  let last = -1;
  for (;;) {
    const counts = await sparkPointCounts(page);
    const lowest = counts.length ? Math.min(...counts) : 0;
    if (counts.length && lowest >= opts.minPoints) {
      log(`trend lines filled: ${counts.length} series, ${lowest}+ samples each`);
      return;
    }
    if (Date.now() > deadline) {
      // Not fatal. A shorter line still reads as a trend, and failing here
      // would throw away several minutes of warm-up over a cosmetic margin.
      console.warn(
        `! warm-up budget spent with ${lowest}/${opts.minPoints} samples — ` +
          "shooting anyway (raise --warmup for longer lines)",
      );
      return;
    }
    if (lowest !== last) {
      last = lowest;
      log(`warming up: ${lowest}/${opts.minPoints} samples`);
    }
    await page.waitForTimeout(2000);
  }
}

/**
 * The house total, read the way the page's headline reads it.
 *
 * Mirrors `gridTotal` in ts/dashboard/model.ts: the CT emulator's own figure
 * when it has one, the power source's otherwise. Kept to the primary path
 * because this only ever runs against the stack booted below, where a CT
 * emulator is always present.
 */
function gridWatts(snapshot: any): number | undefined {
  let total: number | undefined;
  for (const device of snapshot?.devices || []) {
    const grid = device.grid;
    if (!grid) continue;
    const phases = [grid.l1_w, grid.l2_w, grid.l3_w].filter((v: unknown) => v != null);
    const value =
      grid.grid_total_w ??
      (phases.length ? phases.reduce((a: number, b: number) => a + b, 0) : undefined);
    if (value != null) total = (total ?? 0) + value;
  }
  return total;
}

/** Every battery's reported power this moment. */
function batteryWatts(snapshot: any): number[] {
  return (snapshot?.devices || [])
    .flatMap((d: any) => d.consumers || [])
    .map((c: any) => c.reported_power_w)
    .filter((w: unknown): w is number => typeof w === "number");
}

/**
 * Hold until the picture is worth taking.
 *
 * A screenshot lands on whatever instant it lands on, and a house whose
 * appliances switch every few seconds spends some of those instants mid-step,
 * with the grid far from zero and the fleet still ramping. That moment is real
 * but it is not representative: it shows the balancer behind rather than the
 * balancer working. So wait for one where the grid is held near zero *and*
 * every battery is the reason — no staging, just a settled moment instead of
 * an arbitrary one.
 *
 * Every battery, not the busiest: under a light load AstraMeter deliberately
 * concentrates on fewer of them, and a shot of one battery working beside two
 * parked at 0 W documents the efficiency rule instead of the balancing.
 *
 * And every battery *discharging*, because that is what the captions claim.
 * A sunny moment with the fleet absorbing surplus is just as real, but it
 * would sit under alt text promising the opposite.
 */
/**
 * How long to wait for a settled moment before giving up.
 *
 * Generous because the sun moves: a stretch where solar covers the house
 * outright leaves the fleet charging, and the gate is waiting for it to be
 * supplying. That passes on its own, so waiting beats failing the run.
 */
const PATIENCE_MS = 300_000;

interface Moment {
  ok: boolean;
  grid: number | undefined;
  quietest: number;
}

/** Whether the grid is at zero and the whole fleet is holding it there. */
async function settledNow(opts: Options): Promise<Moment> {
  const snapshot = await statusSnapshot();
  const grid = gridWatts(snapshot);
  // What each battery is supplying to the house. `reported_power_w` is
  // positive discharging and negative charging (see the `charging` test in
  // ts/dashboard/view.ts) — the Overview rail shows the negation of it,
  // because that column is each battery's *effect on the grid*, so do not
  // take the sign from the screenshots.
  //
  // The direction matters: the docs caption both images with "three batteries
  // between them supply what the house is drawing", and a fleet soaking up
  // solar is equally real but illustrates the opposite.
  const supplying = batteryWatts(snapshot);
  const quietest = supplying.length ? Math.min(...supplying) : 0;
  const ok =
    grid != null && Math.abs(grid) <= opts.settleW && quietest >= opts.workingW;
  return { ok, grid, quietest };
}

async function waitForGoodMoment(opts: Options, label: string): Promise<void> {
  const deadline = Date.now() + PATIENCE_MS;
  for (;;) {
    const { ok, grid, quietest } = await settledNow(opts);
    if (ok) return;
    if (Date.now() > deadline) {
      // Refuse rather than shoot. A warning here is how the first pass came to
      // commit images reading +72 W under a caption claiming zero: it scrolls
      // past in an eight-minute run and the PNG lands anyway. If the house
      // cannot reach a settled moment, the scenario is wrong and the fix
      // belongs in SHOWCASE — not in a caption written around a bad shot.
      throw new Error(
        `${label}: no settled moment within ${PATIENCE_MS / 1000} s ` +
          `(grid ${grid?.toFixed(0) ?? "?"} W, quietest battery supplying ${quietest.toFixed(0)} W)`,
      );
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

/** Switch tabs the way a click does — no reload, so the history survives. */
async function openTab(page: Page, tab: Tab): Promise<void> {
  await page.evaluate((t) => {
    location.hash = `#/${t}`;
  }, tab);
  await page.locator(`.tab[aria-current="page"]`).waitFor();
  // The tab paints from the snapshot already in hand; give the poll that
  // follows a moment to land so the shot is of a settled page.
  await page.waitForTimeout(500);
}

/**
 * Shrink the window to what this tab actually draws.
 *
 * `fullPage` grows a short page to the viewport rather than cropping to it,
 * and the tabs differ in height by hundreds of pixels — so a fixed viewport
 * left the Overview shot half empty grey while the Batteries one was tight.
 * Measuring the content and resizing to it crops every tab the same way.
 */
async function fitToContent(page: Page, opts: Options): Promise<void> {
  const height = await page.evaluate(() => {
    const main = document.querySelector("main");
    if (!main) return document.documentElement.scrollHeight;
    // Bottom relative to the document, plus the page's own bottom padding, so
    // the last card does not end flush against the edge of the image.
    const rect = main.getBoundingClientRect();
    return Math.ceil(rect.bottom + window.scrollY + 20);
  });
  await page.setViewportSize({
    width: opts.width,
    // Floor only to keep a degenerate measurement from producing a sliver;
    // it must stay well under the shortest real tab or it puts back exactly
    // the band of empty background this function exists to remove.
    height: Math.max(360, Math.min(height, 2400)),
  });
  await page.waitForTimeout(200);
}

async function setTheme(page: Page, theme: Theme): Promise<void> {
  // Both halves matter: the attribute drives the dashboard's own palette, and
  // the media emulation drives what the browser paints around it (scrollbars,
  // form controls), which would otherwise stay light in a dark screenshot.
  await page.emulateMedia({ colorScheme: theme });
  await page.evaluate((t) => {
    document.documentElement.setAttribute("data-theme", t);
  }, theme);
  await page.waitForTimeout(200);
}

/** The poll the dashboard makes to refresh itself. */
const STATUS_ROUTE = "**/api/status*";

/**
 * Take both themes of one tab, on a page frozen at a settled moment.
 *
 * The gate reads the status API, but the image reads the DOM, and the page
 * polls on its own schedule — so without care a poll can repaint the page
 * between the check passing and the shutter, putting a state nobody checked
 * into the picture. Two things close that gap:
 *
 * Polls are answered `304 Not Modified` for the duration of the capture. That
 * is the same answer the server gives when nothing has changed, so the page
 * takes its normal "no new data" path and simply holds what it has — no error,
 * no offline banner, and nothing that can repaint under the shutter.
 *
 * The paint being frozen is bracketed by two passing checks, one either side
 * of the poll interval. For the frozen render to be unsettled, the grid would
 * have to leave the band and return inside those ~2 s, which is far quicker
 * than the loop can move (settling runs ~35 s, and the house changes every
 * 45-95 s) — so a paint between two settled checks is itself settled.
 *
 * The freeze is also what makes the pair identical: with the page held still,
 * the two themes are the same instant by construction rather than by luck.
 */
async function captureSettled(page: Page, opts: Options, tab: Tab): Promise<void> {
  for (let attempt = 1; ; attempt++) {
    await waitForGoodMoment(opts, tab);

    // Freeze *before* the confirming check, not after. `route` only intercepts
    // requests that start once it is installed, so a poll already in flight
    // when it goes on still delivers a real snapshot — and a paint arriving
    // after the last check is exactly the unchecked repaint this prevents.
    await page.route(STATUS_ROUTE, (route) => route.fulfill({ status: 304 }));
    try {
      // Long enough for that in-flight poll to land; every later one is 304.
      await page.waitForTimeout(2200);
      const { ok, grid, quietest } = await settledNow(opts);
      if (!ok) {
        // The house moved while the page was being frozen, so the paint under
        // the freeze is not one that passed. Thaw and wait for a fresh moment
        // rather than holding the freeze until it settles again: the page ages
        // while frozen, and a long hold puts a stale "Reading 45 s ago" into a
        // picture whose whole job is to look live.
        if (attempt >= 5) {
          throw new Error(
            `${tab}: could not hold a settled moment across the freeze ` +
              `(grid ${grid?.toFixed(0) ?? "?"} W, quietest battery supplying ${quietest.toFixed(0)} W)`,
          );
        }
        log(`${tab}: house moved while freezing, waiting for another moment`);
        continue;
      }

      // Bracket closed: the page's last real paint happened between this check
      // and the one above, and nothing can repaint after it.
      for (const theme of opts.themes) {
        await setTheme(page, theme);
        const file = join(opts.out, `dashboard-${tab}-${theme}.png`);
        await page.screenshot({ path: file });
        log(`wrote ${file}`);
      }
      return;
    } finally {
      await page.unroute(STATUS_ROUTE);
    }
  }
}

async function main(): Promise<void> {
  const opts = parseArgs(process.argv.slice(2));
  mkdirSync(opts.out, { recursive: true });

  // Nothing may already be on the dashboard port. The stack's readiness probe
  // is "something answers on 52500", which a leftover AstraMeter from an
  // earlier run satisfies perfectly: the one we start loses the bind and dies,
  // the probe passes against the corpse, and every shot then documents the
  // *previous* run's configuration. That failure is invisible — the page
  // renders and every value is plausible — so refuse to start instead.
  if (await answers(`${BASE_URL}health`)) {
    throw new Error(
      `something already answers on ${BASE_URL} — stop it first ` +
        "(a leftover AstraMeter would be screenshotted instead of this one)",
    );
  }
  // Same trap one layer down. startStack's simulator probe is "something
  // answers on /status", so a leftover simulator passes it too — and then the
  // house in the picture is the *previous* run's, with whatever loads and
  // batteries it was configured for, while the new simulator dies on the bind.
  const simStatus = `http://127.0.0.1:${SIM_HTTP_PORT}/status`;
  if (await answers(simStatus)) {
    throw new Error(
      `something already answers on ${simStatus} — stop it first ` +
        "(a leftover simulator would drive the house instead of this one)",
    );
  }

  log(`booting the stack (${SHOWCASE.batteries.length} batteries)…`);
  // No meter filters configured, so the shots show the raw signal. Adding
  // some would populate the Power source card's "Filters" row, but not
  // HAMPEL_WINDOW: on a house that steps this often it latches. Once a
  // *sustained* change moves the total past the threshold, the median it
  // writes back into its own window keeps every later sample looking like an
  // outlier, and the reading freezes at the pre-change value indefinitely —
  // which froze the control loop and flattened every trend line with it.
  const stack = await startStack({ sim: SHOWCASE, configDir: opts.stackDir });
  // Launch inside the block that stops the stack: the simulator and AstraMeter
  // are already running by now, so a failure here — a missing Chromium, most
  // likely — would otherwise leave them holding the dashboard port, and the
  // next run would refuse to start against the leftovers.
  let browser: Browser | undefined;

  try {
    browser = await chromium.launch({
      executablePath: process.env.ASTRAMETER_E2E_CHROMIUM || undefined,
    });
    const context = await browser.newContext({
      viewport: { width: opts.width, height: opts.height },
      deviceScaleFactor: opts.scale,
    });
    const page = await context.newPage();

    // Let the balancer catch up *before* the page starts recording. Its first
    // seconds are a ramp from zero to whatever the house is doing, and that
    // spike is several times any later movement — so it flattens every trend
    // line drawn after it into a straight edge along the bottom of the box.
    // The page keeps 150 samples, so the transient would sit in the picture
    // for five minutes. Waiting a few seconds here is cheaper.
    log("waiting for the balancer to settle…");
    await waitForGoodMoment(opts, "start-up");
    await page.goto(BASE_URL);

    // Sit on the batteries tab while warming up: it is the one carrying a
    // trend line per battery, so it is where the wait can be observed.
    await openTab(page, "batteries");
    await warmUp(page, opts);

    for (const tab of opts.tabs) {
      await openTab(page, tab);
      await fitToContent(page, opts);
      await captureSettled(page, opts, tab);
    }
  } finally {
    // Nested, not sequential: a rejecting browser.close() must not carry off
    // the stack.stop() with it, or the simulator and AstraMeter keep the
    // dashboard port and the next run refuses to start.
    try {
      await browser?.close();
    } finally {
      await stack.stop();
    }
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
