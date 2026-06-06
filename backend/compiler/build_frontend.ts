import fs from "node:fs";
import path from "node:path";
import { build } from "esbuild";

const projectRoot = path.resolve(import.meta.dirname, "..", "..");
const frontendDir = path.join(projectRoot, "frontend");
const staticDir = path.join(projectRoot, "backend", "static");
const assetsDir = path.join(staticDir, "assets");

fs.rmSync(staticDir, { recursive: true, force: true });
fs.mkdirSync(assetsDir, { recursive: true });

await build({
  entryPoints: [path.join(frontendDir, "src", "app.ts")],
  bundle: true,
  outfile: path.join(assetsDir, "app.js"),
  format: "esm",
  target: "es2022",
  minify: process.env.NODE_ENV === "production",
  sourcemap: process.env.NODE_ENV !== "production",
});

fs.copyFileSync(path.join(frontendDir, "index.html"), path.join(staticDir, "index.html"));
fs.copyFileSync(path.join(frontendDir, "src", "styles.css"), path.join(assetsDir, "styles.css"));

console.log(`Built frontend into ${path.relative(projectRoot, staticDir)}`);
