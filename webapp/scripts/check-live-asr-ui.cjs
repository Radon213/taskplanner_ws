const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const app = fs.readFileSync(path.join(root, "src", "App.tsx"), "utf8");
const bridge = fs.readFileSync(path.join(root, "src", "hooks", "useRosBridge.ts"), "utf8");
const asrMessages = fs.readFileSync(
  path.join(root, "src", "ros", "liveAsrMessages.ts"),
  "utf8",
);
const asrRestartControl = fs.readFileSync(
  path.join(root, "src", "ros", "asrRestartControl.ts"),
  "utf8",
);
const productionServer = fs.readFileSync(
  path.join(root, "scripts", "serve-production.mjs"),
  "utf8",
);
const panel = fs.readFileSync(path.join(root, "src", "components", "command", "LiveAsrPanel.tsx"), "utf8");
const styles = fs.readFileSync(path.join(root, "src", "styles.css"), "utf8");
const violations = [];

if (!app.includes('runtimeMode === "live" ? (\n            <LiveAsrPanel')) {
  violations.push("Live ASR controls must render only in the live integration runtime");
}
if (!bridge.includes('name: "/input/asr/runtime_status"') || !asrMessages.includes('"taskplanner.asr.status.v1"')) {
  violations.push("Live ASR must subscribe to and validate the authoritative status contract");
}
if (!bridge.includes('"/input/asr/control"') || !bridge.includes('"surgical_msgs/srv/AsrControl"')) {
  violations.push("Live ASR controls must use the typed ROS service contract");
}
if (bridge.includes("result.schema === \"taskplanner.asr.status.v1\"")
  || bridge.includes("result.asr && typeof result.asr")) {
  violations.push("ASR service results must use the single authoritative status envelope");
}
if (!asrMessages.includes("const latencyMissing = final.response_latency_ms === null")
  || !asrMessages.includes("latencyMissing ? Number.NaN")) {
  violations.push("Missing ASR latency must remain null instead of being coerced to 0 ms");
}
for (const operation of ["refresh_devices", "set_route_policy", "start", "stop"]) {
  if (!panel.includes(`onControl("${operation}"`)) {
    violations.push(`Live ASR UI is missing the ${operation} operation`);
  }
}
if (!panel.includes('data-slot="live-asr-node-restart"')
  || !panel.includes('onControl("restart_node")')
  || !panel.includes("ASR 노드 새로 시작")) {
  violations.push("Live ASR must expose the dedicated node restart control");
}
if (!asrRestartControl.includes('fetch("/api/runtime/asr/restart"')
  || !asrRestartControl.includes('body: "{}"')
  || !asrRestartControl.includes('fetch("/api/runtime/asr/status"')) {
  violations.push("ASR node restart must use the fixed host endpoint and poll its read-only status");
}
if (!asrRestartControl.includes("crypto.randomUUID()")
  || !asrRestartControl.includes('"X-Taskplanner-Request-Id": requestId')
  || (asrRestartControl.match(/request_id === requestId/g) ?? []).length < 3
  || !productionServer.includes('request.headers["x-taskplanner-request-id"]')) {
  violations.push("ASR restart POST and GET reconciliation must preserve one UUIDv4 request_id through the production proxy");
}
if (!asrRestartControl.includes("Never repeat this uncertain\n    // POST")
  || asrRestartControl.match(/fetch\("\/api\/runtime\/asr\/restart"/g)?.length !== 1) {
  violations.push("An uncertain ASR restart POST must never be retried automatically");
}
for (const field of ["node_instance_id", "node_started_at_sec", "source_revision"]) {
  if (!asrMessages.includes(field) || !panel.includes(field)) {
    violations.push(`Live ASR must normalize and present ${field} restart proof`);
  }
}
if (!asrMessages.includes("[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}")) {
  violations.push("ASR node_instance_id must be normalized as a canonical UUID before it can prove restart");
}
if (!asrMessages.includes("recording_active")
  || !asrRestartControl.includes('"start_recording"')
  || !asrRestartControl.includes("녹화는 새 세그먼트로 복원됨")) {
  violations.push("ASR hot restart must restore an active recording as an explicit new segment");
}
if (!asrRestartControl.includes("const isNewInstance = Boolean(current.status.node_instance_id)")
  || !asrRestartControl.includes("current.status.node_instance_id !== previousStatus.node_instance_id")
  || !asrRestartControl.includes("current.receivedAt >= requestedAt && isNewInstance")) {
  violations.push("ASR owner restart must wait for a fresh heartbeat from a new node instance");
}
if (asrRestartControl.includes("latestPlausibleStartMs")
  || asrRestartControl.includes("containerStartedAtMs < requestedAt")
  || asrRestartControl.includes("containerStartedAtMs > restartedStatusReceivedAt")) {
  violations.push("ASR start provenance must not compare host timestamps to the browser clock");
}
for (const field of ["output_mode", "output_topic"]) {
  if (!asrMessages.includes(field) || !asrRestartControl.includes(field)) {
    violations.push(`ASR hot restart must normalize and verify ${field}`);
  }
}
if (!asrRestartControl.includes("status.available === true")
  || !asrRestartControl.includes('REQUIRED_OUTPUT_MODE = "typed_utterance"')
  || !asrRestartControl.includes('REQUIRED_OUTPUT_TOPIC = "/sensors/surgeon/utterance"')) {
  violations.push("ASR hot restart success must require the Live typed utterance contract");
}
if (!asrRestartControl.includes("const restoreRoutePolicy = previousStatusFresh")
  && !asrRestartControl.includes("const restoreRoutePolicy = previousStatusFresh && !restorationSuppressed")) {
  violations.push("ASR hot restart must preserve route policy for every fresh prior state");
}
if (!asrRestartControl.includes("if (restoreRoutePolicy)")) {
  violations.push("ASR hot restart must preserve route policy for every fresh prior state");
}
const completionTimeout = Number(asrRestartControl.match(/const COMPLETION_TIMEOUT_MS\s*=\s*([\d_]+)/)?.[1]?.replaceAll("_", ""));
if (!Number.isFinite(completionTimeout) || completionTimeout <= 75_000 || completionTimeout > 120_000) {
  violations.push("ASR restart polling must exceed the bounded backend worst-case duration");
}
for (const field of ["state", "connected", "route_policy", "device_id", "recording_active"]) {
  if (!asrRestartControl.includes(`left.${field} === right.${field}`)) {
    violations.push(`ASR restart restore CAS must compare ${field}`);
  }
}
if (!asrRestartControl.includes("RESTORE_CAS_OBSERVATION_PHASES")
  || !asrRestartControl.includes("onBackendStatus(current)")
  || !asrRestartControl.includes("다른 ASR 제어를 감지해 이전 경로·마이크·녹음을 복원하지 않음")) {
  violations.push("ASR restart must suppress stale restore after a concurrent old-instance control change");
}
if (asrRestartControl.includes("onBackendStatus(baseline)")) {
  violations.push("A prior terminal baseline job must not close the new restart's restore CAS window");
}
if (/onBackendStatus\(current\);\s*if \(current\.generation/.test(asrRestartControl)) {
  violations.push("Foreign or repeated baseline snapshots must not reach the restore CAS observer");
}
for (const field of ["endpoint_id", "route_policy", "lan_health"]) {
  if (!asrMessages.includes(field)) {
    violations.push(`Live ASR must normalize the ${field} route-status field`);
  }
}
for (const field of ["available", "connected", "recording_active"]) {
  if (!asrMessages.includes(`typeof snapshot.${field} !== "boolean"`)
    || asrMessages.includes(`Boolean(snapshot.${field})`)) {
    violations.push(`Live ASR must reject a non-boolean ${field} field instead of coercing it`);
  }
}
if (!asrMessages.includes("const routePolicy = normalizeLiveAsrRoutePolicy(snapshot.route_policy)")
  || !asrMessages.includes("routePolicy === null")
  || !asrMessages.includes("route_policy: routePolicy")) {
  violations.push("Live ASR must reject an invalid route policy instead of defaulting it to cloud");
}
if (!bridge.includes('server_url: ""') || !bridge.includes("route_policy: routePolicy")) {
  violations.push("Live ASR controls must send a reviewed route policy, never a URL");
}
if (bridge.includes("server_url: liveAsrStatus.server_url")) {
  violations.push("The browser must not echo a status URL back into ASR control");
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
if (!panel.includes('data-slot="live-asr-route-policy"') || !panel.includes("<fieldset")) {
  violations.push("Live ASR route selection must use a labeled native fieldset");
}
if (!panel.includes('type="radio"') || !panel.includes("routePolicyDisabled")) {
  violations.push("Live ASR route policies must be explicit, keyboard-accessible choices");
}
if (!panel.includes("lanOnlyUnavailable") || !panel.includes("LAN route는 평문 ws://")) {
  violations.push("LAN-only ASR must fail closed and disclose its trusted-network boundary");
}
if (!panel.includes('data-slot="live-asr-route-summary"') || !panel.includes("lanHealthSummary")) {
  violations.push("Live ASR must visibly distinguish the cached LAN health from capture state");
}
if (!panel.includes('aria-live="polite"') || !panel.includes('aria-atomic="true"')) {
  violations.push("Active capture and transcript state must be announced accessibly");
}
if (!panel.includes('role="meter"') || !panel.includes("입력 레벨")) {
  violations.push("The audio level needs a labeled semantic meter");
}
if (!panel.includes("status.output_topic") || !panel.includes("surgical_msgs/msg/SpeechUtterance")) {
  violations.push("The finalized output topic and type must be visible");
}
if (!styles.includes(".live-asr-actions .button {\n  min-height: 44px")) {
  violations.push("ASR action touch targets must be at least 44px high");
}
if (!styles.includes(".live-asr-node-restart .button {\n  width: 100%;\n  min-height: 44px")) {
  violations.push("The ASR node restart control must be full-width and at least 44px high");
}
if (!styles.includes(".live-asr-panel .field select {\n  min-height: 44px")) {
  violations.push("The ASR device selector must be at least 44px high");
}
if (!styles.includes(".live-asr-panel .field select:focus-visible")) {
  violations.push("The ASR device selector needs a visible keyboard focus style");
}
if (!styles.includes(".live-asr-route-policy label {\n  display: flex")
  || !styles.includes("min-height: 52px")
  || !styles.includes(".live-asr-route-policy input:focus-visible")) {
  violations.push("ASR route policies need accessible touch targets and keyboard focus");
}
if (!styles.includes(".live-asr-route-policy > div {\n    grid-template-columns: 1fr")) {
  violations.push("ASR route policies must stack on narrow screens");
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
