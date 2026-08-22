import { fileURLToPath } from "node:url";
import { loadConfigFromFile } from "vite";

const root = fileURLToPath(new URL("..", import.meta.url));
const configPath = fileURLToPath(new URL("../vite.config.ts", import.meta.url));
const loaded = await loadConfigFromFile(
  { command: "serve", mode: "development", isSsrBuild: false, isPreview: false },
  configPath,
  root,
  "error",
);

if (!loaded) {
  console.error("Dev-server contract check could not load vite.config.ts.");
  process.exit(1);
}

const { host, port, strictPort } = loaded.config.server ?? {};
const violations = [];
if (host !== "127.0.0.1") violations.push(`host must be 127.0.0.1, found ${String(host)}`);
if (port !== 4173) violations.push(`port must be 4173, found ${String(port)}`);
if (strictPort !== true) violations.push("strictPort must be true so 4173 never drifts to 4174");

if (violations.length) {
  console.error("Dev-server contract check failed:");
  for (const violation of violations) console.error(`- ${violation}`);
  process.exit(1);
}

console.log("Dev-server contract passed: loopback-only, fixed to strict port 4173.");
