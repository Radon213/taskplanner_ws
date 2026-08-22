import { compressedImageTiming, normalizeCompressedImage } from "./ros/compressed-image.js";
import { cameraTimingStampNs, createCameraPlayout } from "./ros/camera-playout.js";
import { createDiagnosticLog } from "./ros/diagnostic-log.js";
import {
  PUBLIC_CONTRACT,
  PUBLIC_TOPIC_NAMES,
  SCENARIO_STATE_TOPICS,
  createMainLayoutSubscriptions,
} from "./ros/public-contract.js";
import { RosBridgeClient } from "./ros/ros-bridge-client.js";
import { replayDummyFixture } from "./ros/dummy-fixture.js";
import { MainLayoutScenarioMapper } from "./ros/scenario-mapper.js";
import {
  loadDummyData as fetchDummyData,
  readDummyDataFile,
  settingsDefaults,
  validateDummyFixture,
  validateSettings,
} from "./runtime-settings.js";

const DESIGN_WIDTH = 1920;
const DESIGN_HEIGHT = 1080;
const DIRECT_TEACHING_DELAY_MS = 5000;
const CAMERA_PLAYOUT_MODES = new Set(["latest", "live", "replay"]);
const CAMERA_FITS = new Set(["contain", "cover"]);

function normalizedCameraPlayoutMode(value) {
  const mode = String(value || "").trim().toLowerCase();
  return CAMERA_PLAYOUT_MODES.has(mode) ? mode : "latest";
}

function normalizedCameraFit(value) {
  const fit = String(value || "").trim().toLowerCase();
  return CAMERA_FITS.has(fit) ? fit : "contain";
}

function applyCameraFit(value) {
  const fit = normalizedCameraFit(value);
  document.querySelector(".app-shell").dataset.cameraFit = fit;
  return fit;
}

const assets = {
  surgeryFallback: new URL("./assets/figma/raw-14.png", import.meta.url).href,
  surgeon: new URL("./assets/figma/surgeon-prof-sung.png", import.meta.url).href,
  forceps: new URL("./assets/figma/raw-03.png", import.meta.url).href,
  adsonForceps: new URL("./assets/figma/adson-forceps.png", import.meta.url).href,
  kocherClamp: new URL("./assets/figma/raw-19.png", import.meta.url).href,
  metzenbaumScissors: new URL("./assets/figma/metzenbaum-scissors.png", import.meta.url).href,
  t01Scalpel: new URL("./assets/figma/instruments/t01-scalpel.png", import.meta.url).href,
  t02AdsonForceps: new URL("./assets/figma/instruments/t02-adson-forceps.png", import.meta.url).href,
  t03AllisClampForceps: new URL("./assets/figma/instruments/t03-allis-clamp-forceps.png", import.meta.url).href,
  t04BovieSurgicalCautery: new URL("./assets/figma/instruments/t04-bovie-surgical-cautery.png", import.meta.url).href,
  t05ArmyNavyRetractor: new URL("./assets/figma/instruments/t05-army-navy-retractor.png", import.meta.url).href,
  t07BipolarCautery: new URL("./assets/figma/instruments/t07-bipolar-cautery.png", import.meta.url).href,
  t08MosquitoForceps: new URL("./assets/figma/instruments/t08-mosquito-forceps.png", import.meta.url).href,
  t11ThyroidRetractor: new URL("./assets/figma/instruments/t11-thyroid-retractor.png", import.meta.url).href,
};

const toolRegistry = {
  none: { name: "NONE", image: null },
  kelly: { name: "Kelly", image: null },
  forceps: { name: "Forceps", image: assets.forceps },
  "adson-forceps": { name: "Adson Forceps", image: assets.adsonForceps },
  "kocher-clamp": { name: "Kocher Clamp", image: assets.kocherClamp },
  "metzenbaum-scissors": { name: "Metzenbaum Scissors", image: assets.metzenbaumScissors },
  T01: { name: "Scalpel", image: assets.t01Scalpel, visualClass: "instrument-t01" },
  T02: { name: "Adson Forceps", image: assets.t02AdsonForceps, visualClass: "instrument-t02" },
  T03: { name: "Allis Clamp Forceps", image: assets.t03AllisClampForceps, visualClass: "instrument-t03" },
  T04: { name: "Bovie Surgical Cautery", image: assets.t04BovieSurgicalCautery, visualClass: "instrument-t04" },
  T05: { name: "Army Navy Retractor", image: assets.t05ArmyNavyRetractor, visualClass: "instrument-t05" },
  T07: { name: "Bipolar Cautery", image: assets.t07BipolarCautery, visualClass: "instrument-t07" },
  T08: { name: "Mosquito Forceps", image: assets.t08MosquitoForceps, visualClass: "instrument-t08" },
  T11: { name: "Thyroid Retractor", image: assets.t11ThyroidRetractor, visualClass: "instrument-t11" },
};
const builtInToolIds = new Set(Object.keys(toolRegistry));
const reservedRegistryKeys = new Set(["__proto__", "prototype", "constructor"]);

const state = {
  connected: false,
  connectionState: "simulation",
  connectionDetail: "",
  elapsedSeconds: 1 * 3600 + 24 * 60 + 5,
  elapsedAvailable: true,
  elapsedSource: "simulation",
  view: "overview",
  phase: {
    id: "",
    code: "—",
    index: 0,
    total: 0,
    name: "Waiting for catalog",
    description: "",
    waitingForUpdate: true,
  },
  procedure: { name: "Thyroidectomy", targetSite: "Right Lobectomy", approach: "BABA" },
  surgeon: { name: "Prof. Sung", department: "ENT", image: assets.surgeon },
  arms: {
    1: { status: "idle", toolId: "none", endEffectorId: "left_hand" },
    2: { status: "idle", toolId: "adson-forceps", endEffectorId: "right_hand" },
    3: { status: "idle", toolName: "Retraction" },
    4: { status: "idle", toolName: "Suction" },
  },
  instrumentFlow: {
    inUse: [{ toolId: "metzenbaum-scissors", instanceId: "", state: "in_use", confidence: 1, evidenceStatus: "SIMULATION" }],
    mayo: [{ toolId: "kelly", instanceId: "", state: "awaiting_retrieval", confidence: 1, evidenceStatus: "SIMULATION" }],
  },
  retrieval: { retrievedToolId: "kelly", location: "MAYO", inUseToolId: "metzenbaum-scissors" },
  predictions: [
    { toolId: "forceps", confidence: 82.3, arm: 2, status: "standby" },
    { toolId: "kocher-clamp", confidence: 51.6, arm: 2, status: "standby" },
    { toolId: "metzenbaum-scissors", confidence: 36.7, arm: 2, status: "standby" },
  ],
  voice: { status: "listening", text: "Listening..." },
};
const simulationState = structuredClone(state);

const sharedFlirFrame = {
  topic: PUBLIC_TOPIC_NAMES.flirCamera,
  src: "",
  objectUrl: null,
  lastSeenAt: 0,
  presentedAt: 0,
  sourceTimestampMs: null,
  frameId: "",
  sourceDeltaMs: null,
  receiveDeltaMs: null,
  observedPlaybackRate: null,
};

const rosRuntime = {
  client: null,
  cameraPlayout: null,
  scenarioMapper: new MainLayoutScenarioMapper(),
  customMapper: null,
  gatewayLastSeenAt: 0,
  lastAnyMessageAt: 0,
  transportConnectedAt: 0,
  livenessReconnectRequested: false,
  lastSeenByTopic: new Map(),
  lastRevisionByTopic: new Map(),
  staleTopics: new Set(),
  contractCompatible: false,
  procedureActive: false,
  health: null,
  gatewayTimer: null,
  cameraFrames: {
    overview: sharedFlirFrame,
    focus: sharedFlirFrame,
  },
  cameraThrottleRateMs: 100,
  cameraPlayoutMode: "latest",
  observedRunId: "",
  activeObservedAt: 0,
  connectionGeneration: 0,
  lastGatewayMeta: null,
  lastHealthFingerprint: "",
  cameraVisibilityByView: new Map(),
};

const runtimeConfig = window.SURGIMATE_CONFIG || { mode: "ros", rosbridge: {} };
const runtimeDefaults = settingsDefaults(runtimeConfig);
const defaultSettings = runtimeDefaults;
// Connection settings are intentionally session-only. Every page load starts
// from the reviewed runtime-config server instead of a previously saved Dummy
// mode or bridge URL.
let activeSettings = defaultSettings;
let settingsApplyGeneration = 0;
const diagnostics = createDiagnosticLog({ capacity: 10000 });
diagnostics.record("info", "session.started", {
  mode: activeSettings.mode,
  url: activeSettings.bridgeUrl,
  cameraThrottleRateMs: activeSettings.throttleRateMs,
  cameraFit: activeSettings.cameraFit,
  userAgent: typeof navigator === "object" ? navigator.userAgent : "",
});

const directTeachingRuntime = {
  activeArm: null,
  pendingArm: null,
  timerId: null,
};

function directTeachingSnapshot() {
  return Object.freeze({
    activeArm: directTeachingRuntime.activeArm,
    pendingArm: directTeachingRuntime.pendingArm,
    delayMs: DIRECT_TEACHING_DELAY_MS,
  });
}

function renderDirectTeachingState() {
  document.querySelectorAll("[data-direct-teaching-arm]").forEach((summary) => {
    const armNumber = Number(summary.dataset.directTeachingArm);
    const active = directTeachingRuntime.activeArm === armNumber;
    const pending = directTeachingRuntime.pendingArm === armNumber;
    summary.setAttribute("aria-pressed", String(active));
    summary.setAttribute("aria-busy", String(pending));
    summary.dataset.directTeachingState = active ? "active" : pending ? "pending" : "off";
    summary.title = active
      ? `ARM ${armNumber} Direct Teaching 모드 해제`
      : pending
        ? `ARM ${armNumber} Direct Teaching 시작 대기 중 · 다시 누르면 취소`
        : `ARM ${armNumber} Direct Teaching 모드 시작`;
  });
}

function cancelDirectTeachingTimer() {
  if (directTeachingRuntime.timerId !== null) clearTimeout(directTeachingRuntime.timerId);
  directTeachingRuntime.timerId = null;
}

function clearDirectTeaching(reason = "reset") {
  const previousActiveArm = directTeachingRuntime.activeArm;
  const previousPendingArm = directTeachingRuntime.pendingArm;
  cancelDirectTeachingTimer();
  directTeachingRuntime.activeArm = null;
  directTeachingRuntime.pendingArm = null;
  renderDirectTeachingState();
  if (previousActiveArm !== null || previousPendingArm !== null) {
    diagnostics.record("info", "direct_teaching.cleared", {
      reason,
      previousActiveArm,
      previousPendingArm,
    });
  }
}

function handleDirectTeachingRequest(armNumber) {
  if (![3, 4].includes(armNumber)) return directTeachingSnapshot();

  if (directTeachingRuntime.activeArm === armNumber) {
    clearDirectTeaching("user_deactivated");
    return directTeachingSnapshot();
  }

  if (directTeachingRuntime.pendingArm === armNumber) {
    clearDirectTeaching("user_cancelled_pending");
    return directTeachingSnapshot();
  }

  cancelDirectTeachingTimer();
  directTeachingRuntime.activeArm = null;
  directTeachingRuntime.pendingArm = armNumber;
  renderDirectTeachingState();
  diagnostics.record("info", "direct_teaching.requested", {
    armNumber,
    delayMs: DIRECT_TEACHING_DELAY_MS,
  });

  directTeachingRuntime.timerId = setTimeout(() => {
    if (directTeachingRuntime.pendingArm !== armNumber) return;
    directTeachingRuntime.timerId = null;
    directTeachingRuntime.pendingArm = null;
    directTeachingRuntime.activeArm = armNumber;
    renderDirectTeachingState();
    diagnostics.record("info", "direct_teaching.activated", { armNumber });
  }, DIRECT_TEACHING_DELAY_MS);

  return directTeachingSnapshot();
}

function initializeDirectTeachingControls() {
  document.querySelectorAll("[data-direct-teaching-arm]").forEach((summary) => {
    const request = () => handleDirectTeachingRequest(Number(summary.dataset.directTeachingArm));
    summary.addEventListener("click", request);
    summary.addEventListener("keydown", (event) => {
      if (event.repeat || !["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      request();
    });
  });
  renderDirectTeachingState();
}

function finiteRevision(message) {
  const revision = Number(message?.revision);
  return Number.isFinite(revision) ? revision : null;
}

function topicItemCount(topic, message) {
  const field = topic === PUBLIC_TOPIC_NAMES.catalog
    ? "phases"
    : topic === PUBLIC_TOPIC_NAMES.instruments
      ? "instruments"
      : topic === PUBLIC_TOPIC_NAMES.robots
        ? "robots"
        : topic === PUBLIC_TOPIC_NAMES.robotEndEffectors
          ? "end_effectors"
          : topic === PUBLIC_TOPIC_NAMES.toolPredictions
            ? "predictions"
            : "";
  return field && Array.isArray(message?.[field]) ? message[field].length : null;
}

function topicEnvelope(topic, message) {
  return {
    topic,
    revision: finiteRevision(message),
    procedureRunId: typeof message?.procedure_run_id === "string" ? message.procedure_run_id : "",
    procedureActive: typeof message?.procedure_active === "boolean" ? message.procedure_active : null,
    gatewayInstanceId: typeof message?.gateway_instance_id === "string" ? message.gateway_instance_id : "",
    catalogVersion: typeof message?.catalog_version === "string" ? message.catalog_version : "",
    itemCount: topicItemCount(topic, message),
  };
}

function recordTopicResult(topic, message, { accepted, reason = "", details = {} } = {}) {
  const currentRevision = finiteRevision(message);
  const previousRevision = rosRuntime.lastRevisionByTopic.get(topic);
  let revisionDirection = "missing";
  if (currentRevision !== null) {
    revisionDirection = previousRevision === undefined
      ? "initial"
      : currentRevision > previousRevision
        ? "increase"
        : currentRevision < previousRevision
          ? "decrease"
          : "same";
    rosRuntime.lastRevisionByTopic.set(topic, currentRevision);
  }
  diagnostics.record(accepted ? "debug" : "warn", accepted ? "topic.accepted" : "topic.rejected", {
    ...topicEnvelope(topic, message),
    previousRevision: previousRevision ?? null,
    revisionDirection,
    reason,
    ...details,
  });
  if (accepted && rosRuntime.staleTopics.has(topic)) {
    diagnostics.record("info", "topic.recovered", {
      topic,
      revision: currentRevision,
      staleDurationMs: Math.max(0, Date.now() - (rosRuntime.lastSeenByTopic.get(topic) || Date.now())),
    });
  }
}

const pad = (value) => String(value).padStart(2, "0");
const getTool = (toolId) => toolRegistry[toolId] ?? { name: toolId || "NONE", image: null };

function normalizedToolName(value) {
  return String(value || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

function visualDefinitionForTool(name) {
  const normalized = normalizedToolName(name);
  if (normalized.includes("adson") && normalized.includes("forceps")) return toolRegistry["adson-forceps"];
  if (normalized.includes("metzenbaum")) return toolRegistry["metzenbaum-scissors"];
  if (normalized.includes("kocher") && normalized.includes("clamp")) return toolRegistry["kocher-clamp"];
  if (normalized === "forceps") return toolRegistry.forceps;
  return null;
}

function registerRosTools(definitions = []) {
  definitions.forEach(({ id, name }) => {
    if (!id || !name || String(id).length > 96 || reservedRegistryKeys.has(String(id))) return;
    const visual = toolRegistry[id]?.image ? toolRegistry[id] : visualDefinitionForTool(name);
    toolRegistry[id] = {
      ...(visual || { image: null }),
      name,
    };
  });
}

function clearDynamicToolRegistry() {
  Object.keys(toolRegistry).forEach((toolId) => {
    if (!builtInToolIds.has(toolId)) delete toolRegistry[toolId];
  });
}

function deepMerge(target, source) {
  Object.entries(source || {}).forEach(([key, value]) => {
    if (["__proto__", "prototype", "constructor"].includes(key)) return;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      target[key] = deepMerge({ ...(target[key] || {}) }, value);
    } else {
      target[key] = value;
    }
  });
  return target;
}

const safeText = (value, maximum = 256) => String(value ?? "").trim().slice(0, maximum);

function createBlankDummyState(view = state.view) {
  return {
    connected: false,
    connectionState: "dummy",
    connectionDetail: activeSettings.dummyDataFile,
    elapsedSeconds: 0,
    elapsedAvailable: true,
    elapsedSource: "dummy",
    view,
    phase: {
      id: "",
      code: "—",
      index: 0,
      total: 0,
      name: "None",
      description: "",
      waitingForUpdate: false,
    },
    procedure: { name: "None", targetSite: "None", approach: "None" },
    surgeon: { name: "None", department: "", image: assets.surgeon },
    arms: {
      1: { status: "unknown", toolId: "none", endEffectorId: "left_hand" },
      2: { status: "unknown", toolId: "none", endEffectorId: "right_hand" },
      3: { status: "unknown", toolName: "Retraction" },
      4: { status: "unknown", toolName: "Suction" },
    },
    instrumentFlow: { inUse: [], mayo: [] },
    retrieval: { retrievedToolId: "none", location: "MAYO", inUseToolId: "none" },
    predictions: [],
    voice: { status: "listening", text: "Listening..." },
  };
}

function replaceState(nextState) {
  Object.keys(state).forEach((key) => delete state[key]);
  Object.assign(state, structuredClone(nextState));
}

function fitDesignToViewport() {
  const app = document.querySelector(".app-shell");
  const viewportWidth = Math.max(1, window.innerWidth || DESIGN_WIDTH);
  const viewportHeight = Math.max(1, window.innerHeight || DESIGN_HEIGHT);
  const scale = Math.min(viewportWidth / DESIGN_WIDTH, viewportHeight / DESIGN_HEIGHT);
  const logicalWidth = Math.max(DESIGN_WIDTH, viewportWidth / scale);
  const logicalHeight = Math.max(DESIGN_HEIGHT, viewportHeight / scale);

  // The monitor canvas intentionally scales as one pixel-accurate composition.
  // A settings dialog is an interaction surface, though, and becomes unusably
  // small when that composition is shown on a compact viewport. Compensate
  // only the dialog (and only below the desktop scale) while keeping the
  // surgical view itself unchanged. The height bound prevents the enlarged
  // dialog from extending beyond the viewport on short screens.
  const panelWidth = 510;
  const panelHeight = 877;
  const panelScale = scale < 0.8
    ? Math.min(
      (viewportWidth * 0.92) / (panelWidth * scale),
      (viewportHeight * 0.9) / (panelHeight * scale),
    )
    : 1;

  app.style.left = "0px";
  app.style.top = "0px";
  app.style.width = `${logicalWidth}px`;
  app.style.height = `${logicalHeight}px`;
  app.style.setProperty("--settings-panel-scale", String(Math.max(1, panelScale)));
  app.style.transform = `scale(${scale})`;
}

function renderTime() {
  const now = new Date();
  const currentTime = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
  const elapsedSeconds = Math.max(0, Number(state.elapsedSeconds) || 0);
  const hours = Math.floor(elapsedSeconds / 3600);
  const minutes = Math.floor((elapsedSeconds % 3600) / 60);
  const seconds = Math.floor(elapsedSeconds % 60);
  const elapsedTime = state.elapsedAvailable ? `${pad(hours)}:${pad(minutes)}:${pad(seconds)}` : "--:--:--";
  document.querySelector("#clock").textContent = currentTime;
  document.querySelector("#focus-clock").textContent = currentTime;
  document.querySelector("#elapsed").textContent = elapsedTime;
  document.querySelector("#focus-elapsed").textContent = elapsedTime;
  const elapsedTitle = state.elapsedSource === "observed"
    ? "UI-observed elapsed time (starts when this browser first sees the active run)"
    : state.elapsedSource === "idle"
      ? "No active procedure"
      : "";
  document.querySelector("#elapsed").title = elapsedTitle;
  document.querySelector("#focus-elapsed").title = elapsedTitle;
}

function renderPhase() {
  const copy = document.querySelector(".phase-copy");
  const phaseCode = copy.querySelector("small");
  const phaseName = copy.querySelector("strong");
  const phaseDescription = copy.querySelector("span");
  const total = Math.min(64, Math.max(0, Math.trunc(Number(state.phase.total) || 0)));
  const activeIndex = Math.min(total, Math.max(0, Math.trunc(Number(state.phase.index) || 0)));
  phaseCode.textContent = state.phase.code;
  phaseName.textContent = state.phase.name;
  phaseDescription.textContent = state.phase.description;
  phaseName.style.fontSize = "";
  phaseDescription.style.fontSize = "";
  const phase = document.querySelector(".phase");
  phase.classList.toggle("uncertain", state.phase.uncertain === true);
  phase.classList.toggle("waiting", state.phase.waitingForUpdate === true);
  phase.setAttribute("aria-busy", String(state.phase.waitingForUpdate === true));
  const details = [
    state.phase.waitingForUpdate ? "Waiting for latest context" : "",
    Number.isFinite(state.phase.confidence) ? `confidence ${(state.phase.confidence * 100).toFixed(1)}%` : "",
    state.phase.executionState || "",
    ...(Array.isArray(state.phase.safetyFlags) ? state.phase.safetyFlags : []),
  ].filter(Boolean);
  phase.title = details.join(" · ");
  const track = document.querySelector(".phase-track");
  while (track.children.length < total) track.append(document.createElement("i"));
  while (track.children.length > total) track.lastElementChild.remove();
  const segmentCount = track.children.length;
  if (!segmentCount) {
    track.style.gridTemplateColumns = "none";
    track.style.gap = "0px";
    copy.style.marginLeft = "0px";
    copy.style.width = "417px";
    return;
  }
  const gap = segmentCount > 1 ? 10 : 0;
  const availableWidth = 840 - gap * (segmentCount - 1);
  const desiredCopyWidth = Math.ceil(Math.max(
    420,
    phaseCode.scrollWidth,
    phaseName.scrollWidth,
    phaseDescription.scrollWidth,
  ) + 4);
  const requestedInactiveWidth = 50;
  const maximumInactiveWidth = segmentCount > 1 ? availableWidth / segmentCount : availableWidth;
  const inactiveWidth = Math.min(requestedInactiveWidth, maximumInactiveWidth);
  const activeWidth = activeIndex > 0
    ? availableWidth - inactiveWidth * (segmentCount - 1)
    : inactiveWidth;
  const columns = Array.from({ length: segmentCount }, (_, index) => (
    activeIndex > 0 && index + 1 === activeIndex ? `${activeWidth}px` : `${inactiveWidth}px`
  ));
  track.style.gridTemplateColumns = columns.join(" ");
  track.style.gap = `${gap}px`;
  copy.style.marginLeft = activeIndex > 0
    ? `${(activeIndex - 1) * (inactiveWidth + gap)}px`
    : "0px";
  copy.style.width = activeIndex > 0 ? `${activeWidth}px` : `${Math.min(840, desiredCopyWidth)}px`;
  [...track.children].forEach((segment, index) => {
    segment.classList.toggle("completed", index + 1 < activeIndex);
    segment.classList.toggle("active", index + 1 === activeIndex);
  });
}

function setStatus(element, status, fallback = "idle") {
  const normalized = String(status || fallback).toLowerCase();
  element.className = `status ${normalized}`;
  element.replaceChildren(document.createElement("i"), document.createTextNode(normalized.toUpperCase()));
}

function applyToolVisual(image, tool, baseClass = "") {
  image.className = [baseClass, "instrument-visual", tool.visualClass].filter(Boolean).join(" ");
  image.style.transform = tool.transform || "none";
}

function setArmToolImage(card, tool, isOverview, showTool = true) {
  let image = card.querySelector(":scope > img.instrument-visual");
  if (!showTool || !tool.image) {
    const images = typeof card.querySelectorAll === "function"
      ? [...card.querySelectorAll(":scope > img.instrument-visual")]
      : image ? [image] : [];
    images.forEach((candidate) => {
      candidate.hidden = true;
      candidate.removeAttribute("src");
      candidate.removeAttribute("alt");
      if (typeof candidate.remove === "function") candidate.remove();
    });
    if (!images.length && image) {
      image.hidden = true;
      image.removeAttribute("src");
      image.removeAttribute("alt");
    }
    return;
  }
  if (!image) {
    image = document.createElement("img");
    card.append(image);
  }
  image.hidden = false;
  image.src = tool.image;
  image.alt = tool.name;
  applyToolVisual(image, tool, isOverview ? "forceps-large" : "focus-tool-image");
}

function renderArm(number) {
  const arm = state.arms[number] || { status: "unknown", toolId: "none" };
  const tool = getTool(arm.toolId);
  [`.arm-${number}`, `.focus-arm-${number}`].forEach((selector, index) => {
    const card = document.querySelector(selector);
    if (!card) return;
    setStatus(card.querySelector(".status"), arm.status, state.connected ? "unknown" : "idle");
    card.querySelector("h2").textContent = tool.name;
    card.classList.toggle("possession-holding", arm.status === "holding");
    const hand = arm.endEffectorId || (number === 1 ? "left_hand" : "right_hand");
    const details = [
      hand,
      arm.instanceId,
      Number.isFinite(arm.confidence) ? `confidence ${(arm.confidence * 100).toFixed(1)}%` : "",
      arm.evidenceStatus,
    ].filter(Boolean);
    card.title = details.join(" · ");
    const taskLabel = card.querySelector(".task-label");
    if (taskLabel) {
      taskLabel.textContent = number === 1 ? "RETRIEVE" : "DELIVER";
    }
    const showTool = arm.toolId && arm.toolId !== "none"
      && String(tool.name || "").toUpperCase() !== "NONE"
      && (activeSettings.mode !== "ros" || arm.status === "holding");
    card.classList.toggle("active-card", Boolean(showTool));
    card.classList.toggle("task-active", Boolean(showTool));
    card.classList.toggle("has-instrument-image", Boolean(showTool && tool.image));
    setArmToolImage(card, tool, index === 0, showTool);
  });
}

function renderProcedure() {
  const procedure = document.querySelector(".procedure");
  procedure.querySelector(":scope > div > strong").textContent = state.procedure.name;
  const values = procedure.querySelectorAll("dd");
  values[0].textContent = state.procedure.targetSite;
  values[1].textContent = state.procedure.approach;
  const focusProcedure = document.querySelector(".focus-info > span:first-child strong");
  if (focusProcedure) focusProcedure.textContent = state.procedure.name;
  const surgeonState = state.surgeon || {};
  const rawSurgeonName = String(surgeonState.name || "").trim();
  const unavailableNames = new Set(["", "-", "—", "none", "unknown", "waiting for catalog"]);
  const surgeonName = unavailableNames.has(rawSurgeonName.toLowerCase())
    ? "Prof. Sung"
    : rawSurgeonName;
  const surgeonDepartment = String(surgeonState.department || "").trim() || "ENT";
  const surgeonImage = surgeonState.image || assets.surgeon;
  const surgeon = document.querySelector(".surgeon");
  surgeon.querySelector("img").src = surgeonImage;
  surgeon.querySelector("img").alt = surgeonName;
  surgeon.querySelector("strong").textContent = surgeonName;
  surgeon.querySelector("small").textContent = surgeonDepartment;
  const focusDoctor = document.querySelector(".focus-doctor");
  focusDoctor.querySelector("img").src = surgeonImage;
  focusDoctor.querySelector("img").alt = surgeonName;
  focusDoctor.querySelector("strong").textContent = surgeonName;
  focusDoctor.lastChild.textContent = `  ${surgeonDepartment}`;
}

function renderInstrumentList(card, rows, emptyLabel) {
  const list = card.querySelector(".flow-list");
  const count = card.querySelector(".flow-count");
  if (count) count.textContent = rows.length ? String(rows.length) : "";
  const cardLabel = card.querySelector(".in-use");
  if (cardLabel) cardLabel.textContent = rows.length > 1 ? `IN USE · ${rows.length}` : "IN USE";
  const items = rows.map((row) => {
    const tool = getTool(row.toolId);
    const item = document.createElement("div");
    item.className = "flow-tool-row";
    const copy = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = tool.name;
    copy.append(name);
    item.append(copy);
    if (tool.image) {
      const image = document.createElement("img");
      image.src = tool.image;
      image.alt = tool.name;
      applyToolVisual(image, tool, "flow-tool-image");
      item.classList.add("has-image");
      item.append(image);
    }
    return item;
  });
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "flow-empty";
    empty.textContent = emptyLabel;
    items.push(empty);
  }
  list.replaceChildren(...items);
}

function renderToolFlow() {
  const fallback = state.retrieval || {};
  const flow = state.instrumentFlow || {
    inUse: fallback.inUseToolId && fallback.inUseToolId !== "none"
      ? [{ toolId: fallback.inUseToolId, state: "in_use" }]
      : [],
    mayo: fallback.retrievedToolId && fallback.retrievedToolId !== "none"
      ? [{ toolId: fallback.retrievedToolId, state: "awaiting_retrieval" }]
      : [],
  };
  renderInstrumentList(document.querySelector(".tool-card.current"), flow.inUse || [], "NONE");
  renderInstrumentList(document.querySelector(".tool-card.previous"), flow.mayo || [], "NONE");
}

function renderPredictions() {
  const predictions = Array.isArray(state.predictions) ? state.predictions.slice(0, 3) : [];
  const dock = document.querySelector(".predicted-dock");
  const rankTabs = dock.querySelector(".rank-tabs");
  const rankButtons = rankTabs.querySelectorAll("button");
  const arrows = dock.querySelectorAll(".prediction-arrow");
  const cards = document.querySelectorAll(".prediction-mini");
  const activeIndex = predictions.reduce((bestIndex, prediction, index) => {
    if (bestIndex < 0) return index;
    const confidence = Number(prediction?.confidence);
    const bestConfidence = Number(predictions[bestIndex]?.confidence);
    return confidence > bestConfidence ? index : bestIndex;
  }, -1);
  dock.style.setProperty("--prediction-count", "3");
  dock.classList.toggle("empty", predictions.length === 0);
  rankTabs.hidden = false;
  arrows.forEach((arrow) => { arrow.hidden = false; });
  rankButtons.forEach((button, index) => {
    const prediction = predictions[index];
    button.hidden = false;
    button.textContent = String(prediction?.rank || index + 1);
    button.classList.toggle("active", index === activeIndex && Boolean(prediction));
  });
  cards.forEach((card, index) => {
    const prediction = predictions[index];
    if (!prediction) {
      const empty = document.createElement("strong");
      empty.textContent = "NONE";
      card.replaceChildren(empty);
      card.hidden = false;
      card.classList.add("empty");
      return;
    }
    card.hidden = false;
    card.classList.remove("empty");
    const tool = getTool(prediction.toolId);
    const name = document.createElement("strong");
    name.textContent = tool.name;
    const confidence = document.createElement("span");
    confidence.textContent = `${Number(prediction.confidence).toFixed(1)} %`;
    const children = [];
    if (index === activeIndex && prediction.arm && prediction.status) {
      const status = document.createElement("small");
      const arm = document.createElement("i");
      arm.textContent = prediction.arm === 1 ? "L" : "R";
      status.append(arm, document.createTextNode(` ${String(prediction.status).toUpperCase()}`));
      children.push(status);
    }
    card.replaceChildren(...children, name, confidence);
  });
  const first = predictions[0];
  const focusPrediction = document.querySelector(".focus-predicted");
  focusPrediction.hidden = false;
  if (first) {
    focusPrediction.querySelector("small").textContent = "1st Predicted";
    focusPrediction.querySelector("strong").textContent = getTool(first.toolId).name;
  } else {
    focusPrediction.querySelector("small").textContent = "1st Predicted";
    focusPrediction.querySelector("strong").textContent = "NONE";
  }
}

function renderVoice() {
  const card = document.querySelector(".voice-card");
  const status = state.voice?.status || "listening";
  const text = String(state.voice?.text || "").trim();
  card.dataset.status = status;
  card.querySelector("strong").textContent = text || "Listening...";
  card.querySelector(".listening").hidden = status !== "listening" && Boolean(text);
}

function renderDockArm(number) {
  const arm = state.arms[number] || { status: "unknown" };
  const summary = document.querySelector(`.arm${number}-control .dock-arm-summary`);
  if (!summary) return;
  setStatus(summary.querySelector(".status"), arm.status);
  summary.querySelector("h3").textContent = number === 3 ? "Retraction" : number === 4 ? "Suction" : "NONE";
}

function renderConnection() {
  const indicator = document.querySelector(".connection");
  // The markup starts hidden to avoid showing the placeholder before the
  // runtime configuration is applied. Every subsequent render has an
  // authoritative state, so keep that state visible to the operator instead
  // of leaving the connection badge permanently hidden.
  indicator.hidden = false;
  const labels = {
    simulation: "SIMULATION",
    dummy: "DUMMY DATA",
    connecting: "CONNECTING",
    reconnecting: "RECONNECTING",
    waiting: "GATEWAY WAIT",
    live: "LIVE",
    idle: "ROS IDLE",
    degraded: "HEALTH WARN",
    "contract-mismatch": "CONTRACT ERROR",
    stale: "STALE",
    error: "ERROR",
    offline: "OFFLINE",
    stopped: "STOPPED",
  };
  indicator.dataset.state = state.connectionState;
  indicator.querySelector("span").textContent = labels[state.connectionState] || state.connectionState.toUpperCase();
  indicator.querySelector("strong").textContent = ["simulation", "dummy"].includes(state.connectionState)
    ? "LOCAL FILE"
    : "ROS BRIDGE";
  indicator.title = state.connectionDetail || "";
}

function renderDashboard() {
  renderPhase();
  renderProcedure();
  renderArm(1);
  renderArm(2);
  renderToolFlow();
  renderPredictions();
  renderVoice();
  renderDockArm(3);
  renderDockArm(4);
  renderConnection();
  renderTime();
}

function setView(view) {
  state.view = view;
  document.querySelector(".app-shell").classList.toggle("field-focus", view === "focus");
  document.querySelector(".surgical-view").classList.toggle("focused", view === "focus");
  document.querySelectorAll(".view-tabs button").forEach((button) => {
    const selected = button.dataset.view === view;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
  });
  renderActiveCamera();
}

function applyRobotState(robotState) {
  if (!robotState || typeof robotState !== "object") return;
  if (Object.prototype.hasOwnProperty.call(robotState, "elapsedSeconds")) {
    const elapsed = Number(robotState.elapsedSeconds);
    if (!Number.isFinite(elapsed) || elapsed < 0) return;
    state.elapsedAvailable = true;
  }
  deepMerge(state, robotState);
  renderDashboard();
}

function setControlsReadOnly(readOnly) {
  document.querySelectorAll(".control-dock button").forEach((button) => {
    button.disabled = readOnly;
    button.setAttribute("aria-disabled", String(readOnly));
  });
}

function setConnectionState(connectionState, detail = "") {
  const previousState = state.connectionState;
  state.connectionState = connectionState;
  state.connectionDetail = detail;
  if (previousState !== connectionState) {
    diagnostics.record(
      ["error", "contract-mismatch", "stale"].includes(connectionState) ? "warn" : "info",
      "connection.state_changed",
      { previousState, connectionState },
    );
  }
  renderConnection();
}

function releaseCameraObjectUrl(view) {
  const frame = rosRuntime.cameraFrames[view];
  if (!frame) return;
  if (frame.objectUrl) URL.revokeObjectURL(frame.objectUrl);
  frame.objectUrl = null;
}

function cameraFrameStaleAfterMs() {
  return PUBLIC_CONTRACT.cameraStaleAfterMs;
}

async function prepareCameraFrame(frame) {
  let objectUrl = null;
  const src = frame.dataUrl || (() => {
    objectUrl = URL.createObjectURL(new Blob([frame.bytes], { type: frame.mimeType }));
    return objectUrl;
  })();
  const image = new Image();
  image.decoding = "async";
  try {
    const loaded = new Promise((resolve, reject) => {
      image.addEventListener("load", resolve, { once: true });
      image.addEventListener("error", () => reject(new Error("Camera frame decode failed")), { once: true });
    });
    image.src = src;
    if (typeof image.decode === "function") {
      try {
        await image.decode();
      } catch {
        await loaded;
      }
    } else {
      await loaded;
    }
    return { image, src, objectUrl };
  } catch (error) {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    throw error;
  }
}

function disposePreparedCameraFrame(prepared) {
  if (prepared?.objectUrl) URL.revokeObjectURL(prepared.objectUrl);
}

function presentCameraFrame(frame, prepared) {
  if (!prepared?.src) return false;
  releaseCameraObjectUrl("overview");
  sharedFlirFrame.src = prepared.src;
  sharedFlirFrame.objectUrl = prepared.objectUrl || null;
  sharedFlirFrame.presentedAt = Date.now();
  sharedFlirFrame.frameId = frame.frameId || "";
  renderActiveCamera();
  return true;
}

function recordCameraPlayoutEvent(event) {
  const type = String(event?.type || "event");
  const level = type === "decode.failed" || type === "present.failed"
    ? "warn"
    : type === "frame.dropped" || type === "playout.underflow"
      ? "debug"
      : "debug";
  diagnostics.record(level, `camera.playout.${type}`, {
    topic: PUBLIC_TOPIC_NAMES.flirCamera,
    configuredMode: rosRuntime.cameraPlayoutMode,
    ...event,
  });
}

function destroyCameraPlayout(reason = "destroy") {
  if (!rosRuntime.cameraPlayout) return;
  rosRuntime.cameraPlayout.destroy();
  rosRuntime.cameraPlayout = null;
  diagnostics.record("info", "camera.playout.destroyed", {
    topic: PUBLIC_TOPIC_NAMES.flirCamera,
    reason,
  });
}

function configureCameraPlayout(cameraStreams = {}, cameraDefinition = null) {
  destroyCameraPlayout("reconfigure");
  if (cameraStreams.enabled === false || !cameraDefinition) return;
  rosRuntime.cameraPlayoutMode = normalizedCameraPlayoutMode(cameraStreams.playoutMode);
  rosRuntime.cameraThrottleRateMs = Number(cameraDefinition.throttle_rate) || 100;
  rosRuntime.cameraPlayout = createCameraPlayout({
    mode: rosRuntime.cameraPlayoutMode,
    throttleMs: rosRuntime.cameraThrottleRateMs,
    decode: prepareCameraFrame,
    present: presentCameraFrame,
    disposeDecoded: disposePreparedCameraFrame,
    onEvent: recordCameraPlayoutEvent,
  });
  diagnostics.record("info", "camera.playout.configured", {
    topic: PUBLIC_TOPIC_NAMES.flirCamera,
    mode: rosRuntime.cameraPlayoutMode,
    throttleRateMs: rosRuntime.cameraThrottleRateMs,
  });
}

function renderActiveCamera() {
  const camera = document.querySelector(".surgery-image");
  const view = state.view === "focus" ? "focus" : "overview";
  const frame = rosRuntime.cameraFrames[view];
  camera.dataset.view = view;
  camera.dataset.topic = frame?.topic || "";

  if (["simulation", "dummy"].includes(state.connectionState)) {
    camera.src = assets.surgeryFallback;
    camera.alt = "수술 부위 카메라 화면";
    camera.hidden = false;
    camera.classList.remove("stream-stale");
    return;
  }

  // Freshness is diagnostic-only for the camera. Once a valid frame has been
  // presented, keep it visible across transient gateway, health, frame, and
  // transport gaps. Semantic boundaries clear the shared frame explicitly.
  if (cameraMayRender(view)) {
    camera.src = frame.src;
    camera.alt = "ROS FLIR 수술 필드 실시간 영상";
    camera.hidden = false;
    camera.classList.remove("stream-stale");
    return;
  }

  camera.removeAttribute("src");
  camera.alt = "ROS FLIR 영상 대기";
  camera.hidden = true;
  camera.classList.add("stream-stale");
}

function clearCameraFrame(view, {
  render = true,
  reason = "cleared",
  resetPlayout = true,
} = {}) {
  const frame = rosRuntime.cameraFrames[view];
  if (!frame) return;
  if (resetPlayout) rosRuntime.cameraPlayout?.reset({ reason });
  const hadFrame = Boolean(frame.src || frame.lastSeenAt);
  releaseCameraObjectUrl(view);
  frame.src = "";
  frame.lastSeenAt = 0;
  frame.presentedAt = 0;
  frame.sourceTimestampMs = null;
  frame.frameId = "";
  frame.sourceDeltaMs = null;
  frame.receiveDeltaMs = null;
  frame.observedPlaybackRate = null;
  if (hadFrame) diagnostics.record("info", "camera.cleared", { topic: frame.topic, reason });
  if (render && state.view === view) renderActiveCamera();
}

function clearAllCameraFrames({ restoreFallback = false, reason = "cleared" } = {}) {
  rosRuntime.cameraPlayout?.reset({ reason });
  const clearedFrames = new Set();
  Object.keys(rosRuntime.cameraFrames).forEach((view) => {
    const frame = rosRuntime.cameraFrames[view];
    if (clearedFrames.has(frame)) return;
    clearedFrames.add(frame);
    clearCameraFrame(view, { render: false, reason, resetPlayout: false });
  });
  if (restoreFallback) {
    const camera = document.querySelector(".surgery-image");
    camera.src = assets.surgeryFallback;
    camera.alt = "수술 부위 카메라 화면";
    camera.hidden = false;
    camera.classList.remove("stream-stale");
    return;
  }
  renderActiveCamera();
}

function resetRosDisplay({
  keepProcedure = false,
  keepCatalog = false,
  clearCamera = true,
} = {}) {
  clearDirectTeaching("ros_display_reset");
  const retainedPhaseTotal = keepCatalog ? Math.max(0, Number(state.phase.total) || 0) : 0;
  state.phase = {
    id: "",
    code: "—",
    index: 0,
    total: retainedPhaseTotal,
    name: keepCatalog ? "Waiting for procedure" : "Waiting for catalog",
    description: "",
    uncertain: true,
    waitingForUpdate: true,
    confidence: null,
    executionState: "",
    evidenceStatus: "",
    safetyFlags: [],
  };
  if (!keepProcedure) {
    state.procedure = { name: "Waiting for catalog", targetSite: "—", approach: "—" };
    state.surgeon = { name: "—", department: "", image: assets.surgeon };
  }
  state.arms = {
    1: { status: "unknown", toolId: "none", endEffectorId: "left_hand" },
    2: { status: "unknown", toolId: "none", endEffectorId: "right_hand" },
    3: { status: "unknown", toolName: "Retraction" },
    4: { status: "unknown", toolName: "Suction" },
  };
  state.instrumentFlow = { inUse: [], mayo: [] };
  state.retrieval = { retrievedToolId: "none", location: "MAYO", inUseToolId: "none" };
  state.predictions = [];
  state.voice = { status: "listening", text: "Listening..." };
  state.elapsedAvailable = false;
  state.elapsedSource = "waiting";
  if (!keepCatalog) clearDynamicToolRegistry();
  if (clearCamera) clearAllCameraFrames({ reason: "ros_display_reset" });
  renderDashboard();
}

function markTopicWaiting(topic) {
  switch (topic) {
    case PUBLIC_TOPIC_NAMES.context:
      state.phase.waitingForUpdate = true;
      renderPhase();
      break;
    case PUBLIC_TOPIC_NAMES.health:
      break;
    default:
      return;
  }
}

function gatewayIsFresh() {
  const staleAfterMs = Number(runtimeConfig.rosbridge?.gatewayStaleAfterMs)
    || PUBLIC_CONTRACT.snapshotStaleAfterMs;
  return Boolean(
    rosRuntime.gatewayLastSeenAt
    && Date.now() - rosRuntime.gatewayLastSeenAt <= staleAfterMs,
  );
}

function topicIsFresh(topic, now = Date.now()) {
  const receivedAt = rosRuntime.lastSeenByTopic.get(topic) || 0;
  return Boolean(receivedAt && now - receivedAt <= PUBLIC_CONTRACT.snapshotStaleAfterMs);
}

function healthIsFresh() {
  return topicIsFresh(PUBLIC_TOPIC_NAMES.health);
}

function cameraSourceIsBlocked(view, health = rosRuntime.health) {
  if (!health) return false;
  const frameTopic = rosRuntime.cameraFrames[view]?.topic || "";
  const sourceNeedle = frameTopic === PUBLIC_TOPIC_NAMES.flirCamera ? "flir" : "cam4";
  const unavailableSources = Array.isArray(health.unavailableSources) ? health.unavailableSources : [];
  const staleSources = Array.isArray(health.staleSources) ? health.staleSources : [];
  return [...unavailableSources, ...staleSources]
    .some((source) => String(source || "").toLowerCase().includes(sourceNeedle));
}

function applyCameraHealth(health) {
  rosRuntime.health = health;
}

function cameraMayRender(view) {
  return rosRuntime.contractCompatible
    && rosRuntime.procedureActive
    && Boolean(rosRuntime.cameraFrames[view]?.src);
}

function applyCameraFrame(topic, message, definition) {
  // Reception is independent from visibility. The playout controller follows
  // the ROS source clock and presents only due frames; renderActiveCamera()
  // only hides the last frame at explicit procedure/contract/session boundaries.
  const targetViews = Object.entries(rosRuntime.cameraFrames)
    .filter(([, frame]) => frame.topic === topic)
    .map(([view]) => view);
  if (!targetViews.length) {
    diagnostics.record("warn", "camera.rejected", { topic, reason: "unconfigured_topic" });
    return;
  }
  if (
    rosRuntime.gatewayLastSeenAt === 0
    || rosRuntime.contractCompatible === false
    || rosRuntime.procedureActive === false
  ) {
    const reason = rosRuntime.gatewayLastSeenAt === 0
      ? "gateway_not_ready"
      : rosRuntime.contractCompatible === false
        ? "contract_not_ready"
        : "procedure_inactive";
    diagnostics.record("debug", "camera.rejected", {
      topic,
      reason,
    });
    return;
  }
  const normalized = normalizeCompressedImage(message);
  if (!normalized) {
    diagnostics.record("warn", "camera.invalid", {
      topic,
      format: typeof message?.format === "string" ? message.format : "",
      dataType: typeof message?.data,
      hasHeader: Boolean(message?.header),
    });
    return;
  }
  if (!rosRuntime.cameraPlayout) {
    diagnostics.record("warn", "camera.rejected", { topic, reason: "playout_unavailable" });
    return;
  }
  const receivedAt = Date.now();
  const timing = compressedImageTiming(message);
  const sourceStampNs = cameraTimingStampNs(timing);
  const sourceTimestampMs = sourceStampNs === null ? null : timing.sourceTimestampMs;
  const previousReceivedAt = sharedFlirFrame.lastSeenAt;
  const previousSourceTimestampMs = sharedFlirFrame.sourceTimestampMs;
  const receiveDeltaMs = previousReceivedAt ? receivedAt - previousReceivedAt : null;
  const sourceDeltaMs = sourceTimestampMs !== null && previousSourceTimestampMs !== null
    ? sourceTimestampMs - previousSourceTimestampMs
    : null;
  const observedPlaybackRate = sourceDeltaMs > 0 && receiveDeltaMs > 0
    ? sourceDeltaMs / receiveDeltaMs
    : null;
  const result = rosRuntime.cameraPlayout.ingest({
    stampNs: sourceStampNs,
    sourceTimestampMs,
    mimeType: normalized.mimeType,
    dataUrl: normalized.dataUrl,
    bytes: normalized.bytes,
    frameId: timing?.frameId || "",
    receivedAt,
  });
  if (!result.accepted) {
    diagnostics.record("debug", "camera.rejected", { topic, reason: result.reason, sourceTimestampMs });
    return;
  }

  if (rosRuntime.staleTopics?.delete(topic)) {
    diagnostics.record("info", "camera.recovered", {
      topic,
      staleDurationMs: previousReceivedAt ? Math.max(0, receivedAt - previousReceivedAt) : null,
    });
  }
  sharedFlirFrame.lastSeenAt = receivedAt;
  sharedFlirFrame.sourceTimestampMs = sourceTimestampMs;
  sharedFlirFrame.frameId = timing?.frameId || "";
  sharedFlirFrame.sourceDeltaMs = sourceDeltaMs;
  sharedFlirFrame.receiveDeltaMs = receiveDeltaMs;
  sharedFlirFrame.observedPlaybackRate = observedPlaybackRate;
  const playout = rosRuntime.cameraPlayout.snapshot();
  diagnostics.record("debug", "camera.frame", {
    topic,
    format: normalized.mimeType,
    byteLength: normalized.bytes?.byteLength ?? null,
    frameId: sharedFlirFrame.frameId,
    sourceTimestampMs,
    sourceDeltaMs,
    receiveDeltaMs,
    observedPlaybackRate: observedPlaybackRate === null
      ? null
      : Number(observedPlaybackRate.toFixed(3)),
    throttleRateMs: Number(definition?.throttle_rate) || null,
    playoutMode: result.mode,
    playoutReason: result.reason,
    queuedFrames: playout.queuedFrames,
    targetDelayMs: playout.targetDelayMs,
  });
  if (result.reason === "rewind") {
    diagnostics.record("info", "camera.timestamp_reset", {
      topic,
      previousSourceTimestampMs,
      sourceTimestampMs,
      sourceDeltaMs,
    });
  } else if (result.reason === "forward_jump") {
    diagnostics.record("warn", "camera.timestamp_gap", { topic, sourceDeltaMs, receiveDeltaMs });
  }
}

function syncObservedElapsed(meta) {
  if (meta.contractCompatible !== true) {
    rosRuntime.observedRunId = "";
    rosRuntime.activeObservedAt = 0;
    state.elapsedSeconds = 0;
    state.elapsedAvailable = false;
    state.elapsedSource = "waiting";
    return;
  }

  const runId = String(meta.procedureRunId || "");
  if (meta.procedureActive !== true) {
    rosRuntime.observedRunId = runId;
    rosRuntime.activeObservedAt = 0;
    state.elapsedSeconds = 0;
    state.elapsedAvailable = true;
    state.elapsedSource = "idle";
    return;
  }

  const scopeStartsNewObservation = ["initial", "gateway", "run"].includes(meta.resetScope);
  if (
    !rosRuntime.activeObservedAt
    || rosRuntime.observedRunId !== runId
    || scopeStartsNewObservation
  ) {
    rosRuntime.observedRunId = runId;
    rosRuntime.activeObservedAt = Date.now();
  }
  state.elapsedSeconds = Math.max(0, Math.floor((Date.now() - rosRuntime.activeObservedAt) / 1000));
  state.elapsedAvailable = true;
  state.elapsedSource = "observed";
}

function noteGatewayHeartbeat(meta) {
  rosRuntime.gatewayLastSeenAt = Date.now();
  const previous = rosRuntime.lastGatewayMeta;
  const current = {
    gatewayInstanceId: String(meta.gatewayInstanceId || ""),
    procedureRunId: String(meta.procedureRunId || ""),
    procedureType: String(meta.procedureType || ""),
    procedureActive: meta.procedureActive === true,
    catalogVersion: String(meta.catalogVersion || ""),
    schemaVersion: String(meta.schemaVersion || ""),
    interfaceVersion: String(meta.interfaceVersion || ""),
    contractCompatible: meta.contractCompatible === true,
    revision: Number.isFinite(Number(meta.revision)) ? Number(meta.revision) : null,
  };
  const changedFields = previous
    ? Object.keys(current).filter((key) => current[key] !== previous[key])
    : Object.keys(current);
  if (!previous || changedFields.length) {
    diagnostics.record("info", previous ? "gateway.changed" : "gateway.first_heartbeat", {
      changedFields,
      previous,
      current,
      resetScope: meta.resetScope || "",
    });
  }
  if (previous && previous.procedureRunId !== current.procedureRunId) {
    diagnostics.record("info", "procedure.run_changed", {
      previousProcedureRunId: previous.procedureRunId,
      procedureRunId: current.procedureRunId,
      revision: current.revision,
    });
  }
  if (previous && previous.procedureActive !== current.procedureActive) {
    diagnostics.record("info", "procedure.active_changed", {
      previousActive: previous.procedureActive,
      procedureActive: current.procedureActive,
      procedureRunId: current.procedureRunId,
      revision: current.revision,
    });
  }
  rosRuntime.lastGatewayMeta = current;
  if (!state.connected) return;
  rosRuntime.contractCompatible = meta.contractCompatible === true;
  rosRuntime.procedureActive = meta.procedureActive === true;

  if (meta.resetScope) {
    const keepCatalog = meta.resetScope === "run";
    const cameraBoundaryChanged = !previous
      || previous.gatewayInstanceId !== current.gatewayInstanceId
      || previous.procedureRunId !== current.procedureRunId
      || previous.procedureActive !== current.procedureActive;
    rosRuntime.health = null;
    resetRosDisplay({
      keepProcedure: keepCatalog,
      keepCatalog,
      clearCamera: cameraBoundaryChanged,
    });
    rosRuntime.lastSeenByTopic.clear();
    rosRuntime.staleTopics.clear();
    rosRuntime.lastSeenByTopic.set(PUBLIC_TOPIC_NAMES.gatewayInfo, rosRuntime.gatewayLastSeenAt);
  }

  syncObservedElapsed(meta);
  renderTime();

  if (!rosRuntime.contractCompatible) {
    resetRosDisplay();
    setConnectionState(
      "contract-mismatch",
      `Expected schema ${PUBLIC_CONTRACT.schemaVersion} / interface ${PUBLIC_CONTRACT.interfaceVersion}; received ${meta.schemaVersion || "—"} / ${meta.interfaceVersion || "—"}`,
    );
    return;
  }

  if (!rosRuntime.procedureActive) {
    clearAllCameraFrames({ reason: "procedure_inactive" });
    setConnectionState("idle", rosRuntime.client?.url || "");
    return;
  }

  if (!healthIsFresh()) {
    setConnectionState("degraded", "Health snapshot waiting");
    return;
  }

  const health = rosRuntime.health;
  const degraded = health && (
    health.healthy !== true
    || health.unavailableSources.length
    || health.staleSources.length
    || health.errorCodes.length
  );
  setConnectionState(degraded ? "degraded" : "live", rosRuntime.client?.url || "");
}

function handleRosMessage(topic, message, definition) {
  const receivedAt = Date.now();
  rosRuntime.lastAnyMessageAt = receivedAt;
  rosRuntime.livenessReconnectRequested = false;

  if (definition.kind === "camera") {
    applyCameraFrame(topic, message, definition);
    return;
  }

  if (rosRuntime.customMapper && topic !== PUBLIC_TOPIC_NAMES.gatewayInfo) {
    if (!rosRuntime.gatewayLastSeenAt || !rosRuntime.contractCompatible) {
      recordTopicResult(topic, message, {
        accepted: false,
        reason: rosRuntime.gatewayLastSeenAt ? "contract_not_ready" : "gateway_not_ready",
      });
      return;
    }
    let patch;
    try {
      patch = rosRuntime.customMapper(topic, message, state);
    } catch (error) {
      recordTopicResult(topic, message, {
        accepted: false,
        reason: "custom_mapper_error",
        details: { error: error instanceof Error ? error.message : String(error) },
      });
      return;
    }
    if (patch) {
      recordTopicResult(topic, message, { accepted: true });
      rosRuntime.lastSeenByTopic.set(topic, receivedAt);
      rosRuntime.staleTopics.delete(topic);
      applyRobotState(patch);
    } else {
      recordTopicResult(topic, message, { accepted: false, reason: "custom_mapper_no_patch" });
    }
    return;
  }

  let result;
  try {
    result = rosRuntime.scenarioMapper.map(topic, message);
  } catch (error) {
    recordTopicResult(topic, message, {
      accepted: false,
      reason: "mapper_error",
      details: { error: error instanceof Error ? error.message : String(error) },
    });
    return;
  }
  if (!result) {
    const rejection = rosRuntime.scenarioMapper.getLastRejection?.();
    recordTopicResult(topic, message, {
      accepted: false,
      reason: rejection?.reason || "validation_failed",
      details: rejection?.details || {},
    });
    return;
  }
  if (result.meta?.gatewayHeartbeat) {
    recordTopicResult(topic, message, { accepted: true });
    rosRuntime.lastSeenByTopic.set(topic, receivedAt);
    rosRuntime.staleTopics.delete(topic);
    noteGatewayHeartbeat(result.meta);
  } else {
    if (!rosRuntime.gatewayLastSeenAt || !rosRuntime.contractCompatible) {
      recordTopicResult(topic, message, {
        accepted: false,
        reason: rosRuntime.gatewayLastSeenAt ? "contract_not_ready" : "gateway_not_ready",
      });
      return;
    }
    recordTopicResult(topic, message, { accepted: true });
    rosRuntime.lastSeenByTopic.set(topic, receivedAt);
    rosRuntime.staleTopics.delete(topic);
  }
  if (!rosRuntime.gatewayLastSeenAt || !rosRuntime.contractCompatible) return;
  if (result.meta?.health) {
    const health = result.meta.health;
    const healthFingerprint = JSON.stringify(health);
    if (healthFingerprint !== rosRuntime.lastHealthFingerprint) {
      diagnostics.record(
        health.healthy === true ? "info" : "warn",
        "health.changed",
        health,
      );
      rosRuntime.lastHealthFingerprint = healthFingerprint;
    }
    applyCameraHealth(health);
    renderActiveCamera();
    const degraded = health.healthy !== true
      || health.unavailableSources.length
      || health.staleSources.length
      || health.errorCodes.length;
    if (rosRuntime.procedureActive && gatewayIsFresh()) {
      setConnectionState(degraded ? "degraded" : "live", rosRuntime.client?.url || "");
    }
  }
  if (result.patch?.phase) {
    const phaseResolved = Number(result.patch.phase.index) > 0;
    if (topic === PUBLIC_TOPIC_NAMES.context) {
      result.patch.phase.waitingForUpdate = !phaseResolved;
    } else if (topic === PUBLIC_TOPIC_NAMES.catalog) {
      result.patch.phase.waitingForUpdate = !phaseResolved
        || !topicIsFresh(PUBLIC_TOPIC_NAMES.context, receivedAt);
    }
  }
  registerRosTools(result.tools);
  if (result.patch) applyRobotState(result.patch);
  else if (result.tools.length) renderDashboard();
}

function handleRosStatus(status) {
  const wasConnected = state.connected;
  const statusLevel = status.state === "error"
    ? "error"
    : ["closed", "reconnecting"].includes(status.state)
      ? "warn"
      : "info";
  diagnostics.record(statusLevel, "connection.status", {
    state: status.state,
    url: status.url,
    retryAttempt: Number(status.retryAttempt) || 0,
    nextRetryMs: Number(status.nextRetryMs) || null,
    reason: typeof status.reason === "string" ? status.reason : "",
    error: typeof status.error === "string" ? status.error : "",
    closeCode: Number.isInteger(status.closeCode) ? status.closeCode : null,
    closeReason: typeof status.closeReason === "string" ? status.closeReason : "",
    wasClean: status.wasClean === true,
  });
  if (status.error === "connection timeout") {
    diagnostics.record("error", "connection.timeout", {
      url: status.url,
      retryAttempt: Number(status.retryAttempt) || 0,
    });
  }
  const retryDetail = status.nextRetryMs
    ? `${status.url} · retry in ${(status.nextRetryMs / 1000).toFixed(1)}s`
    : status.error
      ? `${status.url} · ${status.error}`
      : status.url;

  if (status.state === "connected") {
    state.connected = true;
    rosRuntime.gatewayLastSeenAt = 0;
    rosRuntime.lastAnyMessageAt = 0;
    rosRuntime.transportConnectedAt = Date.now();
    rosRuntime.livenessReconnectRequested = false;
    setConnectionState("waiting", retryDetail);
    return;
  }

  state.connected = false;
  if (wasConnected && status.state !== "connecting") {
    state.phase.waitingForUpdate = true;
    renderPhase();
  }
  if (["connecting", "reconnecting", "error", "stopped"].includes(status.state)) {
    setConnectionState(status.state, retryDetail);
  }
}

function startGatewayFreshnessMonitor(staleAfterMs, topicSilenceTimeoutMs = 3000) {
  if (rosRuntime.gatewayTimer) clearInterval(rosRuntime.gatewayTimer);
  rosRuntime.gatewayTimer = setInterval(() => {
    if (!state.connected) return;
    const now = Date.now();
    const lastTrafficAt = rosRuntime.lastAnyMessageAt || rosRuntime.transportConnectedAt;
    if (lastTrafficAt && now - lastTrafficAt >= topicSilenceTimeoutMs) {
      if (!rosRuntime.livenessReconnectRequested) {
        rosRuntime.livenessReconnectRequested = true;
        diagnostics.record("warn", "connection.liveness_timeout", {
          ageMs: now - lastTrafficAt,
          timeoutMs: topicSilenceTimeoutMs,
          lastAnyMessageAt: rosRuntime.lastAnyMessageAt || null,
          transportConnectedAt: rosRuntime.transportConnectedAt || null,
        });
        const reconnectRequested = rosRuntime.client?.forceReconnect("all_topics_silent") === true;
        if (!reconnectRequested) setConnectionState("offline", "All subscribed topics timed out");
      }
      return;
    }
    if (!rosRuntime.gatewayLastSeenAt) {
      if (state.connectionState !== "waiting") setConnectionState("waiting", rosRuntime.client?.url || "");
      return;
    }
    const gatewayTimedOut = now - rosRuntime.gatewayLastSeenAt > staleAfterMs;
    if (gatewayTimedOut) {
      if (!rosRuntime.staleTopics.has(PUBLIC_TOPIC_NAMES.gatewayInfo)) {
        rosRuntime.staleTopics.add(PUBLIC_TOPIC_NAMES.gatewayInfo);
        diagnostics.record("warn", "gateway.timeout", {
          topic: PUBLIC_TOPIC_NAMES.gatewayInfo,
          ageMs: now - rosRuntime.gatewayLastSeenAt,
          staleAfterMs,
          procedureRunId: rosRuntime.lastGatewayMeta?.procedureRunId || "",
          procedureActive: rosRuntime.procedureActive,
        });
        state.phase.waitingForUpdate = true;
        renderPhase();
      }
      setConnectionState("degraded", "Gateway snapshot timed out; other topic traffic remains active");
    } else {
      rosRuntime.staleTopics.delete(PUBLIC_TOPIC_NAMES.gatewayInfo);
    }
    const snapshotTopics = [
      PUBLIC_TOPIC_NAMES.catalog,
      PUBLIC_TOPIC_NAMES.context,
      PUBLIC_TOPIC_NAMES.instruments,
      PUBLIC_TOPIC_NAMES.robots,
      PUBLIC_TOPIC_NAMES.robotEndEffectors,
      PUBLIC_TOPIC_NAMES.toolPredictions,
      PUBLIC_TOPIC_NAMES.speech,
      PUBLIC_TOPIC_NAMES.health,
    ];
    snapshotTopics.forEach((topic) => {
      const lastSeenAt = rosRuntime.lastSeenByTopic.get(topic) || 0;
      if (!lastSeenAt || now - lastSeenAt <= PUBLIC_CONTRACT.snapshotStaleAfterMs) return;
      if (rosRuntime.staleTopics.has(topic)) return;
      rosRuntime.staleTopics.add(topic);
      diagnostics.record("warn", "topic.stale", {
        topic,
        ageMs: now - lastSeenAt,
        staleAfterMs: PUBLIC_CONTRACT.snapshotStaleAfterMs,
        lastRevision: rosRuntime.lastRevisionByTopic.get(topic) ?? null,
      });
      markTopicWaiting(topic);
    });

    const cameraStaleMs = cameraFrameStaleAfterMs();
    if (
      sharedFlirFrame.lastSeenAt
      && now - sharedFlirFrame.lastSeenAt > cameraStaleMs
      && !rosRuntime.staleTopics.has(sharedFlirFrame.topic)
    ) {
      rosRuntime.staleTopics.add(sharedFlirFrame.topic);
      diagnostics.record("warn", "camera.timeout", {
        topic: sharedFlirFrame.topic,
        ageMs: now - sharedFlirFrame.lastSeenAt,
        staleAfterMs: cameraStaleMs,
        sourceTimestampMs: sharedFlirFrame.sourceTimestampMs,
      });
    }

    if (rosRuntime.procedureActive && !healthIsFresh()) {
      setConnectionState("degraded", "Health snapshot timed out");
    }
  }, 500);
}

function disconnectRosBridge({ restoreSimulation = false, reason = "manual" } = {}) {
  clearDirectTeaching(reason);
  diagnostics.record("info", "connection.disconnect_requested", {
    reason,
    url: rosRuntime.client?.url || "",
    generation: rosRuntime.connectionGeneration,
  });
  rosRuntime.connectionGeneration += 1;
  rosRuntime.client?.stop();
  rosRuntime.client = null;
  destroyCameraPlayout(reason);
  rosRuntime.customMapper = null;
  rosRuntime.gatewayLastSeenAt = 0;
  rosRuntime.lastAnyMessageAt = 0;
  rosRuntime.transportConnectedAt = 0;
  rosRuntime.livenessReconnectRequested = false;
  rosRuntime.lastSeenByTopic.clear();
  rosRuntime.lastRevisionByTopic.clear();
  rosRuntime.staleTopics.clear();
  rosRuntime.contractCompatible = false;
  rosRuntime.procedureActive = false;
  rosRuntime.health = null;
  rosRuntime.observedRunId = "";
  rosRuntime.activeObservedAt = 0;
  rosRuntime.lastGatewayMeta = null;
  rosRuntime.lastHealthFingerprint = "";
  rosRuntime.cameraVisibilityByView.clear();
  if (rosRuntime.gatewayTimer) clearInterval(rosRuntime.gatewayTimer);
  rosRuntime.gatewayTimer = null;
  state.connected = false;

  if (restoreSimulation) {
    clearAllCameraFrames({ restoreFallback: true, reason });
    clearDynamicToolRegistry();
    replaceState(simulationState);
    setControlsReadOnly(false);
    setConnectionState("simulation");
    renderDashboard();
  } else {
    clearAllCameraFrames({ reason });
    setConnectionState("stopped");
  }
}

function connectRosBridge(options = {}) {
  if (Object.prototype.hasOwnProperty.call(options, "subscriptions")) {
    throw new Error("Custom ROS subscriptions are not supported by the Main Layout");
  }
  const rosbridgeConfig = { ...(runtimeConfig.rosbridge || {}), ...(options.rosbridge || {}) };
  const url = options.url || rosbridgeConfig.url;
  if (!url) throw new Error("rosbridge WebSocket URL is required");

  const cameraStreams = {
    ...(runtimeConfig.rosbridge?.cameraStreams || {}),
    ...(options.cameraStreams || {}),
  };
  const subscriptions = createMainLayoutSubscriptions(cameraStreams);
  if (options.mapper !== undefined) {
    if (options.mapper !== null && typeof options.mapper !== "function") {
      throw new Error("mapper must be a function or null");
    }
  }

  diagnostics.record("info", "connection.requested", {
    url,
    connectTimeoutMs: options.connectTimeoutMs ?? rosbridgeConfig.connectTimeoutMs,
    gatewayStaleAfterMs: options.gatewayStaleAfterMs ?? rosbridgeConfig.gatewayStaleAfterMs,
    topicSilenceTimeoutMs: options.topicSilenceTimeoutMs ?? rosbridgeConfig.topicSilenceTimeoutMs,
    subscriptionCount: subscriptions.length,
    topics: subscriptions.map((definition) => definition.name),
    cameraThrottleRateMs: subscriptions.find((definition) => definition.kind === "camera")?.throttle_rate ?? null,
    cameraPlayoutMode: normalizedCameraPlayoutMode(cameraStreams.playoutMode),
  });

  if (rosRuntime.client || rosRuntime.cameraPlayout) disconnectRosBridge({ reason: "replace_connection" });
  rosRuntime.customMapper = options.mapper ?? null;

  rosRuntime.scenarioMapper = new MainLayoutScenarioMapper();
  rosRuntime.gatewayLastSeenAt = 0;
  rosRuntime.lastAnyMessageAt = 0;
  rosRuntime.transportConnectedAt = 0;
  rosRuntime.livenessReconnectRequested = false;
  rosRuntime.lastSeenByTopic.clear();
  rosRuntime.lastRevisionByTopic.clear();
  rosRuntime.staleTopics.clear();
  rosRuntime.contractCompatible = false;
  rosRuntime.procedureActive = false;
  rosRuntime.health = null;
  rosRuntime.observedRunId = "";
  rosRuntime.activeObservedAt = 0;
  rosRuntime.lastGatewayMeta = null;
  rosRuntime.lastHealthFingerprint = "";
  rosRuntime.cameraVisibilityByView.clear();
  setConnectionState("connecting", url);
  resetRosDisplay();
  setControlsReadOnly(true);
  configureCameraPlayout(
    cameraStreams,
    subscriptions.find((definition) => definition.kind === "camera") || null,
  );

  const generation = ++rosRuntime.connectionGeneration;
  let client;
  client = new RosBridgeClient({
    url,
    subscriptions,
    connectTimeoutMs: options.connectTimeoutMs ?? rosbridgeConfig.connectTimeoutMs,
    reconnect: options.reconnect || rosbridgeConfig.reconnect,
    onMessage(topic, message, definition) {
      if (generation !== rosRuntime.connectionGeneration || rosRuntime.client !== client) {
        diagnostics.record("warn", "topic.rejected", {
          ...topicEnvelope(topic, message),
          reason: "stale_connection_generation",
          generation,
          activeGeneration: rosRuntime.connectionGeneration,
        });
        return;
      }
      handleRosMessage(topic, message, definition);
    },
    onStatus(status) {
      if (generation !== rosRuntime.connectionGeneration || rosRuntime.client !== client) {
        diagnostics.record("debug", "connection.status_ignored", {
          state: status.state,
          generation,
          activeGeneration: rosRuntime.connectionGeneration,
        });
        return;
      }
      handleRosStatus(status);
    },
  });
  rosRuntime.client = client;
  startGatewayFreshnessMonitor(
    Math.max(1000, Number(options.gatewayStaleAfterMs ?? rosbridgeConfig.gatewayStaleAfterMs) || 3000),
    Math.max(1000, Number(options.topicSilenceTimeoutMs ?? rosbridgeConfig.topicSilenceTimeoutMs) || 3000),
  );
  client.start();
}

function applyDummyReplay(replay, sourceLabel = activeSettings.dummyDataFile) {
  const currentView = state.view === "focus" ? "focus" : "overview";
  disconnectRosBridge({ reason: "switch_to_dummy" });
  clearDynamicToolRegistry();
  registerRosTools(replay.tools);
  const nextState = createBlankDummyState(currentView);
  deepMerge(nextState, replay.patch);
  nextState.view = currentView;
  nextState.connected = false;
  nextState.connectionState = "dummy";
  nextState.connectionDetail = sourceLabel;
  nextState.elapsedSource = "dummy";
  replaceState(nextState);
  clearAllCameraFrames({ restoreFallback: true, reason: "dummy_mode" });
  setControlsReadOnly(false);
  renderDashboard();
  setView(currentView);
}

function validateSettingsForPage(candidate) {
  const normalized = validateSettings(candidate, defaultSettings);
  if (
    normalized.mode === "ros"
    && window.location.protocol === "https:"
    && normalized.bridgeUrl.startsWith("ws://")
  ) {
    throw new Error("HTTPS 페이지에서는 wss:// 서버 주소를 사용해 주세요.");
  }
  return normalized;
}

async function loadDummyData(file = activeSettings.dummyDataFile) {
  return fetchDummyData(file);
}

async function applySettings(
  candidate,
  { dummyPayload = null, dummySourceLabel = "" } = {},
) {
  const normalized = validateSettingsForPage(candidate);
  const applyGeneration = ++settingsApplyGeneration;
  const sourceUnchanged = normalized.mode === activeSettings.mode
    && normalized.bridgeUrl === activeSettings.bridgeUrl
    && normalized.throttleRateMs === activeSettings.throttleRateMs
    && normalized.dummyDataFile === activeSettings.dummyDataFile;
  const hasNewDummySelection = normalized.mode === "dummy"
    && Boolean(dummyPayload)
    && dummyPayload !== activeDummySelection?.payload;
  const sourceIsActive = normalized.mode === "ros"
    ? Boolean(rosRuntime.client)
    : state.connectionState === "dummy";

  if (sourceUnchanged && sourceIsActive && !hasNewDummySelection) {
    activeSettings = normalized;
    applyCameraFit(normalized.cameraFit);
    updateSettingsDisconnectButton();
    return activeSettings;
  }

  let dummyReplay = null;
  let validatedDummyPayload = null;
  if (normalized.mode === "dummy") {
    validatedDummyPayload = dummyPayload
      ? validateDummyFixture(dummyPayload)
      : await loadDummyData(normalized.dummyDataFile);
    let fallbackFixture = null;
    if (
      !validatedDummyPayload[PUBLIC_TOPIC_NAMES.catalog]
      && (dummyPayload || normalized.dummyDataFile !== defaultSettings.dummyDataFile)
    ) {
      fallbackFixture = await loadDummyData(defaultSettings.dummyDataFile);
    }
    dummyReplay = replayDummyFixture(validatedDummyPayload, { fallbackFixture });
    if (applyGeneration !== settingsApplyGeneration) return activeSettings;
  }

  activeSettings = normalized;
  applyCameraFit(normalized.cameraFit);
  if (normalized.mode === "ros") {
    activeDummySelection = null;
    connectRosBridge({
      url: normalized.bridgeUrl,
      cameraStreams: { enabled: true, throttleRateMs: normalized.throttleRateMs },
    });
  } else {
    const sourceLabel = safeText(dummySourceLabel, 180) || normalized.dummyDataFile;
    activeDummySelection = dummyPayload
      ? { name: sourceLabel, payload: validatedDummyPayload }
      : null;
    applyDummyReplay(dummyReplay, sourceLabel);
  }
  return activeSettings;
}

const settingsTriggers = [...document.querySelectorAll("[data-settings-trigger]")];
const settingsDialog = document.querySelector("#settings-dialog");
const settingsPanel = settingsDialog.querySelector(".settings-panel");
const settingsForm = document.querySelector("#settings-form");
const settingsError = document.querySelector("#settings-error");
const dummyFileInput = settingsForm.querySelector("#dummy-file-input");
let settingsReturnFocus = null;
let activeDummySelection = null;
let pendingDummySelection = null;
let dummySelectionGeneration = 0;

function renderDummyFileSelection(selection, fallbackPath) {
  const output = settingsForm.querySelector("#dummy-data-file");
  output.textContent = selection?.name || fallbackPath;
  output.title = output.textContent;
}

function fillSettingsForm(settings, { dummySelection = activeDummySelection } = {}) {
  const modeInput = settingsForm.querySelector(`input[name="connection-mode"][value="${settings.mode}"]`);
  if (modeInput) modeInput.checked = true;
  settingsForm.querySelector("#bridge-url").value = settings.bridgeUrl;
  settingsForm.querySelector("#throttle-rate").value = String(settings.throttleRateMs);
  const fitInput = settingsForm.querySelector(`input[name="camera-fit"][value="${normalizedCameraFit(settings.cameraFit)}"]`);
  if (fitInput) fitInput.checked = true;
  const output = settingsForm.querySelector("#dummy-data-file");
  output.dataset.url = settings.dummyDataFile;
  pendingDummySelection = dummySelection;
  dummyFileInput.value = "";
  renderDummyFileSelection(dummySelection, settings.dummyDataFile);
  updateSettingsFormMode();
  updateSettingsDisconnectButton();
}

function selectedSettingsMode() {
  return settingsForm.querySelector('input[name="connection-mode"]:checked')?.value || "ros";
}

function updateSettingsFormMode() {
  const mode = selectedSettingsMode();
  const serverFields = settingsForm.querySelector('[data-mode-fields="ros"]');
  const dummyFields = settingsForm.querySelector('[data-mode-fields="dummy"]');
  serverFields.querySelectorAll("input").forEach((input) => { input.disabled = mode !== "ros"; });
  serverFields.classList.toggle("inactive", mode !== "ros");
  dummyFileInput.disabled = mode !== "dummy";
  dummyFields.hidden = mode !== "dummy";
}

function updateSettingsDisconnectButton() {
  const button = document.querySelector("#settings-disconnect");
  if (!button) return;
  const canDisconnect = activeSettings.mode === "ros"
    && Boolean(rosRuntime.client);
  button.disabled = !canDisconnect;
  button.setAttribute("aria-disabled", String(!canDisconnect));
  button.title = canDisconnect
    ? "현재 ROS Bridge 연결과 자동 재접속을 중지합니다."
    : "활성 서버 연결이 없습니다.";
}

function disconnectFromSettings() {
  settingsApplyGeneration += 1;
  if (activeSettings.mode !== "ros" || !rosRuntime.client) {
    updateSettingsDisconnectButton();
    return;
  }
  disconnectRosBridge({ reason: "settings_disconnect" });
  settingsError.dataset.tone = "success";
  settingsError.textContent = "서버 연결을 해제했습니다. APPLY를 누르면 다시 연결합니다.";
  updateSettingsDisconnectButton();
  settingsForm.querySelector('button[type="submit"]').focus();
}

function openSettingsDialog() {
  settingsReturnFocus = document.activeElement;
  settingsError.textContent = "";
  settingsError.dataset.tone = "";
  fillSettingsForm(activeSettings);
  settingsDialog.hidden = false;
  settingsTriggers.forEach((trigger) => trigger.setAttribute("aria-expanded", "true"));
  settingsForm.querySelector('button[type="submit"]').disabled = false;
  settingsPanel.focus();
}

function closeSettingsDialog() {
  dummySelectionGeneration += 1;
  settingsApplyGeneration += 1;
  settingsDialog.hidden = true;
  settingsTriggers.forEach((trigger) => trigger.setAttribute("aria-expanded", "false"));
  settingsError.textContent = "";
  if (settingsReturnFocus && typeof settingsReturnFocus.focus === "function") settingsReturnFocus.focus();
  settingsReturnFocus = null;
}

function resetSettingsToDefaults() {
  dummySelectionGeneration += 1;
  settingsApplyGeneration += 1;
  fillSettingsForm(defaultSettings, { dummySelection: null });
  settingsError.dataset.tone = "success";
  settingsError.textContent = "기본 설정을 불러왔습니다. APPLY를 눌러 적용해 주세요.";
}

function downloadDiagnosticLog() {
  diagnostics.record("info", "diagnostics.exported", { entryCount: diagnostics.size });
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const blob = new Blob([diagnostics.exportJson()], { type: "application/json;charset=utf-8" });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = `surgimate-diagnostics-${timestamp}.json`;
  anchor.hidden = true;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(href), 0);
}

async function handleDummyFileSelection(event) {
  const file = event.currentTarget.files?.[0];
  if (!file) return;
  settingsApplyGeneration += 1;
  const generation = ++dummySelectionGeneration;
  const submit = settingsForm.querySelector('button[type="submit"]');
  submit.disabled = true;
  settingsError.dataset.tone = "";
  settingsError.textContent = "더미 데이터 파일을 확인하고 있습니다…";
  try {
    const payload = await readDummyDataFile(file);
    if (generation !== dummySelectionGeneration) return;
    pendingDummySelection = {
      name: safeText(file.name, 180) || "선택한 JSON 파일",
      payload,
    };
    renderDummyFileSelection(pendingDummySelection, activeSettings.dummyDataFile);
    settingsError.dataset.tone = "success";
    settingsError.textContent = "파일 검증이 완료되었습니다. APPLY를 눌러 적용해 주세요.";
  } catch (error) {
    if (generation !== dummySelectionGeneration) return;
    pendingDummySelection = activeDummySelection;
    dummyFileInput.value = "";
    renderDummyFileSelection(pendingDummySelection, activeSettings.dummyDataFile);
    settingsError.dataset.tone = "";
    settingsError.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    if (generation === dummySelectionGeneration) submit.disabled = false;
  }
}

settingsTriggers.forEach((trigger) => trigger.addEventListener("click", openSettingsDialog));
settingsForm.querySelectorAll('input[name="connection-mode"]').forEach((input) => {
  input.addEventListener("change", updateSettingsFormMode);
});
settingsDialog.querySelectorAll("[data-settings-close]").forEach((button) => {
  button.addEventListener("click", closeSettingsDialog);
});
settingsDialog.addEventListener("click", (event) => {
  if (event.target === settingsDialog) closeSettingsDialog();
});
settingsDialog.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    closeSettingsDialog();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...settingsDialog.querySelectorAll("button:not(:disabled), input:not(:disabled)")]
    .filter((element) => !element.hidden);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});
settingsForm.querySelector("#settings-reset").addEventListener("click", resetSettingsToDefaults);
settingsForm.querySelector("#diagnostics-download").addEventListener("click", downloadDiagnosticLog);
settingsForm.querySelector("#settings-disconnect").addEventListener("click", disconnectFromSettings);
dummyFileInput.addEventListener("change", handleDummyFileSelection);
settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = settingsForm.querySelector('button[type="submit"]');
  const disconnectButton = settingsForm.querySelector("#settings-disconnect");
  settingsError.textContent = "";
  submit.disabled = true;
  disconnectButton.disabled = true;
  try {
    const mode = selectedSettingsMode();
    const nextSettings = {
      mode,
      bridgeUrl: settingsForm.querySelector("#bridge-url").value,
      throttleRateMs: settingsForm.querySelector("#throttle-rate").valueAsNumber,
      cameraFit: settingsForm.querySelector('input[name="camera-fit"]:checked')?.value || "contain",
      dummyDataFile: settingsForm.querySelector("#dummy-data-file").dataset.url
        || defaultSettings.dummyDataFile,
    };
    await applySettings(nextSettings, {
      dummyPayload: mode === "dummy" ? pendingDummySelection?.payload || null : null,
      dummySourceLabel: mode === "dummy" ? pendingDummySelection?.name || "" : "",
    });
    closeSettingsDialog();
  } catch (error) {
    settingsError.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    submit.disabled = false;
    updateSettingsDisconnectButton();
  }
});

document.querySelectorAll(".view-tabs button").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
initializeDirectTeachingControls();

window.SurgiMate = {
  state,
  tools: toolRegistry,
  ros: {
    stateTopics: SCENARIO_STATE_TOPICS,
    publicTopicNames: PUBLIC_TOPIC_NAMES,
    get connection() {
      return Object.freeze({
        state: state.connectionState,
        connected: state.connected,
        url: rosRuntime.client?.url || activeSettings.bridgeUrl || "",
        retryAttempt: rosRuntime.client?.retryAttempt || 0,
      });
    },
    get cameraPlayout() {
      return rosRuntime.cameraPlayout?.snapshot() || null;
    },
  },
  settings: {
    get current() { return Object.freeze({ ...activeSettings }); },
    open: openSettingsDialog,
    apply: applySettings,
    loadDummyData,
    readDummyDataFile,
    resetToDefaults: resetSettingsToDefaults,
    disconnect: disconnectFromSettings,
  },
  diagnostics: {
    get entries() { return diagnostics.entries(); },
    get size() { return diagnostics.size; },
    clear: diagnostics.clear,
    download: downloadDiagnosticLog,
  },
  directTeaching: {
    get state() { return directTeachingSnapshot(); },
    toggle: handleDirectTeachingRequest,
    clear: clearDirectTeaching,
  },
  applyRobotState,
  registerTool(toolId, definition) { toolRegistry[toolId] = { ...definition }; renderDashboard(); },
  setRosMessageMapper(mapper) {
    if (mapper !== null && typeof mapper !== "function") throw new Error("mapper must be a function or null");
    rosRuntime.customMapper = mapper;
  },
  connectRosBridge,
  disconnectRosBridge,
};

renderDashboard();
fitDesignToViewport();
window.addEventListener("resize", fitDesignToViewport);
window.addEventListener("pagehide", () => {
  clearDirectTeaching("pagehide");
  if (rosRuntime.client || rosRuntime.cameraPlayout) disconnectRosBridge({ reason: "pagehide" });
  Object.keys(rosRuntime.cameraFrames).forEach(releaseCameraObjectUrl);
}, { once: true });

setInterval(() => {
  if (rosRuntime.activeObservedAt) {
    state.elapsedSeconds = Math.max(0, Math.floor((Date.now() - rosRuntime.activeObservedAt) / 1000));
    state.elapsedAvailable = true;
    state.elapsedSource = "observed";
  } else if (["simulation", "dummy"].includes(state.connectionState) && state.elapsedAvailable) {
    state.elapsedSeconds += 1;
  }
  renderTime();
}, 1000);

void applySettings(defaultSettings).catch((error) => {
  setControlsReadOnly(false);
  setConnectionState("error", error instanceof Error ? error.message : String(error));
});

if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    clearDirectTeaching("hot_reload");
    if (rosRuntime.client || rosRuntime.cameraPlayout) disconnectRosBridge({ reason: "hot_reload" });
    Object.keys(rosRuntime.cameraFrames).forEach(releaseCameraObjectUrl);
  });
}
