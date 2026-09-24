// build.mjs — assemble the publishable site into web/dist/.
// Copies the static files (HTML/CSS/assets) and bundles the TypeScript entry
// points with esbuild. The GitHub ref to link to is injected at build time via
// the GH_REF env var (set per branch by the deploy workflows); it defaults to
// "develop" for local builds. This is what replaces the old stamp-ref.sh.
import { build } from "esbuild";
import { cp, rm, mkdir, readFile, writeFile } from "node:fs/promises";

const ref = process.env.GH_REF || "develop";
const outdir = "dist";

// Only the production build (main) is indexable. develop staging and PR previews
// share the custom domain, so they carry a per-page noindex to keep them out of
// search results — a real <meta robots> (not just robots.txt, which blocks
// crawling and so can never let a crawler see a noindex) is the reliable signal.
const noindex = ref !== "main";

await rm(outdir, { recursive: true, force: true });
await mkdir(`${outdir}/js`, { recursive: true });

// Static assets, copied verbatim.
for (const item of ["css", "assets", "CNAME", "robots.txt"]) {
  await cp(item, `${outdir}/${item}`, { recursive: true });
}

// IBM Plex Sans + Mono, self-hosted so the site makes no third-party requests.
// Both are SIL OFL 1.1 with no Reserved Font Name; the licence must travel with
// the font files, so each package's LICENSE is published next to them. Latin
// subsets only; anything else falls back to the system stack in css/styles.css.
await mkdir(`${outdir}/fonts`, { recursive: true });
await cp("node_modules/@fontsource-variable/ibm-plex-sans/LICENSE", `${outdir}/fonts/IBM-Plex-Sans-OFL.txt`);
await cp("node_modules/@fontsource/ibm-plex-mono/LICENSE", `${outdir}/fonts/IBM-Plex-Mono-OFL.txt`);
for (const [pkg, file] of [
  ["@fontsource-variable/ibm-plex-sans", "ibm-plex-sans-latin-wght-normal.woff2"],
  ["@fontsource-variable/ibm-plex-sans", "ibm-plex-sans-latin-ext-wght-normal.woff2"],
  ["@fontsource/ibm-plex-mono", "ibm-plex-mono-latin-400-normal.woff2"],
  ["@fontsource/ibm-plex-mono", "ibm-plex-mono-latin-500-normal.woff2"],
]) {
  await cp(`node_modules/${pkg}/files/${file}`, `${outdir}/fonts/${file}`);
}

// Dashboard screenshots live with the docs that also embed them (one copy,
// referenced by both) and are published under assets/ for the landing page.
// Regenerate with `npm run screenshots`; see web/tools/screenshots.ts.
await cp("../docs/images", `${outdir}/assets/screenshots`, { recursive: true });

// HTML pages: inject a noindex meta into non-production builds.
for (const item of ["index.html", "generator.html"]) {
  let html = await readFile(item, "utf8");
  if (noindex) {
    html = html.replace(
      "</head>",
      '  <meta name="robots" content="noindex" />\n  </head>',
    );
  }
  await writeFile(`${outdir}/${item}`, html);
}

await build({
  entryPoints: ["ts/app.ts", "ts/site.ts"],
  outdir: `${outdir}/js`,
  bundle: true,
  format: "esm",
  target: "es2020",
  minify: true,
  legalComments: "none",
  define: { __GH_REF__: JSON.stringify(ref) },
});

console.log(`Built ${outdir}/ (GitHub ref: ${ref}${noindex ? ", noindex" : ""})`);
