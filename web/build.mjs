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
