import { build } from "esbuild";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const directory = dirname(fileURLToPath(import.meta.url));

await build({
  entryPoints: [resolve(directory, "entry.mjs")],
  bundle: true,
  format: "iife",
  globalName: "NekoSoullinkEmotion",
  target: "es2020",
  minify: true,
  legalComments: "none",
  banner: {
    js: "/*! @soullink-emotion/engine v0.1.0-beta.1 | MIT | static/libs/licenses/soullink-emotion-engine-MIT.txt */",
  },
  outfile: resolve(directory, "../../static/libs/soullink-emotion-engine.min.js"),
});
