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

// ── scroll effects (landing only) ──
// Kept to what helps you find your place: the nav marks the section you're
// reading, the wiring diagram draws its arrows in order so the direction of
// flow reads at a glance, and sections fade in once. All of it is skipped
// under prefers-reduced-motion, and nothing starts hidden without this script.
const navLinks = new Map<string, HTMLAnchorElement>();
document.querySelectorAll<HTMLAnchorElement>('.nav-links a[href^="#"]').forEach((a) => {
  navLinks.set(a.getAttribute("href")!.slice(1), a);
});
const spySections = [...document.querySelectorAll<HTMLElement>("main section[id]")];
if (navLinks.size && spySections.length) {
  let current: HTMLAnchorElement | undefined;
  let queued = false;
  const update = () => {
    queued = false;
    // The section crossing a line a third of the way down the viewport.
    const line = window.innerHeight / 3;
    const section = spySections.find((s) => {
      const r = s.getBoundingClientRect();
      return r.top <= line && r.bottom > line;
    });
    const link = section ? navLinks.get(section.id) : undefined;
    if (link === current) return;
    current?.classList.remove("active");
    current?.removeAttribute("aria-current");
    link?.classList.add("active");
    link?.setAttribute("aria-current", "location");
    current = link;
  };
  const onScroll = () => {
    if (!queued) {
      queued = true;
      requestAnimationFrame(update);
    }
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll, { passive: true });
  update();
}

const motionOk = window.matchMedia("(prefers-reduced-motion: no-preference)").matches;
if (motionOk && "IntersectionObserver" in window) {
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        entry.target.classList.add("in");
        observer.unobserve(entry.target);
      }
    },
    { rootMargin: "0px 0px -12% 0px" },
  );
  // Anything already on screen stays as it is; only what's below the fold waits.
  const belowFold = (el: Element) => el.getBoundingClientRect().top > window.innerHeight;
  const watch = (el: HTMLElement, cls: string) => {
    if (!belowFold(el)) return;
    el.classList.add(cls);
    observer.observe(el);
  };
  document.querySelectorAll<HTMLElement>(".band .rail-head, .band .rail-body, .band .shot").forEach((el) => watch(el, "reveal"));
  document.querySelectorAll<HTMLElement>(".band .spec > div").forEach((el, i) => {
    el.style.transitionDelay = `${(i % 2) * 90}ms`;
    watch(el, "reveal");
  });
  document.querySelectorAll<HTMLElement>(".wiring").forEach((el) => watch(el, "draw"));
}

// ── FAQ: slide answers open and shut ──
// <details> toggles instantly on its own; this animates the height between
// the closed and open sizes. Without the script, or under reduced motion, the
// native toggle is left alone.
if (motionOk) {
  document.querySelectorAll<HTMLDetailsElement>(".faq details").forEach((details) => {
    const summary = details.querySelector("summary");
    if (!summary) return;
    let running: Animation | undefined;
    const slide = (from: number, to: number, done?: () => void) => {
      details.style.overflow = "hidden";
      running = details.animate(
        { height: [`${from}px`, `${to}px`] },
        { duration: 220, easing: "cubic-bezier(0.2, 0, 0, 1)" },
      );
      running.onfinish = () => {
        running = undefined;
        details.style.overflow = "";
        done?.();
      };
    };
    summary.addEventListener("click", (e) => {
      e.preventDefault();
      // Start from wherever a running slide has got to, then measure the
      // real sizes with it out of the way.
      const from = details.offsetHeight;
      running?.cancel();
      details.style.overflow = "";
      if (details.open && !details.classList.contains("closing")) {
        details.classList.add("closing");
        const borders = details.offsetHeight - details.clientHeight;
        slide(from, summary.offsetHeight + borders, () => {
          details.open = false;
          details.classList.remove("closing");
        });
      } else {
        details.classList.remove("closing");
        details.open = true;
        slide(from, details.offsetHeight);
      }
    });
  });
}
