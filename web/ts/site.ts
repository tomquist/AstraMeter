// site.ts — light enhancements shared by all pages: mobile nav, nav shadow on
// scroll, resolving data-gh links to the deployed GitHub ref, and (on the
// landing page) rendering the feature cards + supported-power-meter grid from
// the same schema the generator uses, so marketing copy can't drift from the
// actual capabilities.
import { POWERMETERS } from "./schema.js";
import { resolveGh } from "./links.js";

// ── data-gh links → ref-correct GitHub URLs (set at runtime from the build ref) ──
document.querySelectorAll<HTMLAnchorElement>("a[data-gh]").forEach((a) => {
  const spec = a.dataset.gh;
  if (spec) a.href = resolveGh(spec);
});

// ── mobile nav ──
const toggle = document.getElementById("nav-toggle");
const links = document.getElementById("nav-links");
if (toggle && links) {
  toggle.addEventListener("click", () => {
    const open = links.classList.toggle("open");
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  });
  links.addEventListener("click", (e) => {
    if ((e.target as HTMLElement).tagName === "A") {
      links.classList.remove("open");
      toggle.setAttribute("aria-expanded", "false");
    }
  });
}

// ── nav shadow on scroll ──
const nav = document.getElementById("nav");
if (nav) {
  const onScroll = () => nav.classList.toggle("scrolled", window.scrollY > 8);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
}

// ── feature cards (landing only) ──
const FEATURES: [string, string, string][] = [
  ["⚖️", "Multi-battery load balancing", "Split the load across several Marstek batteries with fair distribution, saturation handling, and efficiency rotation."],
  ["📊", "Real-time monitoring", "Optional MQTT Insights publishes grid power, per-battery targets and topology to Home Assistant via auto-discovery."],
  ["🔌", "Reads 18+ meter sources", "Shelly, Home Assistant, MQTT, Modbus, SML, HomeWizard, Enphase, SMA and more — use what you already own."],
  ["🧰", "Runs anywhere", "Home Assistant add-on, Docker, direct install, or standalone on an ESP32 via ESPHome."],
  ["🎛️", "Advanced signal conditioning", "EMA smoothing, deadband, Hampel outlier rejection and an optional PID controller for rock-steady control."],
  ["🔱", "Three-phase ready", "Per-phase readings and calibration (offset / multiplier) across L1 / L2 / L3."],
  ["🆓", "Free & open source", "GPL-3.0 licensed. The config generator runs entirely in your browser — nothing is uploaded."],
  ["🪄", "Beginner-friendly setup", "A guided config generator writes the file for you, with every option explained."],
];
const featuresGrid = document.getElementById("features-grid");
if (featuresGrid) {
  for (const [icon, title, body] of FEATURES) {
    const card = document.createElement("div");
    card.className = "feature";
    card.innerHTML = `<div class="feature-icon">${icon}</div><h3>${title}</h3><p>${body}</p>`;
    featuresGrid.appendChild(card);
  }
}

// ── power-meter grid (landing only) ──
const TIER_LABEL: Record<string, string> = {
  native: "ESP32 native",
  generic: "ESP32 via HTTP",
  alternate: "ESP32 alt.",
  unsupported: "Python only",
};
const pmGrid = document.getElementById("pm-grid");
if (pmGrid) {
  for (const pm of POWERMETERS) {
    const tier = (pm.esphome && pm.esphome.tier) || "unsupported";
    const tierCls = tier === "unsupported" ? "bad" : tier === "alternate" ? "warn" : "ok";
    const item = document.createElement("a");
    item.className = "pm-item";
    item.href = "generator.html";
    item.innerHTML = `<span class="pm-name">${pm.label}</span><span class="badge badge-${tierCls}">${TIER_LABEL[tier]}</span>`;
    pmGrid.appendChild(item);
  }
  const count = document.getElementById("meter-count");
  if (count) count.textContent = String(POWERMETERS.length);
}
