// build.mjs — assemble the publishable site into web/dist/.
// Copies the static files (HTML/CSS/assets) and bundles the TypeScript entry
// points with esbuild. The GitHub ref to link to is injected at build time via
// the GH_REF env var (set per branch by the deploy workflows); it defaults to
// "develop" for local builds. This is what replaces the old stamp-ref.sh.
import { build } from "esbuild";
import { cp, rm, mkdir } from "node:fs/promises";

const ref = process.env.GH_REF || "develop";
const outdir = "dist";

await rm(outdir, { recursive: true, force: true });
await mkdir(`${outdir}/js`, { recursive: true });

// Static assets, copied verbatim.
for (const item of ["index.html", "generator.html", "css", "assets"]) {
  await cp(item, `${outdir}/${item}`, { recursive: true });
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

console.log(`Built ${outdir}/ (GitHub ref: ${ref})`);
