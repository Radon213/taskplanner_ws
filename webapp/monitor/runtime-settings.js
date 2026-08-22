export const DUMMY_DATA_SCHEMA_VERSION = "taskplanner.ui_contract_fixture.v1";
export const MIN_THROTTLE_RATE_MS = 100;
export const MAX_THROTTLE_RATE_MS = 5000;
export const DEFAULT_THROTTLE_RATE_MS = 100;
export const MAX_DUMMY_DATA_BYTES = 512 * 1024;

const VALID_MODES = new Set(["ros", "dummy"]);
const VALID_CAMERA_FITS = new Set(["contain", "cover"]);
const DUMMY_FIXTURE_TOPICS = new Set([
  "/surgery/gateway_info",
  "/surgery/catalog",
  "/surgery/context",
  "/surgery/instruments",
  "/surgery/robots",
  "/surgery/robot_end_effectors",
  "/surgery/tool_predictions",
  "/surgery/speech",
  "/surgery/health",
]);
const DUMMY_FIXTURE_ROOT_KEYS = new Set([
  "fixture_schema",
  "synthetic",
  "usage",
  "baseline",
  ...DUMMY_FIXTURE_TOPICS,
]);
const REQUIRED_DUMMY_TOPICS = Object.freeze([
  "/surgery/context",
  "/surgery/instruments",
  "/surgery/tool_predictions",
]);
const isRecord = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);

function normalizeMode(value) {
  if (value === "simulation") return "dummy";
  return String(value || "").trim().toLowerCase();
}

function normalizeCameraFit(value) {
  const fit = String(value || "").trim().toLowerCase();
  return VALID_CAMERA_FITS.has(fit) ? fit : "";
}

export function normalizeBridgeUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) throw new Error("서버 접속 주소를 입력해 주세요.");
  if (raw.length > 2048) throw new Error("서버 접속 주소가 너무 깁니다.");
  const candidate = /^[a-z][a-z\d+.-]*:\/\//i.test(raw) ? raw : `ws://${raw}`;
  let parsed;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new Error("올바른 WebSocket 주소를 입력해 주세요.");
  }
  if (!new Set(["ws:", "wss:"]).has(parsed.protocol) || !parsed.hostname) {
    throw new Error("서버 주소는 ws:// 또는 wss:// 형식이어야 합니다.");
  }
  if (parsed.username || parsed.password) {
    throw new Error("서버 주소에 사용자 정보는 포함할 수 없습니다.");
  }
  if (parsed.search || parsed.hash) {
    throw new Error("서버 주소에 query 또는 fragment는 포함할 수 없습니다.");
  }
  return parsed.href.replace(/\/$/, "");
}

export function settingsDefaults(runtimeConfig = {}) {
  const rosbridge = runtimeConfig.rosbridge || {};
  return Object.freeze({
    mode: normalizeMode(runtimeConfig.mode) === "dummy" ? "dummy" : "ros",
    bridgeUrl: normalizeBridgeUrl(rosbridge.url || "ws://127.0.0.1:9092"),
    throttleRateMs: Math.min(
      MAX_THROTTLE_RATE_MS,
      Math.max(
        MIN_THROTTLE_RATE_MS,
        Math.trunc(Number(rosbridge.cameraStreams?.throttleRateMs) || DEFAULT_THROTTLE_RATE_MS),
      ),
    ),
    cameraFit: normalizeCameraFit(rosbridge.cameraStreams?.fit) || "contain",
    dummyDataFile: String(runtimeConfig.dummyDataFile || "/monitor/dummy-data.json"),
  });
}

export function validateSettings(candidate = {}, defaults = settingsDefaults()) {
  const mode = normalizeMode(candidate.mode ?? defaults.mode);
  if (!VALID_MODES.has(mode)) throw new Error("서버 접속 또는 더미 데이터를 선택해 주세요.");
  const throttleRateMs = Number(candidate.throttleRateMs ?? defaults.throttleRateMs);
  if (
    !Number.isInteger(throttleRateMs)
    || throttleRateMs < MIN_THROTTLE_RATE_MS
    || throttleRateMs > MAX_THROTTLE_RATE_MS
  ) {
    throw new Error(`영상 수신 간격은 ${MIN_THROTTLE_RATE_MS}~${MAX_THROTTLE_RATE_MS}ms 정수로 입력해 주세요.`);
  }
  const dummyDataFile = String((candidate.dummyDataFile ?? defaults.dummyDataFile) || "").trim();
  if (!dummyDataFile) throw new Error("더미 데이터 파일 경로가 필요합니다.");
  const cameraFit = normalizeCameraFit(candidate.cameraFit ?? defaults.cameraFit ?? "contain");
  if (!cameraFit) throw new Error("영상 표시 방식은 contain 또는 cover여야 합니다.");
  return Object.freeze({
    mode,
    bridgeUrl: normalizeBridgeUrl(candidate.bridgeUrl ?? defaults.bridgeUrl),
    throttleRateMs,
    cameraFit,
    dummyDataFile,
  });
}

export function validateDummyFixture(payload) {
  if (!isRecord(payload)) throw new Error("더미 데이터는 JSON 객체여야 합니다.");
  const unsupportedKey = Object.keys(payload).find((key) => !DUMMY_FIXTURE_ROOT_KEYS.has(key));
  if (unsupportedKey) throw new Error(`지원하지 않는 더미 데이터 항목입니다: ${unsupportedKey}`);
  if (String(payload.fixture_schema || "") !== DUMMY_DATA_SCHEMA_VERSION) {
    throw new Error(`더미 데이터 fixture_schema은 ${DUMMY_DATA_SCHEMA_VERSION}이어야 합니다.`);
  }
  if (payload.synthetic !== true) {
    throw new Error("더미 데이터에는 synthetic: true가 필요합니다.");
  }
  if (!isRecord(payload.baseline)) {
    throw new Error("더미 데이터의 baseline 객체가 필요합니다.");
  }
  if (String(payload.baseline.schema_version || "") !== "1.1.0") {
    throw new Error("더미 데이터 baseline.schema_version은 1.1.0이어야 합니다.");
  }
  if (String(payload.baseline.interface_version || "") !== "0.3.0") {
    throw new Error("더미 데이터 baseline.interface_version은 0.3.0이어야 합니다.");
  }
  if (!String(payload.baseline.procedure_type || "").trim()) {
    throw new Error("더미 데이터 baseline.procedure_type이 필요합니다.");
  }
  if (!String(payload.baseline.catalog_version || "").trim()) {
    throw new Error("더미 데이터 baseline.catalog_version이 필요합니다.");
  }
  const presentTopics = [...DUMMY_FIXTURE_TOPICS].filter((topic) => payload[topic] !== undefined);
  if (!presentTopics.length) {
    throw new Error("더미 데이터에 지원되는 /surgery 토픽 snapshot이 필요합니다.");
  }
  presentTopics.forEach((topic) => {
    if (!isRecord(payload[topic])) throw new Error(`${topic} snapshot은 JSON 객체여야 합니다.`);
  });
  REQUIRED_DUMMY_TOPICS.forEach((topic) => {
    if (!isRecord(payload[topic])) throw new Error(`더미 데이터에 ${topic} snapshot이 필요합니다.`);
  });
  if (!Array.isArray(payload["/surgery/instruments"].instruments)) {
    throw new Error("/surgery/instruments.instruments 배열이 필요합니다.");
  }
  if (!Array.isArray(payload["/surgery/tool_predictions"].predictions)) {
    throw new Error("/surgery/tool_predictions.predictions 배열이 필요합니다.");
  }
  return structuredClone(payload);
}

function parseDummyFixtureText(text) {
  if (new TextEncoder().encode(text).byteLength > MAX_DUMMY_DATA_BYTES) {
    throw new Error("더미 데이터 파일은 512KB 이하여야 합니다.");
  }
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error("더미 데이터 JSON 형식이 올바르지 않습니다.");
  }
  return validateDummyFixture(payload);
}

export async function readDummyDataFile(file) {
  if (!file || typeof file.text !== "function") {
    throw new Error("선택한 더미 데이터 파일을 읽을 수 없습니다.");
  }
  if (Number.isFinite(Number(file.size)) && Number(file.size) > MAX_DUMMY_DATA_BYTES) {
    throw new Error("더미 데이터 파일은 512KB 이하여야 합니다.");
  }
  let text;
  try {
    text = await file.text();
  } catch {
    throw new Error("선택한 더미 데이터 파일을 읽을 수 없습니다.");
  }
  return parseDummyFixtureText(String(text));
}

export async function loadDummyData(url, fetchImpl = globalThis.fetch) {
  if (typeof fetchImpl !== "function") throw new Error("더미 데이터 파일을 읽을 수 없습니다.");
  let response;
  try {
    response = await fetchImpl(url, { cache: "no-store" });
  } catch {
    throw new Error(`더미 데이터 파일에 연결할 수 없습니다: ${url}`);
  }
  if (!response?.ok) throw new Error(`더미 데이터 파일을 읽지 못했습니다 (${response?.status || "network"}).`);
  const declaredSize = Number(response.headers?.get?.("content-length"));
  if (Number.isFinite(declaredSize) && declaredSize > MAX_DUMMY_DATA_BYTES) {
    throw new Error("더미 데이터 파일은 512KB 이하여야 합니다.");
  }
  let payload;
  try {
    if (typeof response.text === "function") {
      payload = parseDummyFixtureText(await response.text());
    } else {
      payload = validateDummyFixture(await response.json());
    }
  } catch (error) {
    if (error instanceof Error && (
      error.message.includes("512KB")
      || error.message.includes("fixture_schema")
      || error.message.includes("synthetic")
      || error.message.includes("baseline")
      || error.message.includes("snapshot")
    )) throw error;
    throw new Error("더미 데이터 JSON 형식이 올바르지 않습니다.");
  }
  return payload;
}
