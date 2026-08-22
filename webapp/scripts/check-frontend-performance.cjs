const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const app = fs.readFileSync(path.join(root, "src", "App.tsx"), "utf8");
const missionBridge = fs.readFileSync(
  path.join(root, "src", "hooks", "useRosBridge.ts"),
  "utf8",
);
const multicamBridge = fs.readFileSync(
  path.join(root, "src", "hooks", "useMulticamOpsBridge.ts"),
  "utf8",
);
const debugBridge = fs.readFileSync(
  path.join(root, "src", "hooks", "useIntegrationDebugBridge.ts"),
  "utf8",
);
const multicamWorkspace = fs.readFileSync(
  path.join(root, "src", "components", "multicam", "MulticamOpsWorkspace.tsx"),
  "utf8",
);
const runtimeModes = fs.readFileSync(path.join(root, "src", "runtimeModes.ts"), "utf8");
const procedureDock = fs.readFileSync(
  path.join(root, "src", "components", "command", "ProcedureDock.tsx"),
  "utf8",
);
const statusRibbon = fs.readFileSync(
  path.join(root, "src", "components", "command", "StatusRibbon.tsx"),
  "utf8",
);
const runtimeControl = fs.readFileSync(
  path.join(root, "src", "hooks", "useRuntimeControl.ts"),
  "utf8",
);
const violations = [];

for (const eagerImport of [
  'import { DebugWorkspace } from "./components/debug/DebugWorkspace"',
  'import { MulticamOpsWorkspace } from "./components/multicam/MulticamOpsWorkspace"',
]) {
  if (app.includes(eagerImport)) {
    violations.push(`Optional workspace must not be part of the initial bundle: ${eagerImport}`);
  }
}

for (const lazyImport of [
  'import("./components/debug/DebugWorkspace")',
  'import("./components/multicam/MulticamOpsWorkspace")',
]) {
  if (!app.includes(lazyImport)) {
    violations.push(`Optional workspace must use a lazy chunk: ${lazyImport}`);
  }
}

for (const [name, source] of [
  ["mission", missionBridge],
  ["multicam", multicamBridge],
  ["debug", debugBridge],
]) {
  if (source.includes("new ROSLIB.Ros({ url })")) {
    violations.push(`${name} bridge must not open a WebSocket during StrictMode's disposable first effect`);
  }
  if (!source.includes("const ros = new ROSLIB.Ros();")) {
    violations.push(`${name} bridge must construct its ROS client without eager connection`);
  }
  if (!source.includes("connectionTimer = window.setTimeout")) {
    violations.push(`${name} bridge must defer connect so StrictMode cleanup can cancel it`);
  }
  if (!source.includes("window.clearTimeout(connectionTimer)")) {
    violations.push(`${name} bridge must cancel a deferred connection during cleanup`);
  }
}

for (const debugLifecycleGuard of [
  "DEBUG_STATUS_MAX_AGE_MS",
  "bridgeGenerationRef.current = generation",
  "commandReadyGenerationRef.current = generation",
  "pendingCommandCancelsRef.current",
  "statusReceivedAtRef.current",
  "if (!isCurrentGeneration()) return",
  "ros.off(serviceCallId, handler)",
  "ros.callOnConnection({",
  "디버그 명령 응답 시간이 초과되었습니다.",
]) {
  if (!debugBridge.includes(debugLifecycleGuard)) {
    violations.push(`Debug bridge lifecycle guard is missing: ${debugLifecycleGuard}`);
  }
}

if (debugBridge.includes("setFreshnessTick")) {
  violations.push("Debug freshness monitoring must not force an idle workspace render every 500 ms");
}

if (!multicamWorkspace.includes("const TfScene = memo(function TfScene")) {
  violations.push("The Three.js TF workspace must not rerender for unrelated camera-frame updates");
}

for (const heartbeatGuard of [
  "sameBedRobotArmState(current.arms, status.arms)",
  "scheduleBedRobotArmExpiry(status)",
]) {
  if (!missionBridge.includes(heartbeatGuard)) {
    violations.push(`Bed-robot status heartbeat guard is missing: ${heartbeatGuard}`);
  }
}

for (const generationGuard of [
  "useLayoutEffect(() => {",
  "bridgeGenerationRef.current = generation",
  "commandReadyGenerationRef.current = generation",
  "if (!isCurrentGeneration()) return",
  "pendingServiceCancelsRef.current",
]) {
  if (!missionBridge.includes(generationGuard)) {
    violations.push(`ROS bridge generation guard is missing: ${generationGuard}`);
  }
}

for (const missionFreshnessGuard of [
  "RUNTIME_STATE_MAX_AGE_MS",
  "simulationStateReceivedAtRef.current",
  "shadowReplayStateReceivedAtRef.current",
  'runtimeMode === "shadow"',
  "Runtime state heartbeat expired before the service response arrived.",
]) {
  if (!missionBridge.includes(missionFreshnessGuard)) {
    violations.push(`Mission runtime freshness guard is missing: ${missionFreshnessGuard}`);
  }
}

for (const runtimeLockGuard of [
  "runtimeModeLocked",
  "isRunning || isPaused || startInFlight",
  "startInFlight || commandBusy",
  "disabled={runtimeModeLocked}",
  "runtime-mode-lock-note",
]) {
  if (!procedureDock.includes(runtimeLockGuard)) {
    violations.push(`Active-run runtime switch guard is missing: ${runtimeLockGuard}`);
  }
}

for (const coldStartGuard of [
  "runtimeTransition.activeMode === null",
  "현재 모드 시작",
  "runtimeTransition.retryable || noActiveRuntime",
]) {
  if (!procedureDock.includes(coldStartGuard)) {
    violations.push(`Cold-start runtime recovery guard is missing: ${coldStartGuard}`);
  }
}

for (const debugEntryGuard of [
  [statusRibbon, "debugModeDisabled"],
  [statusRibbon, "disabled={debugModeDisabled}"],
  [app, "safety?.isRunning"],
  [app, "safety?.isPaused"],
  [app, "safety?.startInFlight"],
  [app, "safety?.actionPending"],
  [app, 'onRuntimeModeChange("debug", runtimeTransitionSafety)'],
]) {
  const [source, guard] = debugEntryGuard;
  if (!source.includes(guard)) {
    violations.push(`Standalone Debug entry safety guard is missing: ${guard}`);
  }
}


for (const workspaceRecoveryGuard of [
  "class WorkspaceErrorBoundary",
  "static getDerivedStateFromError",
  "onRetry={() => window.location.reload()}",
]) {
  if (!app.includes(workspaceRecoveryGuard)) {
    violations.push(`Lazy workspace recovery guard is missing: ${workspaceRecoveryGuard}`);
  }
}

for (const runtimeDisplayGuard of [
  "runtimeTransition.activeMode === mode",
  "runtimeTransition.activeMode,",
  "runtimeTransition.requestedMode ?? runtimeMode",
]) {
  if (!(app + procedureDock).includes(runtimeDisplayGuard)) {
    violations.push(`Runtime authority/display guard is missing: ${runtimeDisplayGuard}`);
  }
}

if (!runtimeControl.includes("if (generation !== transitionGenerationRef.current) return false;")) {
  violations.push("Runtime status refresh must not overwrite a newer transition generation");
}

for (const rejectedTransitionGuard of [
  'phase: response.status === 409 ? "blocked" : "failed"',
  'message: status.message?.trim() || ""',
]) {
  if (!runtimeControl.includes(rejectedTransitionGuard)) {
    violations.push(`Runtime rejection feedback guard is missing: ${rejectedTransitionGuard}`);
  }
}

for (const workspaceRuntimeGuard of [
  'onMonitor={() => navigateWorkspace("monitor")}',
  'setRuntimeMode(runtimeTransition.activeMode)',
]) {
  if (!app.includes(workspaceRuntimeGuard)) {
    violations.push(`Observer workspace runtime transition guard is missing: ${workspaceRuntimeGuard}`);
  }
}

for (const unsafeMulticamTransition of [
  "requestMulticamRuntime",
  "MULTICAM_RUNTIME_SWITCH_STORAGE_KEY",
  "MULTICAM_RETURN_MODE_STORAGE_KEY",
]) {
  if (app.includes(unsafeMulticamTransition)) {
    violations.push(`Multicam observer must not replace the active runtime: ${unsafeMulticamTransition}`);
  }
}

for (const observerGuard of [
  [runtimeModes, 'return `${browserProtocol()}//${websocketHostname()}:${port}/multicam`'],
  [multicamBridge, 'const OBSERVER_TOPICS_SERVICE = "/multicam_observer/rosapi/topics"'],
  [multicamBridge, "CAPTURE_STATUS_MAX_AGE_MS"],
  [multicamBridge, "TOPIC_DISCOVERY_TIMEOUT_MS"],
  [multicamBridge, "topicRefreshInFlightRef.current"],
  [multicamBridge, "pendingServiceCancelsRef.current"],
  [multicamBridge, "pendingFrames.current.clear()"],
  [multicamBridge, "setCaptureStatusFresh(true)"],
  [multicamBridge, "Date.now() - captureStatus.receivedAt <= CAPTURE_STATUS_MAX_AGE_MS"],
  [multicamBridge, "observerGenerationRef.current = generation"],
  [multicamBridge, "setCaptureStatus(null)"],
  [multicamBridge, "if (!isCurrentGeneration()) return"],
  [multicamBridge, "World Anchor 서비스 호출을 전송하지 않았습니다."],
  [multicamWorkspace, "전용 observer는 read-only입니다."],
  [multicamWorkspace, "bridge.captureStatusFresh"],
]) {
  const [source, guard] = observerGuard;
  if (!source.includes(guard)) {
    violations.push(`Dedicated read-only Multicam observer guard is missing: ${guard}`);
  }
}

for (const remoteRoutingGuard of [
  "isLoopbackHostname(browserHostname())",
  'VITE_ROSBRIDGE_TAILSCALE_PORT?.trim() || "9091"',
  'VITE_ROSBRIDGE_LIVE_TAILSCALE_PATH?.trim() || "/live"',
  'VITE_ROSBRIDGE_LLM_TAILSCALE_PATH?.trim() || "/llm"',
  'VITE_ROSBRIDGE_SHADOW_TAILSCALE_PATH?.trim() || "/shadow"',
  'hostname.includes(":") && !hostname.startsWith("[")',
]) {
  if (!runtimeModes.includes(remoteRoutingGuard)) {
    violations.push(`Remote path-router guard is missing: ${remoteRoutingGuard}`);
  }
}

for (const forbiddenObserverService of ["WORLD_SERVICES", "/multicam_observer/rosapi/publishers"]) {
  if (multicamBridge.includes(forbiddenObserverService)) {
    violations.push(`Multicam observer must not expose or call this service: ${forbiddenObserverService}`);
  }
}

if (violations.length) {
  console.error("Frontend performance guard failed:");
  for (const violation of violations) console.error(`- ${violation}`);
  process.exit(1);
}

console.log("Frontend performance guard passed.");
