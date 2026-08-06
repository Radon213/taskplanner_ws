import { defineConfig } from "playwright/test";

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
    baseURL: "http://127.0.0.1:4173",
    channel: "chrome",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4173",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
