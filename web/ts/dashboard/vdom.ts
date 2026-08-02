// A ~90-line virtual DOM: enough to re-render a live page every second
// without losing focus, scroll position or a half-typed input, and small
// enough to stay inside the bundle budget that keeps an ESP32 in reach.
//
// Two backends over one tree:
//   patch()          — browser, writes through textContent/setAttribute
//   renderToString() — Node, so views are testable with plain `tsx` and no
//                      jsdom, matching how the rest of web/ts is tested.
//
// Neither backend ever touches innerHTML, so backend data cannot become
// markup. renderToString() escapes through the same escapeHtml() the browser
// path never needs.

export type VChild = VNode | string | number | null | undefined | false;

export interface VNode {
  tag: string;
  props: Record<string, any>;
  children: VChild[];
}

export function h(
  tag: string,
  props: Record<string, any> | null = null,
  ...children: (VChild | VChild[])[]
): VNode {
  const flat: VChild[] = [];
  for (const child of children) {
    if (Array.isArray(child)) flat.push(...child);
    else flat.push(child);
  }
  const p = props || {};
  return { tag, props: p, children: flat };
}

const VOID_TAGS = new Set(["br", "hr", "img", "input", "meta", "link"]);

export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function isEventProp(key: string): boolean {
  return key.startsWith("on") && key.length > 2;
}

export function renderToString(node: VChild): string {
  if (node == null || node === false) return "";
  if (typeof node === "string" || typeof node === "number") {
    return escapeHtml(String(node));
  }
  const attrs: string[] = [];
  for (const [key, value] of Object.entries(node.props)) {
    if (key === "key" || isEventProp(key) || value == null || value === false) {
      continue;
    }
    if (value === true) attrs.push(escapeHtml(key));
    else attrs.push(`${escapeHtml(key)}="${escapeHtml(String(value))}"`);
  }
  const open = `<${node.tag}${attrs.length ? " " + attrs.join(" ") : ""}>`;
  if (VOID_TAGS.has(node.tag)) return open;
  return `${open}${node.children.map(renderToString).join("")}</${node.tag}>`;
}

// -- browser backend --------------------------------------------------

// Attributes the *user* owns once the element exists: the browser writes
// `open` when a <details> is toggled, so a re-render must neither remove it
// nor force it back. Treated as a create-time default only — otherwise a
// 1 Hz poll would slam every disclosure shut (or hold it open) under the
// user's cursor.
const UNCONTROLLED = new Set(["open"]);

function setProp(
  el: HTMLElement,
  key: string,
  value: any,
  creating = false,
): void {
  // Reconciliation is by index, not by key (see patch); a stray `key`
  // prop is ignored rather than written to the DOM.
  if (key === "key") return;
  if (UNCONTROLLED.has(key) && !creating) return;
  if (isEventProp(key)) {
    const type = key.slice(2).toLowerCase();
    const prev = (el as any)["__" + key];
    if (prev) el.removeEventListener(type, prev);
    if (typeof value === "function") {
      el.addEventListener(type, value);
      (el as any)["__" + key] = value;
    }
    return;
  }
  if (key === "value") {
    // Never clobber what the user is typing.
    const input = el as HTMLInputElement;
    if (document.activeElement !== input && input.value !== String(value)) {
      input.value = value == null ? "" : String(value);
    }
    return;
  }
  if (key === "checked") {
    (el as HTMLInputElement).checked = Boolean(value);
    return;
  }
  if (value == null || value === false) el.removeAttribute(key);
  else if (value === true) el.setAttribute(key, "");
  else if (el.getAttribute(key) !== String(value)) {
    el.setAttribute(key, String(value));
  }
}

function create(node: VChild): Node {
  if (node == null || node === false) return document.createComment("");
  if (typeof node === "string" || typeof node === "number") {
    return document.createTextNode(String(node));
  }
  const el = document.createElement(node.tag);
  for (const [key, value] of Object.entries(node.props)) {
    setProp(el, key, value, true);
  }
  for (const child of node.children) el.appendChild(create(child));
  return el;
}

/**
 * Reconcile `parent`'s children against `next`, reusing nodes in place.
 *
 * Absent children keep their slot as a comment placeholder rather than being
 * filtered out. Dropping them would renumber every following sibling the
 * moment a conditional control appeared, so this loop would compare a slider
 * against a text box, replace it, and destroy the element the user was
 * dragging — along with its focus and its scroll position.
 */
export function patch(parent: HTMLElement, next: VChild[]): void {
  for (let i = 0; i < next.length; i++) {
    const existing = parent.childNodes[i];
    const desired = next[i];
    if (!existing) {
      parent.appendChild(create(desired));
      continue;
    }
    patchNode(parent, existing, desired);
  }
  while (parent.childNodes.length > next.length) {
    parent.removeChild(parent.lastChild!);
  }
}

function patchNode(
  parent: HTMLElement,
  existing: ChildNode,
  desired: VChild,
): void {
  if (desired == null || desired === false) {
    // Hold the slot so later siblings keep their index.
    if (existing.nodeType !== Node.COMMENT_NODE) {
      parent.replaceChild(document.createComment(""), existing);
    }
    return;
  }
  const isText = typeof desired === "string" || typeof desired === "number";
  if (isText) {
    if (existing.nodeType === Node.TEXT_NODE) {
      const text = String(desired);
      if (existing.textContent !== text) existing.textContent = text;
    } else {
      parent.replaceChild(create(desired), existing);
    }
    return;
  }
  const vnode = desired as VNode;
  if (
    existing.nodeType !== Node.ELEMENT_NODE ||
    (existing as HTMLElement).tagName.toLowerCase() !== vnode.tag
  ) {
    parent.replaceChild(create(vnode), existing);
    return;
  }
  const el = existing as HTMLElement;
  const seen = new Set(Object.keys(vnode.props));
  for (const attr of Array.from(el.attributes)) {
    if (!seen.has(attr.name) && !UNCONTROLLED.has(attr.name)) {
      el.removeAttribute(attr.name);
    }
  }
  for (const [key, value] of Object.entries(vnode.props)) setProp(el, key, value);
  patch(el, vnode.children);
}
