// site.ts — light enhancements shared by all pages: mobile nav, nav border on
// scroll, resolving data-gh links to the deployed GitHub ref, and (on the
// landing page) rendering the supported-power-meter list and counts from the
// same schema the generator uses, so the page can't drift from the actual
// capabilities.
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

// ── supported-meter list (landing only) ──
const ESP32_SUPPORT: Record<string, string> = {
  native: "ESP32",
  generic: "ESP32 via HTTP",
  alternate: "ESP32, other interface",
  unsupported: "Python only",
};
const pmList = document.getElementById("pm-grid");
if (pmList) {
  for (const pm of POWERMETERS) {
    const tier = (pm.esphome && pm.esphome.tier) || "unsupported";
    const support = pm.esphomeOnly ? "ESP32 only" : ESP32_SUPPORT[tier];
    // The status column already says "ESP32 only"; drop the label's own note.
    const name = pm.label.replace(/\s*\(ESPHome only\)$/, "");
    const item = document.createElement("li");
    item.innerHTML = `<span class="pm-name">${name}</span><span class="pm-esp pm-${tier}">${support}</span>`;
    pmList.appendChild(item);
  }
}
document.querySelectorAll("[data-meter-count]").forEach((el) => {
  el.textContent = String(POWERMETERS.length);
});
