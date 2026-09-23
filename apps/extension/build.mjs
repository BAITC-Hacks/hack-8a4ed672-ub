// node apps/extension/build.mjs [--backend https://hattama.local:8443]
// Copies src/ and the shared audio client into dist/ (no bundler, no dependencies).
// --backend narrows optional host permissions to that single origin.
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const dist = join(here, "dist");
rmSync(dist, { recursive: true, force: true });
mkdirSync(join(dist, "lib"), { recursive: true });
cpSync(join(here, "src"), dist, { recursive: true });
for (const f of ["protocol.js", "uploader.js", "pcm-worklet.js"]) {
  cpSync(join(here, "..", "..", "packages", "audio-client", f), join(dist, "lib", f));
}
const i = process.argv.indexOf("--backend");
if (i > 0) {
  const origin = new URL(process.argv[i + 1]).origin;
  const manifest = JSON.parse(readFileSync(join(dist, "manifest.json"), "utf8"));
  manifest.optional_host_permissions = [`${origin}/*`];
  writeFileSync(join(dist, "manifest.json"), JSON.stringify(manifest, null, 2));
}
console.log(`extension built: ${dist}`);
