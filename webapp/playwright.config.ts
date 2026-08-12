import { defineConfig } from "playwright/test";

const webPort = process.env.PLAYWRIGHT_WEB_PORT ?? "4173";
const baseURL =
  process.env.PLAYWRIGHT_BASE_URL ?? `http://127.0.0.1:${webPort}`;
const browserChannel = process.env.PLAYWRIGHT_BROWSER_CHANNEL ?? "chrome";

export default defineConfig({
  testDir: "./tests",
  outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR ?? "../test_outputs/playwright",
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: {
    timeout: 8_000,
  },
  reporter: "list",
  projects: [
    { name: "fhd", use: { viewport: { width: 1920, height: 1080 } } },
    { name: "qhd", use: { viewport: { width: 2560, height: 1440 } } },
    { name: "uhd", use: { viewport: { width: 3840, height: 2160 } } },
  ],
  use: {
    baseURL,
    ...(browserChannel === "bundled" ? {} : { channel: browserChannel }),
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${webPort}`,
    url: baseURL,
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
