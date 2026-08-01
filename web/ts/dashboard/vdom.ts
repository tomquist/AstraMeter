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
  key?: string;
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
  return { tag, props: p, children: flat, key: p.key };
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

function setProp(el: HTMLElement, key: string, value: any): void {
  if (key === "key") return;
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
  for (const [key, value] of Object.entries(node.props)) setProp(el, key, value);
  for (const child of node.children) el.appendChild(create(child));
  return el;
}

/** Reconcile `parent`'s children against `next`, reusing nodes in place. */
export function patch(parent: HTMLElement, next: VChild[]): void {
  const nodes = next.filter((n) => n != null && n !== false) as VChild[];
  for (let i = 0; i < nodes.length; i++) {
    const existing = parent.childNodes[i];
    const desired = nodes[i];
    if (!existing) {
      parent.appendChild(create(desired));
      continue;
    }
    patchNode(parent, existing, desired, i);
  }
  while (parent.childNodes.length > nodes.length) {
    parent.removeChild(parent.lastChild!);
  }
}

function patchNode(
  parent: HTMLElement,
  existing: ChildNode,
  desired: VChild,
  index: number,
): void {
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
    if (!seen.has(attr.name)) el.removeAttribute(attr.name);
  }
  for (const [key, value] of Object.entries(vnode.props)) setProp(el, key, value);
  patch(el, vnode.children);
  void index;
}
