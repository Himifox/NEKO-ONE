import { Live2DProfileAutoGenerator } from "@soullink-emotion/profile-generator";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const directory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(directory, "../..");
const modelName = process.argv[2] || "xiaoliyu";
const modelDirectory = resolve(repositoryRoot, "var/public-room/live2d", modelName);

if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(modelName)) {
  throw new Error("model name contains unsupported characters");
}
if (!existsSync(modelDirectory)) {
  throw new Error(`Live2D model directory does not exist: ${modelDirectory}`);
}

const generator = new Live2DProfileAutoGenerator({
  modelsRoot: resolve(repositoryRoot, "var/public-room/live2d"),
  modelsBaseUrl: "/live2d-assets",
  useConfiguredOpenAI: false,
});
const result = await generator.ensure({
  modelDir: modelName,
  displayName: modelName,
  // Do not overwrite an operator's hand-tuned mapping. Remove the existing
  // profile first when an intentional regeneration is desired.
  force: false,
});

if (!result?.profile?.parameterMap || !result.profileUrl) {
  throw new Error("Soullink profile generator returned an incomplete profile");
}
console.log(`Generated ${result.profileUrl}`);
