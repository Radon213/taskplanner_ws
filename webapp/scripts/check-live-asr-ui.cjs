const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const app = fs.readFileSync(path.join(root, "src", "App.tsx"), "utf8");
const bridge = fs.readFileSync(path.join(root, "src", "hooks", "useRosBridge.ts"), "utf8");
const panel = fs.readFileSync(path.join(root, "src", "components", "command", "LiveAsrPanel.tsx"), "utf8");
const styles = fs.readFileSync(path.join(root, "src", "styles.css"), "utf8");
const violations = [];

if (!app.includes('runtimeMode === "live" ? (\n            <LiveAsrPanel')) {
  violations.push("Live ASR controls must render only in the live integration runtime");
}
if (!bridge.includes('name: "/input/asr/runtime_status"') || !bridge.includes('"taskplanner.asr.status.v1"')) {
  violations.push("Live ASR must subscribe to and validate the authoritative status contract");
}
if (!bridge.includes('"/input/asr/control"') || !bridge.includes('"surgical_msgs/srv/AsrControl"')) {
  violations.push("Live ASR controls must use the typed ROS service contract");
}
if (bridge.includes("result.schema === \"taskplanner.asr.status.v1\"")
  || bridge.includes("result.asr && typeof result.asr")) {
  violations.push("ASR service results must use the single authoritative status envelope");
}
if (!bridge.includes("const latencyMissing = final.response_latency_ms === null")
  || !bridge.includes("latencyMissing ? Number.NaN")) {
  violations.push("Missing ASR latency must remain null instead of being coerced to 0 ms");
}
for (const operation of ["refresh_devices", "start", "stop"]) {
  if (!panel.includes(`onControl("${operation}"`)) {
    violations.push(`Live ASR UI is missing the ${operation} operation`);
  }
}
if (!panel.includes("!selectedDevice") || !panel.includes("startDisabled")) {
  violations.push("ASR start must require a selected Ubuntu input device");
}
for (const deviceStatus of ["NO_INPUT", "HOST_AUDIO_UNAVAILABLE", "BRIDGE_ERROR"]) {
  if (!panel.includes(`status.device_status === "${deviceStatus}"`)) {
    violations.push(`Korean ASR device guidance is missing ${deviceStatus}`);
  }
}
if (!panel.includes("현재 Ubuntu에 선택 가능한 마이크 입력이 없습니다")) {
  violations.push("The Korean no-input state must explain USB connection and Ubuntu selection");
}
if (!panel.includes("!statusFresh || asrActive") || !panel.includes("selectorDisabled")) {
  violations.push("Device refresh and selection must fail closed until status is fresh");
}
if (panel.includes('onChange={(event) => setServer') || panel.includes('type="url"')) {
  violations.push("The backend-authoritative ASR server URL must not be user-editable");
}
if (!panel.includes('aria-live="polite"') || !panel.includes('aria-atomic="true"')) {
  violations.push("Active capture and transcript state must be announced accessibly");
}
if (!panel.includes('role="meter"') || !panel.includes("입력 레벨")) {
  violations.push("The audio level needs a labeled semantic meter");
}
if (!panel.includes("/sensors/surgeon/sentence") || !panel.includes("std_msgs/msg/String")) {
  violations.push("The finalized output topic and type must be visible");
}
if (!styles.includes(".live-asr-actions .button {\n  min-height: 44px")) {
  violations.push("ASR action touch targets must be at least 44px high");
}
if (!styles.includes(".live-asr-panel .field select {\n  min-height: 44px")) {
  violations.push("The ASR device selector must be at least 44px high");
}
if (!styles.includes(".live-asr-panel .field select:focus-visible")) {
  violations.push("The ASR device selector needs a visible keyboard focus style");
}
if (!styles.includes("@media (prefers-reduced-motion: reduce)") || !styles.includes(".live-asr-state.active svg")) {
  violations.push("Live capture motion must honor reduced-motion preferences");
}

if (violations.length) {
  console.error("Live ASR UI guard failed:");
  for (const violation of violations) console.error(`- ${violation}`);
  process.exit(1);
}

console.log("Live ASR UI guard passed.");
