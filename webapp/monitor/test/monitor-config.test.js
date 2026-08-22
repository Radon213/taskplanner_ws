import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";

import {
  DEFAULT_THROTTLE_RATE_MS,
  MIN_THROTTLE_RATE_MS,
  settingsDefaults,
  validateSettings,
} from "../runtime-settings.js";

const runtimeConfigUrl = new URL("../../public/monitor/runtime-config.js", import.meta.url);

async function loadRuntimeConfig(location) {
  const source = await readFile(runtimeConfigUrl, "utf8");
  const window = { location };
  runInNewContext(source, { URLSearchParams, window });
  return window.SURGIMATE_CONFIG;
}

test("monitor runtime config uses the browser host and public bridge port safely", async () => {
  const ipv4 = await loadRuntimeConfig({
    protocol: "http:",
    hostname: "192.168.1.4",
    search: "",
  });
  assert.equal(ipv4.mode, "ros");
  assert.equal(ipv4.rosbridge.url, "ws://192.168.1.4:9092");

  const ipv6 = await loadRuntimeConfig({
    protocol: "https:",
    hostname: "2001:db8::7",
    search: "",
  });
  assert.equal(ipv6.rosbridge.url, "wss://[2001:db8::7]:9092");
});

test("?mode=dummy boots the scoped synthetic fixture without changing the bridge contract", async () => {
  const config = await loadRuntimeConfig({
    protocol: "http:",
    hostname: "localhost",
    search: "?mode=dummy",
  });
  assert.equal(config.mode, "dummy");
  assert.equal(config.dummyDataFile, "/monitor/dummy-data.json");
  assert.equal(config.rosbridge.cameraStreams.throttleRateMs, 100);

  const defaults = settingsDefaults(config);
  assert.equal(defaults.mode, "dummy");
  assert.equal(defaults.bridgeUrl, "ws://localhost:9092");
  assert.equal(defaults.throttleRateMs, 100);
  assert.equal(validateSettings({}, defaults).mode, "dummy");
});

test("monitor page honors runtime mode and keeps every entry path under /monitor", async () => {
  const [app, html, css] = await Promise.all([
    readFile(new URL("../app.js", import.meta.url), "utf8"),
    readFile(new URL("../index.html", import.meta.url), "utf8"),
    readFile(new URL("../styles.css", import.meta.url), "utf8"),
  ]);

  assert.match(app, /const\s+defaultSettings\s*=\s*runtimeDefaults\s*;/);
  assert.doesNotMatch(app, /defaultSettings\s*=\s*Object\.freeze\([^\n]*mode\s*:\s*["']ros["']/);
  assert.match(html, /src=["']\/monitor\/runtime-config\.js["']/);
  assert.match(html, /src=["']\/monitor\/app\.js["']/);
  assert.match(html, /href=["']\/monitor\/styles\.css/);
  assert.equal((html.match(/class=["']flow-list["'][^>]*tabindex=["']0["']/g) || []).length, 2);
  assert.doesNotMatch(html, /fonts\.googleapis\.com|fonts\.gstatic\.com/);
  assert.match(css, /JetBrainsMonoVariable-Latin\.woff2/);
  assert.match(css, /InterVariable-Latin\.woff2/);
});

test("monitor camera settings match the public bridge 100ms floor", () => {
  assert.equal(MIN_THROTTLE_RATE_MS, 100);
  assert.equal(DEFAULT_THROTTLE_RATE_MS, 100);
  const defaults = settingsDefaults({
    mode: "ros",
    dummyDataFile: "/monitor/dummy-data.json",
    rosbridge: { url: "ws://localhost:9092", cameraStreams: {} },
  });
  assert.equal(defaults.throttleRateMs, 100);
  assert.throws(() => validateSettings({ throttleRateMs: 99 }, defaults), /100/);
  assert.equal(validateSettings({ throttleRateMs: 250 }, defaults).throttleRateMs, 250);
});
