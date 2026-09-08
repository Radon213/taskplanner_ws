import { defineConfig } from "playwright/test";

// Keep the presentation-only loop completely isolated from the operator's
// static 4173 page.  This starts a disposable Vite instance with the same UI
// feature surface, while every test supplies its own read-only ROS fixtures.
const webPort = process.env.PLAYWRIGHT_UI_FAST_PORT ?? "4174";
const baseURL = `http://127.0.0.1:${webPort}`;

export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.ui-fast.spec.ts",
  outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR ?? "../test_outputs/playwright-ui-fast",
  workers: 1,
  timeout: 15_000,
  expect: { timeout: 3_000 },
  reporter: "list",
  projects: [
    { name: "fhd", use: { viewport: { width: 1920, height: 1080 } } },
  ],
  use: {
    baseURL,
    ...(process.env.PLAYWRIGHT_BROWSER_CHANNEL === "bundled"
      ? {}
      : { channel: process.env.PLAYWRIGHT_BROWSER_CHANNEL ?? "chrome" }),
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: `VITE_ENABLE_OPTIONAL_UI=true npm run dev -- --host 127.0.0.1 --port ${webPort}`,
    url: baseURL,
    // Never accept the research operator's actual 4173 static page as a test
    // target; a dedicated server makes focused fixtures deterministic.
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
