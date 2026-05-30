// links.ts — every GitHub URL the site uses, derived from a single ref that the
// build injects (GH_REF -> __GH_REF__). This replaces the old deploy-time sed:
//   main build    -> links to .../tree/main, .../blob/main/, @main
//   develop build -> develop (also the default for local/tests)
//   PR preview    -> the PR's head branch
//
// __GH_REF__ is replaced at build time by esbuild's `define`. The `typeof`
// guard keeps it safe when running un-bundled (tsx tests), where it's undefined.
declare const __GH_REF__: string;

export const ghRef: string = typeof __GH_REF__ !== "undefined" ? __GH_REF__ : "develop";

const REPO = "https://github.com/tomquist/astrameter";

/** Repo home at the current ref. */
export function ghRepo(): string {
  return `${REPO}/tree/${ghRef}`;
}

/** A README section (renders the ref's README with the heading anchor). */
export function ghReadme(anchor?: string): string {
  return `${REPO}/tree/${ghRef}${anchor ? `#${anchor}` : ""}`;
}

/** A file in the repo at the current ref. `pathWithAnchor` may include `#frag`. */
export function ghDoc(pathWithAnchor: string): string {
  return `${REPO}/blob/${ghRef}/${pathWithAnchor}`;
}

/** Issues are not branch-specific. */
export function ghIssues(): string {
  return `${REPO}/issues`;
}

/** The ESPHome external_components source ref for generated configs. */
export function esphomeSource(): string {
  return `github://tomquist/astrameter@${ghRef}`;
}

/** Resolve a `data-gh` attribute value (used by site.ts on static links). */
export function resolveGh(spec: string): string {
  if (spec === "repo") return ghRepo();
  if (spec === "issues") return ghIssues();
  if (spec === "readme") return ghReadme();
  if (spec.startsWith("readme#")) return ghReadme(spec.slice("readme#".length));
  if (spec.startsWith("doc:")) return ghDoc(spec.slice("doc:".length));
  return ghRepo();
}
