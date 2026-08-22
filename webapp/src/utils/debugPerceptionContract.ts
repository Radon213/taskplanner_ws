export const DEBUG_PERCEPTION_MAX_AGE_MS = 3000;
export const DEBUG_PERCEPTION_FINAL_OVERLAY_TOPIC = "/perception/debug/final_overlay/compressed";
export const DEBUG_PERCEPTION_FINAL_OVERLAY_STATUS_TOPIC = "/perception/debug/final_overlay/status";
export const DEBUG_PERCEPTION_FINAL_OVERLAY_STATUS_SCHEMA = "pnu.perception.final_overlay.v1";
export const DEBUG_PERCEPTION_RAW_TOPIC = "/synced/cam_4/color/image_raw/compressed";
export const DEBUG_PERCEPTION_OVERLAY_TOPIC = "/surgery/images/cam4/detection_overlay/compressed";
export const DEBUG_PERCEPTION_POSE_OVERLAY_TOPIC = "/surgery/images/cam4/pose_overlay/compressed";
export const DEBUG_PERCEPTION_TOOL_POSES_TOPIC = "/surgery/perception/cam4/tool_poses";
export const DEBUG_PERCEPTION_HAND_KEYPOINTS_TOPIC = "/surgery/perception/cam4/hand_keypoints";
export const DEBUG_PERCEPTION_BLOOD_SEMANTICS_TOPIC = "/surgery/perception/cam4/blood_semantics/json";
export const DEBUG_PERCEPTION_HEALTH_TOPIC = "/surgery/perception/rfdetr/health";
export const DEBUG_PERCEPTION_DIAGNOSTICS_TOPIC = "/surgery/perception/rfdetr/diagnostics/json";

export interface DebugPerceptionFrame {
  src: string;
  format: string;
  topic: string;
  frameId: string;
  sizeBytes: number;
  sourceStampSec: number;
  sourceStampNanosec: number;
  sourceStampKey: string;
  receivedAt: number;
}

export type DebugFinalOverlayState = "live" | "stale" | "missing" | "disabled";
export type DebugFinalOverlayCameraId = "cam3" | "cam4";
export type DebugFinalOverlayLayerId = "tool" | "pose" | "hand" | "blood";

export interface DebugFinalOverlayStamp {
  sec: number;
  nanosec: number;
  key: string;
}

export interface DebugFinalOverlayOutput {
  sourceStamp: DebugFinalOverlayStamp | null;
  hz: number;
  bytes: number;
  width: number;
  height: number;
}

export interface DebugFinalOverlayBaseStatus {
  sourceStamp: DebugFinalOverlayStamp | null;
  ageSec: number | null;
  received: number;
  dropped: number;
}

export interface DebugFinalOverlayLayerStatus {
  state: DebugFinalOverlayState;
  sourceStamp: DebugFinalOverlayStamp | null;
  ageSec: number | null;
  count: number;
  dropped: number;
}

export interface DebugFinalOverlayCameraStatus {
  state: DebugFinalOverlayState;
  base: DebugFinalOverlayBaseStatus;
  layers: Record<DebugFinalOverlayLayerId, DebugFinalOverlayLayerStatus>;
}

export interface DebugFinalOverlayStatus {
  publishedAt: DebugFinalOverlayStamp;
  output: DebugFinalOverlayOutput;
  cameras: Record<DebugFinalOverlayCameraId, DebugFinalOverlayCameraStatus>;
  receivedAt: number;
}

export interface DebugPerceptionHealth {
  provider: "pnu_hand_blood";
  enabled: boolean;
  connected: boolean;
  status: string;
  modelReady: boolean;
  semanticReady: boolean;
  depthAligned: boolean;
  metric3dReady: boolean;
  metric3dReasons: string[];
  supportPlaneValidated: boolean;
  transportMode: DebugPerceptionTransportMode;
  authMode: DebugPerceptionAuthMode;
  requestedAlgorithms: string[];
  executedAlgorithms: string[];
  detectionCount: number;
  emptyDetectionResult: boolean;
  lastErrorCode: string;
  lastErrorMessage: string;
  sourceStampSec: number;
  sourceStampNanosec: number;
  sourceStampKey: string;
  receivedAt: number;
  stale: boolean;
}

export type DebugPerceptionTransportMode = "https" | "http_local" | "http_trusted_lan_dev";
export type DebugPerceptionAuthMode = "bearer" | "none_local" | "none_trusted_lan_dev";

export interface DebugSupportPlaneDiagnostics {
  schema: "pnu.tool.support_plane_diagnostics.v1";
  validationRequested: boolean;
  artifactLoaded: boolean;
  staticReasons: string[];
  calibrationFit: {
    available: boolean;
    inlierRatio: number | null;
    residualP95M: number | null;
  };
  runtimeValidation: {
    evaluated: boolean;
    metricsAvailable: boolean;
    valid: boolean;
    reasons: string[];
    sampleCount: number;
    inlierRatio: number | null;
    residualMedianM: number | null;
    residualP95M: number | null;
    cameraInfoSha256: string;
  };
}

export interface DebugPerceptionDiagnostics {
  provider: "pnu_hand_blood";
  sequence: number;
  frameId: string;
  sourceStampSec: number;
  sourceStampNanosec: number;
  sourceStampKey: string;
  requestedAlgorithms: string[];
  executedAlgorithms: string[];
  modelVersion: string;
  modelDigests: Record<string, string>;
  toolDetectionCount: number;
  bloodDetectionCount: number;
  handCount: number;
  instanceCount: number;
  emptyDetectionResult: boolean;
  metric3dReady: boolean;
  metric3dReasons: string[];
  depthAligned: boolean;
  depthScaleValidated: boolean;
  supportPlaneValidated: boolean;
  transportMode: DebugPerceptionTransportMode;
  authMode: DebugPerceptionAuthMode;
  supportPlaneDiagnostics: DebugSupportPlaneDiagnostics | null;
  inferenceLatencyMs: number;
  sourceToOutputLatencyMs: number;
  queueAgeMs: number;
  renderEncodeLatencyMs: number;
  overlayPublished: boolean | null;
  overlayStatus: string;
  overlayTruncated: boolean;
  overlayDrawnToolCount: number;
  overlayDrawnBloodCount: number;
  overlayDrawnHandCount: number;
  poseOverlayPublished: boolean | null;
  poseOverlayStatus: string;
  poseOverlayTruncated: boolean;
  poseOverlayDrawnAxisCount: number;
  poseOverlayDrawnPositionOnlyCount: number;
  poseOverlayRenderEncodeLatencyMs: number | null;
  errorCode: string;
  errorMessage: string;
  receivedAt: number;
}

export type DebugToolPoseMode = 0 | 1 | 2 | 3 | 4;
export type DebugToolPoseValidity = 0 | 1 | 2 | 3;

export interface DebugToolPose {
  frameLocalInstanceId: number;
  canonicalClassId: number;
  modelClassIndex: number;
  className: string;
  classConfidence: number;
  position: { x: number; y: number; z: number };
  orientation: { x: number; y: number; z: number; w: number };
  poseMode: DebugToolPoseMode;
  positionValid: boolean;
  orientationValid: boolean;
  dofObserved: [boolean, boolean, boolean, boolean, boolean, boolean];
  observationPointDefinition: string;
  observationPointUvPx: [number, number];
  observationPointInsideMask: boolean;
  observationPointDepthValid: boolean;
  observationPointSelectionMode: string;
  observationPointBoundaryClearancePx: number;
  axisDefinition: string;
  symmetryType: string;
  endpointSignConfidence: number;
  validDepthRatio: number;
  posePointCount: number;
  axisAnisotropy: number;
  supportPlaneInlierRatio: number;
  supportPlaneResidualP95M: number;
  poseConfidence: number;
  poseConfidenceCalibrated: boolean;
  validity: DebugToolPoseValidity;
  statusFlags: string[];
  invalidReason: string;
}

export interface DebugToolPoseArray {
  sequence: string;
  schemaVersion: "pnu.surgical_tool_pose_array.v1.3";
  observationId: string;
  sourceView: "cam4";
  modelVersion: string;
  ontologyVersion: string;
  calibrationVersion: string;
  poseConventionVersion: string;
  frameId: string;
  sourceStampSec: number;
  sourceStampNanosec: number;
  sourceStampKey: string;
  tools: DebugToolPose[];
  receivedAt: number;
}

export interface DebugToolPoseEvidence {
  poses: DebugToolPoseArray;
  diagnostics: DebugPerceptionDiagnostics;
}

export type DebugHandDepthSource = "real" | "mono" | "2d_only";

export interface DebugHandJoint {
  index: number;
  u: number;
  v: number;
  x: number;
  y: number;
  z: number;
  score: number;
  validDepth: boolean;
}

export interface DebugHandKeypoint {
  handIndex: number;
  hasHandedness: boolean;
  handednessLabel: "Left" | "Right" | "";
  handednessScore: number;
  joints: DebugHandJoint[];
  hasPalm6d: boolean;
  palm6d: {
    translation: { x: number; y: number; z: number };
    orientation: { x: number; y: number; z: number; w: number };
    rotationMatrix: [number, number, number, number, number, number, number, number, number];
  } | null;
}

export interface DebugHandKeypoints {
  frameId: string;
  sourceStampSec: number;
  sourceStampNanosec: number;
  sourceStampKey: string;
  depthSource: DebugHandDepthSource;
  hands: DebugHandKeypoint[];
  receivedAt: number;
}

export interface DebugBloodInstance {
  instanceId: number;
  confidence: number;
  bboxXyxyPx: [number, number, number, number];
  centroidXyPx: [number, number];
  centroidDepthValid: boolean;
  centroidDepthM: number | null;
}

export interface DebugBloodSemantics {
  frameId: string;
  sourceStampSec: number;
  sourceStampNsKey: string;
  metric3dReady: boolean;
  detections: DebugBloodInstance[];
  combinedCentroidXyPx: [number, number] | null;
  combinedCentroidDepthValid: boolean;
  combinedCentroidDepthM: number | null;
  receivedAt: number;
}

export interface DebugHandEvidence {
  diagnostics: DebugPerceptionDiagnostics;
  result: DebugHandKeypoints;
}

export interface DebugBloodEvidence {
  diagnostics: DebugPerceptionDiagnostics;
  result: DebugBloodSemantics;
}

export type DebugPerceptionEvidenceState =
  | "waiting_for_health"
  | "disabled"
  | "waiting_for_frame"
  | "waiting_for_overlay"
  | "waiting_for_matching_raw"
  | "ready"
  | "stale"
  | "error"
  | "contract_error";

const MAX_JSON_CHARS = 256 * 1024;
const MAX_COLLECTION_ITEMS = 512;
const MAX_OBJECT_KEYS = 512;
const MAX_STRING_CHARS = 16 * 1024;
const MAX_COMPRESSED_IMAGE_BYTES = 12 * 1024 * 1024;
const MAX_TOOL_POSES = 64;
const MAX_TOOL_STATUS_FLAGS = 32;
const PROVIDER = "pnu_hand_blood";
const ALGORITHMS = new Set(["tool", "blood", "hand"]);
const OVERLAY_STATUSES = new Set([
  "published",
  "rate_limited",
  "disabled",
  "publisher_unavailable",
  "render_error",
]);
const TOOL_POSE_SCHEMA = "pnu.surgical_tool_pose_array.v1.3";
const SUPPORT_PLANE_DIAGNOSTICS_SCHEMA = "pnu.tool.support_plane_diagnostics.v1";
const TRANSPORT_MODES = new Set<DebugPerceptionTransportMode>([
  "https",
  "http_local",
  "http_trusted_lan_dev",
]);
const AUTH_MODES = new Set<DebugPerceptionAuthMode>([
  "bearer",
  "none_local",
  "none_trusted_lan_dev",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isBoundedPayload(value: unknown, depth = 0): boolean {
  if (depth > 8) return false;
  if (typeof value === "string") return value.length <= MAX_STRING_CHARS;
  if (Array.isArray(value)) {
    return value.length <= MAX_COLLECTION_ITEMS
      && value.every((item) => isBoundedPayload(item, depth + 1));
  }
  if (isRecord(value)) {
    const entries = Object.entries(value);
    return entries.length <= MAX_OBJECT_KEYS
      && entries.every(([key, item]) => key.length <= MAX_STRING_CHARS
        && isBoundedPayload(item, depth + 1));
  }
  return true;
}

function boundedText(value: unknown, maxLength = 500): string {
  return typeof value === "string" ? value.trim().slice(0, maxLength) : "";
}

function finiteNonnegativeNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function finiteNonnegativeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function sourceStamp(
  secondsValue: unknown,
  nanosecondsValue: unknown,
): { sec: number; nanosec: number; key: string } | null {
  const sec = finiteNonnegativeInteger(secondsValue);
  const nanosec = finiteNonnegativeInteger(nanosecondsValue);
  if (sec === null || nanosec === null || nanosec >= 1_000_000_000) return null;
  if (sec === 0 && nanosec === 0) return { sec, nanosec, key: "" };
  return { sec, nanosec, key: `${sec}:${String(nanosec).padStart(9, "0")}` };
}

function normalizedAlgorithms(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.length > ALGORITHMS.size) return null;
  const normalized = value.map((item) => boundedText(item, 16).toLowerCase());
  if (
    normalized.some((item) => !ALGORITHMS.has(item))
    || new Set(normalized).size !== normalized.length
  ) return null;
  return normalized;
}

function normalizedReasons(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.length > 16) return null;
  const reasons = value.map((item) => boundedText(item, 160));
  return reasons.every(Boolean) ? reasons : null;
}

function normalizedTransportMode(value: unknown): DebugPerceptionTransportMode | null {
  return typeof value === "string" && TRANSPORT_MODES.has(value as DebugPerceptionTransportMode)
    ? value as DebugPerceptionTransportMode
    : null;
}

function normalizedAuthMode(value: unknown): DebugPerceptionAuthMode | null {
  return typeof value === "string" && AUTH_MODES.has(value as DebugPerceptionAuthMode)
    ? value as DebugPerceptionAuthMode
    : null;
}

function exactRecord(value: unknown, keys: readonly string[]): Record<string, unknown> | null {
  if (!isRecord(value)) return null;
  const actual = Object.keys(value);
  return actual.length === keys.length
    && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key))
    ? value
    : null;
}

function normalizedSupportPlaneReasons(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.length > 16) return null;
  const reasons: string[] = [];
  for (const item of value) {
    if (
      typeof item !== "string"
      || item.length === 0
      || item.length > 160
      || item.trim() !== item
      || /[\u0000-\u001f\u007f]/.test(item)
    ) return null;
    reasons.push(item);
  }
  return new Set(reasons).size === reasons.length ? reasons : null;
}

function nullableFiniteMetric(
  value: unknown,
  minimum: number,
  maximum: number,
): number | null | undefined {
  if (value === null) return null;
  return typeof value === "number"
    && Number.isFinite(value)
    && value >= minimum
    && value <= maximum
    ? value
    : undefined;
}

function parseSupportPlaneDiagnostics(value: unknown): DebugSupportPlaneDiagnostics | null {
  const parsed = exactRecord(value, [
    "schema",
    "validation_requested",
    "artifact_loaded",
    "static_reasons",
    "calibration_fit",
    "runtime_validation",
  ]);
  if (
    !parsed
    || parsed.schema !== SUPPORT_PLANE_DIAGNOSTICS_SCHEMA
    || typeof parsed.validation_requested !== "boolean"
    || typeof parsed.artifact_loaded !== "boolean"
  ) return null;
  const staticReasons = normalizedSupportPlaneReasons(parsed.static_reasons);
  const fit = exactRecord(parsed.calibration_fit, ["available", "inlier_ratio", "residual_p95_m"]);
  const runtime = exactRecord(parsed.runtime_validation, [
    "evaluated",
    "metrics_available",
    "valid",
    "reasons",
    "sample_count",
    "inlier_ratio",
    "residual_median_m",
    "residual_p95_m",
    "camera_info_sha256",
  ]);
  if (
    staticReasons === null
    || !fit
    || !runtime
    || typeof fit.available !== "boolean"
    || typeof runtime.evaluated !== "boolean"
    || typeof runtime.metrics_available !== "boolean"
    || typeof runtime.valid !== "boolean"
  ) return null;
  const fitInlierRatio = nullableFiniteMetric(fit.inlier_ratio, 0, 1);
  const fitResidualP95M = nullableFiniteMetric(fit.residual_p95_m, 0, 10);
  const runtimeReasons = normalizedSupportPlaneReasons(runtime.reasons);
  const sampleCount = finiteNonnegativeInteger(runtime.sample_count);
  const runtimeInlierRatio = nullableFiniteMetric(runtime.inlier_ratio, 0, 1);
  const runtimeResidualMedianM = nullableFiniteMetric(runtime.residual_median_m, 0, 10);
  const runtimeResidualP95M = nullableFiniteMetric(runtime.residual_p95_m, 0, 10);
  const cameraInfoSha256 = typeof runtime.camera_info_sha256 === "string"
    ? runtime.camera_info_sha256
    : "__invalid__";
  if (
    fitInlierRatio === undefined
    || fitResidualP95M === undefined
    || runtimeReasons === null
    || sampleCount === null
    || runtimeInlierRatio === undefined
    || runtimeResidualMedianM === undefined
    || runtimeResidualP95M === undefined
    || (cameraInfoSha256 !== "" && !/^[0-9a-f]{64}$/.test(cameraInfoSha256))
    || sampleCount > 50_000
    || fit.available !== parsed.artifact_loaded
    || (parsed.artifact_loaded && !parsed.validation_requested)
    || (parsed.artifact_loaded && staticReasons.length !== 0)
    || (!parsed.artifact_loaded && staticReasons.length === 0)
    || (fit.available && (fitInlierRatio === null || fitResidualP95M === null))
    || (!fit.available && (fitInlierRatio !== null || fitResidualP95M !== null))
    || (runtime.metrics_available && !runtime.evaluated)
    || (runtime.valid && (!runtime.evaluated || !runtime.metrics_available || !parsed.artifact_loaded))
    || (runtime.valid && runtimeReasons.length !== 0)
    || (!runtime.valid && runtimeReasons.length === 0)
    || (!runtime.evaluated && (
      runtime.metrics_available
      || sampleCount !== 0
      || runtimeInlierRatio !== null
      || runtimeResidualMedianM !== null
      || runtimeResidualP95M !== null
      || cameraInfoSha256 !== ""
    ))
    || (runtime.metrics_available && (
      sampleCount === 0
      || runtimeInlierRatio === null
      || runtimeResidualMedianM === null
      || runtimeResidualP95M === null
    ))
    || (!runtime.metrics_available && (
      sampleCount !== 0
      || runtimeInlierRatio !== null
      || runtimeResidualMedianM !== null
      || runtimeResidualP95M !== null
    ))
  ) return null;
  return {
    schema: SUPPORT_PLANE_DIAGNOSTICS_SCHEMA,
    validationRequested: parsed.validation_requested,
    artifactLoaded: parsed.artifact_loaded,
    staticReasons,
    calibrationFit: {
      available: fit.available,
      inlierRatio: fitInlierRatio,
      residualP95M: fitResidualP95M,
    },
    runtimeValidation: {
      evaluated: runtime.evaluated,
      metricsAvailable: runtime.metrics_available,
      valid: runtime.valid,
      reasons: runtimeReasons,
      sampleCount,
      inlierRatio: runtimeInlierRatio,
      residualMedianM: runtimeResidualMedianM,
      residualP95M: runtimeResidualP95M,
      cameraInfoSha256,
    },
  };
}

function sameStringArray(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function parseEnvelope(message: unknown, schema: string): Record<string, unknown> | null {
  const raw = (message as { data?: unknown } | null)?.data;
  if (typeof raw !== "string" || raw.length === 0 || raw.length > MAX_JSON_CHARS) return null;
  try {
    const parsed = JSON.parse(raw) as unknown;
    return isRecord(parsed) && isBoundedPayload(parsed) && parsed.schema === schema
      ? parsed
      : null;
  } catch {
    return null;
  }
}

const FINAL_OVERLAY_STATES = new Set<DebugFinalOverlayState>([
  "live",
  "stale",
  "missing",
  "disabled",
]);
const FINAL_OVERLAY_CAMERA_IDS: readonly DebugFinalOverlayCameraId[] = ["cam3", "cam4"];
const FINAL_OVERLAY_LAYER_IDS: readonly DebugFinalOverlayLayerId[] = [
  "tool",
  "pose",
  "hand",
  "blood",
];
const MAX_FINAL_OVERLAY_BYTES = 12 * 1024 * 1024;
const MAX_FINAL_OVERLAY_DIMENSION = 8_192;
const MAX_FINAL_OVERLAY_PIXELS = 16_000_000;
const MAX_FINAL_OVERLAY_HZ = 240;
const MAX_FINAL_OVERLAY_AGE_SEC = 3_600;
const MAX_FINAL_OVERLAY_COUNT = 1_000_000_000;

function parseFinalOverlayStamp(value: unknown): DebugFinalOverlayStamp | null {
  const record = exactRecord(value, ["sec", "nanosec"]);
  const stamp = record ? sourceStamp(record.sec, record.nanosec) : null;
  return stamp ? { sec: stamp.sec, nanosec: stamp.nanosec, key: stamp.key } : null;
}

function parseFinalOverlayState(value: unknown): DebugFinalOverlayState | null {
  return typeof value === "string" && FINAL_OVERLAY_STATES.has(value as DebugFinalOverlayState)
    ? value as DebugFinalOverlayState
    : null;
}

function boundedFinalOverlayNumber(
  value: unknown,
  maximum: number,
  options: { integer: boolean },
): number | null {
  return typeof value === "number"
    && Number.isFinite(value)
    && value >= 0
    && value <= maximum
    && (!options.integer || Number.isSafeInteger(value))
    ? value
    : null;
}

function parseFinalOverlayBaseStatus(value: unknown): DebugFinalOverlayBaseStatus | null {
  const record = exactRecord(value, ["source_stamp", "age_sec", "received", "dropped"]);
  const sourceIsExplicitNull = record?.source_stamp === null;
  const source = sourceIsExplicitNull ? null : parseFinalOverlayStamp(record?.source_stamp);
  const ageSec = record?.age_sec === null
    ? null
    : record
      ? boundedFinalOverlayNumber(record.age_sec, MAX_FINAL_OVERLAY_AGE_SEC, { integer: false })
      : null;
  const received = record
    ? boundedFinalOverlayNumber(record.received, MAX_FINAL_OVERLAY_COUNT, { integer: true })
    : null;
  const dropped = record
    ? boundedFinalOverlayNumber(record.dropped, MAX_FINAL_OVERLAY_COUNT, { integer: true })
    : null;
  if (!record || (!sourceIsExplicitNull && source === null)) return null;
  return received !== null && dropped !== null
    ? { sourceStamp: source, ageSec, received, dropped }
    : null;
}

function parseFinalOverlayLayerStatus(value: unknown): DebugFinalOverlayLayerStatus | null {
  const record = exactRecord(value, ["state", "source_stamp", "age_sec", "count", "dropped"]);
  const state = record ? parseFinalOverlayState(record.state) : null;
  const sourceIsExplicitNull = record?.source_stamp === null;
  const source = sourceIsExplicitNull ? null : parseFinalOverlayStamp(record?.source_stamp);
  const ageSec = record?.age_sec === null
    ? null
    : record
      ? boundedFinalOverlayNumber(record.age_sec, MAX_FINAL_OVERLAY_AGE_SEC, { integer: false })
      : null;
  const count = record
    ? boundedFinalOverlayNumber(record.count, MAX_FINAL_OVERLAY_COUNT, { integer: true })
    : null;
  const dropped = record
    ? boundedFinalOverlayNumber(record.dropped, MAX_FINAL_OVERLAY_COUNT, { integer: true })
    : null;
  if (!state || (!sourceIsExplicitNull && source === null) || count === null || dropped === null) return null;
  if ((state === "live" || state === "stale") && (source === null || ageSec === null)) return null;
  if ((state === "missing" || state === "disabled") && source !== null) return null;
  if (state === "disabled" && count !== 0) return null;
  return { state, sourceStamp: source, ageSec, count, dropped };
}

function parseFinalOverlayCameraStatus(value: unknown): DebugFinalOverlayCameraStatus | null {
  const record = exactRecord(value, ["state", "base", "layers"]);
  const state = record ? parseFinalOverlayState(record.state) : null;
  const base = record ? parseFinalOverlayBaseStatus(record.base) : null;
  const layersRecord = record && exactRecord(record.layers, FINAL_OVERLAY_LAYER_IDS);
  if (!state || !base || !layersRecord) return null;
  const layers = {} as Record<DebugFinalOverlayLayerId, DebugFinalOverlayLayerStatus>;
  for (const id of FINAL_OVERLAY_LAYER_IDS) {
    const layer = parseFinalOverlayLayerStatus(layersRecord[id]);
    if (!layer) return null;
    layers[id] = layer;
  }
  if (
    (state === "missing" && (base.sourceStamp !== null || base.ageSec !== null))
    || (state !== "missing" && (base.sourceStamp === null || base.ageSec === null))
  ) return null;
  return { state, base, layers };
}

/**
 * Parse the small server-authored health record for the single final overlay.
 * This intentionally rejects permissive aliases: the raster stays visible on
 * a bad status record, while the UI shows a bounded contract-error state.
 */
export function parseFinalOverlayStatus(
  message: unknown,
  receivedAt: number,
): DebugFinalOverlayStatus | null {
  const record = parseEnvelope(message, DEBUG_PERCEPTION_FINAL_OVERLAY_STATUS_SCHEMA);
  const parsed = record && exactRecord(record, ["schema", "published_at", "output", "cameras"]);
  const publishedAt = parsed ? parseFinalOverlayStamp(parsed.published_at) : null;
  const outputRecord = parsed && exactRecord(parsed.output, ["source_stamp", "hz", "bytes", "width", "height"]);
  const outputSourceIsExplicitNull = outputRecord?.source_stamp === null;
  const outputSourceStamp = outputSourceIsExplicitNull
    ? null
    : outputRecord
      ? parseFinalOverlayStamp(outputRecord.source_stamp)
      : null;
  const hz = outputRecord
    ? boundedFinalOverlayNumber(outputRecord.hz, MAX_FINAL_OVERLAY_HZ, { integer: false })
    : null;
  const bytes = outputRecord
    ? boundedFinalOverlayNumber(outputRecord.bytes, MAX_FINAL_OVERLAY_BYTES, { integer: true })
    : null;
  const width = outputRecord
    ? boundedFinalOverlayNumber(outputRecord.width, MAX_FINAL_OVERLAY_DIMENSION, { integer: true })
    : null;
  const height = outputRecord
    ? boundedFinalOverlayNumber(outputRecord.height, MAX_FINAL_OVERLAY_DIMENSION, { integer: true })
    : null;
  const camerasRecord = parsed && exactRecord(parsed.cameras, FINAL_OVERLAY_CAMERA_IDS);
  if (
    !publishedAt
    || hz === null
    || bytes === null
    || width === null
    || height === null
    || width * height > MAX_FINAL_OVERLAY_PIXELS
    || !camerasRecord
  ) return null;
  if (!outputRecord || (!outputSourceIsExplicitNull && outputSourceStamp === null)) return null;
  if (
    (bytes === 0 && (outputSourceStamp !== null || width !== 0 || height !== 0))
    || (bytes > 0 && (outputSourceStamp === null || width === 0 || height === 0))
  ) return null;
  const cameras = {} as Record<DebugFinalOverlayCameraId, DebugFinalOverlayCameraStatus>;
  for (const id of FINAL_OVERLAY_CAMERA_IDS) {
    const camera = parseFinalOverlayCameraStatus(camerasRecord[id]);
    if (!camera) return null;
    cameras[id] = camera;
  }
  if (
    cameras.cam3.layers.hand.state !== "disabled"
    || cameras.cam3.layers.blood.state !== "disabled"
  ) return null;
  return {
    publishedAt,
    output: { sourceStamp: outputSourceStamp, hz, bytes, width, height },
    cameras,
    receivedAt,
  };
}

export function parsePerceptionHealth(
  message: unknown,
  receivedAt: number,
): DebugPerceptionHealth | null {
  const parsed = parseEnvelope(message, "taskplanner.rfdetr_health.v1");
  if (!parsed || parsed.provider !== PROVIDER) return null;
  const requestedAlgorithms = normalizedAlgorithms(parsed.requested_algorithms);
  const executedAlgorithms = normalizedAlgorithms(parsed.executed_algorithms);
  const metric3dReasons = normalizedReasons(parsed.metric_3d_reasons);
  const transportMode = normalizedTransportMode(parsed.transport_mode);
  const authMode = normalizedAuthMode(parsed.auth_mode);
  const stamp = sourceStamp(parsed.source_stamp_sec, parsed.source_stamp_nanosec);
  const detectionCount = finiteNonnegativeInteger(parsed.detection_count);
  if (
    requestedAlgorithms === null
    || executedAlgorithms === null
    || metric3dReasons === null
    || transportMode === null
    || authMode === null
    || stamp === null
    || detectionCount === null
    || typeof parsed.enabled !== "boolean"
    || typeof parsed.connected !== "boolean"
    || typeof parsed.model_ready !== "boolean"
    || typeof parsed.semantic_ready !== "boolean"
    || typeof parsed.depth_aligned !== "boolean"
    || typeof parsed.metric_3d_ready !== "boolean"
    || typeof parsed.support_plane_validated !== "boolean"
    || typeof parsed.empty_detection_result !== "boolean"
  ) return null;
  if (executedAlgorithms.some((algorithm) => !requestedAlgorithms.includes(algorithm))) return null;
  if (parsed.connected && (
    requestedAlgorithms.length === 0
    || !sameStringArray(requestedAlgorithms, executedAlgorithms)
  )) return null;
  if (parsed.empty_detection_result !== (parsed.connected && detectionCount === 0)) return null;
  const status = boundedText(parsed.status, 64);
  if (!status) return null;
  return {
    provider: PROVIDER,
    enabled: parsed.enabled,
    connected: parsed.connected,
    status,
    modelReady: parsed.model_ready,
    semanticReady: parsed.semantic_ready,
    depthAligned: parsed.depth_aligned,
    metric3dReady: parsed.metric_3d_ready,
    metric3dReasons,
    supportPlaneValidated: parsed.support_plane_validated,
    transportMode,
    authMode,
    requestedAlgorithms,
    executedAlgorithms,
    detectionCount,
    emptyDetectionResult: parsed.empty_detection_result,
    lastErrorCode: boundedText(parsed.last_error_code, 120),
    lastErrorMessage: boundedText(parsed.last_error_message, 500),
    sourceStampSec: stamp.sec,
    sourceStampNanosec: stamp.nanosec,
    sourceStampKey: stamp.key,
    receivedAt,
    stale: false,
  };
}

function parseModelDigests(value: unknown): Record<string, string> | null {
  if (value === undefined) return {};
  if (!isRecord(value) || Object.keys(value).length > ALGORITHMS.size) return null;
  const digests: Record<string, string> = {};
  for (const [algorithm, digest] of Object.entries(value)) {
    if (
      !ALGORITHMS.has(algorithm)
      || typeof digest !== "string"
      || !/^[0-9a-f]{64}$/.test(digest)
    ) return null;
    digests[algorithm] = digest;
  }
  return digests;
}

export function parsePerceptionDiagnostics(
  message: unknown,
  receivedAt: number,
): DebugPerceptionDiagnostics | null {
  const parsed = parseEnvelope(message, "pnu.rfdetr_diagnostics.v2");
  if (!parsed || parsed.provider !== PROVIDER) return null;
  const stamp = sourceStamp(parsed.source_stamp_sec, parsed.source_stamp_nanosec);
  const requestedAlgorithms = normalizedAlgorithms(parsed.requested_algorithms);
  const executedAlgorithms = normalizedAlgorithms(parsed.executed_algorithms);
  const metric3dReasons = normalizedReasons(parsed.metric_3d_reasons);
  const modelDigests = parseModelDigests(parsed.model_digests);
  const transportMode = normalizedTransportMode(parsed.transport_mode);
  const authMode = normalizedAuthMode(parsed.auth_mode);
  const errorCode = boundedText(parsed.error_code, 120);
  const supportPlaneValidated = typeof parsed.support_plane_validated === "boolean"
    ? parsed.support_plane_validated
    : errorCode && parsed.support_plane_validated === undefined
      ? false
      : null;
  const supportPlaneDiagnostics = parsed.support_plane_diagnostics === undefined
    || parsed.support_plane_diagnostics === null
    ? null
    : parseSupportPlaneDiagnostics(parsed.support_plane_diagnostics);
  const sequence = finiteNonnegativeInteger(parsed.sequence);
  const toolDetectionCount = finiteNonnegativeInteger(
    parsed.tool_detection_count === undefined && errorCode ? 0 : parsed.tool_detection_count,
  );
  const bloodDetectionCount = finiteNonnegativeInteger(
    parsed.blood_detection_count === undefined && errorCode ? 0 : parsed.blood_detection_count,
  );
  const handCount = finiteNonnegativeInteger(
    parsed.hand_count === undefined && errorCode ? 0 : parsed.hand_count,
  );
  const instanceCount = finiteNonnegativeInteger(parsed.instance_count);
  const inferenceLatencyMs = finiteNonnegativeNumber(parsed.inference_latency_ms);
  const sourceToOutputLatencyMs = finiteNonnegativeNumber(parsed.source_to_output_latency_ms);
  const queueAgeMs = finiteNonnegativeNumber(parsed.queue_age_ms);
  const renderEncodeLatencyMs = finiteNonnegativeNumber(parsed.render_encode_latency_ms);
  const frameId = boundedText(parsed.frame_id, 240);
  const modelVersion = boundedText(parsed.model_version, 240);
  if (
    stamp === null
    || !stamp.key
    || requestedAlgorithms === null
    || executedAlgorithms === null
    || metric3dReasons === null
    || modelDigests === null
    || transportMode === null
    || authMode === null
    || supportPlaneValidated === null
    || (parsed.support_plane_diagnostics !== undefined
      && parsed.support_plane_diagnostics !== null
      && supportPlaneDiagnostics === null)
    || sequence === null
    || toolDetectionCount === null
    || bloodDetectionCount === null
    || handCount === null
    || instanceCount === null
    || inferenceLatencyMs === null
    || sourceToOutputLatencyMs === null
    || queueAgeMs === null
    || renderEncodeLatencyMs === null
    || typeof parsed.metric_3d_ready !== "boolean"
    || typeof parsed.depth_aligned !== "boolean"
  ) return null;
  if (executedAlgorithms.some((algorithm) => !requestedAlgorithms.includes(algorithm))) return null;
  if (!errorCode && !sameStringArray(requestedAlgorithms, executedAlgorithms)) return null;
  if (!errorCode && instanceCount !== toolDetectionCount + bloodDetectionCount + handCount) return null;
  if (!errorCode && (
    !frameId
    || !modelVersion
    || requestedAlgorithms.length === 0
    || Object.keys(modelDigests).length !== requestedAlgorithms.length
    || requestedAlgorithms.some((algorithm) => !modelDigests[algorithm])
    || typeof parsed.depth_scale_validated !== "boolean"
    || typeof parsed.empty_detection_result !== "boolean"
  )) return null;
  if (!errorCode && (
    (supportPlaneDiagnostics && (
      !requestedAlgorithms.includes("tool")
      || !executedAlgorithms.includes("tool")
      || supportPlaneDiagnostics.runtimeValidation.valid !== supportPlaneValidated
    ))
    || (supportPlaneValidated && !supportPlaneDiagnostics)
  )) return null;
  if (
    parsed.depth_scale_validated !== undefined
    && typeof parsed.depth_scale_validated !== "boolean"
  ) return null;
  const emptyDetectionResult = parsed.empty_detection_result === undefined && errorCode
    ? false
    : parsed.empty_detection_result;
  if (typeof emptyDetectionResult !== "boolean" || (!errorCode && emptyDetectionResult !== (instanceCount === 0))) return null;
  const overlayPublished = parsed.overlay_published === undefined
    ? null
    : typeof parsed.overlay_published === "boolean"
      ? parsed.overlay_published
      : undefined;
  if (overlayPublished === undefined) return null;
  const overlayStatus = boundedText(parsed.overlay_status, 120);
  const overlayTruncated = parsed.overlay_truncated;
  const overlayDrawnToolCount = finiteNonnegativeInteger(
    parsed.overlay_drawn_tool_count === undefined && errorCode
      ? 0
      : parsed.overlay_drawn_tool_count,
  );
  const overlayDrawnBloodCount = finiteNonnegativeInteger(
    parsed.overlay_drawn_blood_count === undefined && errorCode
      ? 0
      : parsed.overlay_drawn_blood_count,
  );
  const overlayDrawnHandCount = finiteNonnegativeInteger(
    parsed.overlay_drawn_hand_count === undefined && errorCode
      ? 0
      : parsed.overlay_drawn_hand_count,
  );
  if (
    overlayDrawnToolCount === null
    || overlayDrawnBloodCount === null
    || overlayDrawnHandCount === null
  ) return null;
  if (!errorCode && (
    overlayPublished === null
    || typeof overlayTruncated !== "boolean"
    || !OVERLAY_STATUSES.has(overlayStatus)
    || overlayPublished !== (overlayStatus === "published")
    || (overlayTruncated && !overlayPublished)
    || overlayDrawnToolCount > toolDetectionCount
    || overlayDrawnBloodCount > bloodDetectionCount
    || overlayDrawnHandCount > handCount
  )) return null;
  const poseOverlayFieldNames = [
    "pose_overlay_published",
    "pose_overlay_status",
    "pose_overlay_truncated",
    "pose_overlay_drawn_axis_count",
    "pose_overlay_drawn_position_only_count",
    "pose_overlay_render_encode_latency_ms",
  ] as const;
  const poseOverlayFieldCount = poseOverlayFieldNames.filter((name) => parsed[name] !== undefined).length;
  if (poseOverlayFieldCount !== 0 && poseOverlayFieldCount !== poseOverlayFieldNames.length) return null;
  let poseOverlayPublished: boolean | null = null;
  let poseOverlayStatus = "";
  let poseOverlayTruncated = false;
  let poseOverlayDrawnAxisCount = 0;
  let poseOverlayDrawnPositionOnlyCount = 0;
  let poseOverlayRenderEncodeLatencyMs: number | null = null;
  if (poseOverlayFieldCount === poseOverlayFieldNames.length) {
    poseOverlayPublished = typeof parsed.pose_overlay_published === "boolean"
      ? parsed.pose_overlay_published
      : null;
    poseOverlayStatus = boundedText(parsed.pose_overlay_status, 120);
    poseOverlayDrawnAxisCount = finiteNonnegativeInteger(parsed.pose_overlay_drawn_axis_count) ?? -1;
    poseOverlayDrawnPositionOnlyCount = finiteNonnegativeInteger(
      parsed.pose_overlay_drawn_position_only_count,
    ) ?? -1;
    poseOverlayRenderEncodeLatencyMs = finiteNonnegativeNumber(
      parsed.pose_overlay_render_encode_latency_ms,
    );
    if (
      poseOverlayPublished === null
      || typeof parsed.pose_overlay_truncated !== "boolean"
      || !OVERLAY_STATUSES.has(poseOverlayStatus)
      || poseOverlayPublished !== (poseOverlayStatus === "published")
      || (parsed.pose_overlay_truncated && !poseOverlayPublished)
      || poseOverlayDrawnAxisCount < 0
      || poseOverlayDrawnPositionOnlyCount < 0
      || poseOverlayDrawnAxisCount + poseOverlayDrawnPositionOnlyCount > toolDetectionCount
      || poseOverlayRenderEncodeLatencyMs === null
    ) return null;
    poseOverlayTruncated = parsed.pose_overlay_truncated;
  }
  return {
    provider: PROVIDER,
    sequence,
    frameId,
    sourceStampSec: stamp.sec,
    sourceStampNanosec: stamp.nanosec,
    sourceStampKey: stamp.key,
    requestedAlgorithms,
    executedAlgorithms,
    modelVersion,
    modelDigests,
    toolDetectionCount,
    bloodDetectionCount,
    handCount,
    instanceCount,
    emptyDetectionResult,
    metric3dReady: parsed.metric_3d_ready,
    metric3dReasons,
    depthAligned: parsed.depth_aligned,
    depthScaleValidated: parsed.depth_scale_validated === true,
    supportPlaneValidated,
    transportMode,
    authMode,
    supportPlaneDiagnostics,
    inferenceLatencyMs,
    sourceToOutputLatencyMs,
    queueAgeMs,
    renderEncodeLatencyMs,
    overlayPublished,
    overlayStatus,
    overlayTruncated: typeof overlayTruncated === "boolean" ? overlayTruncated : false,
    overlayDrawnToolCount,
    overlayDrawnBloodCount,
    overlayDrawnHandCount,
    poseOverlayPublished,
    poseOverlayStatus,
    poseOverlayTruncated,
    poseOverlayDrawnAxisCount,
    poseOverlayDrawnPositionOnlyCount,
    poseOverlayRenderEncodeLatencyMs,
    errorCode,
    errorMessage: boundedText(parsed.error_message, 500),
    receivedAt,
  };
}

export function debugPerceptionStampNsKey(sec: number, nanosec: number): string {
  return (BigInt(sec) * 1_000_000_000n + BigInt(nanosec)).toString();
}

function rotationDeterminant(matrix: number[]): number {
  return matrix[0] * (matrix[4] * matrix[8] - matrix[5] * matrix[7])
    - matrix[1] * (matrix[3] * matrix[8] - matrix[5] * matrix[6])
    + matrix[2] * (matrix[3] * matrix[7] - matrix[4] * matrix[6]);
}

function quaternionRotationMatrix(quaternion: number[]): number[] {
  const [x, y, z, w] = quaternion;
  return [
    1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w),
    2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w),
    2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y),
  ];
}

function parseHandKeypoint(value: unknown): DebugHandKeypoint | null {
  const parsed = exactRecord(value, [
    "hand_index",
    "has_handedness",
    "handedness_label",
    "handedness_score",
    "joints_2d",
    "joints_3d",
    "kp_scores",
    "kp_valid_depth",
    "has_palm_6d",
    "palm_6d",
  ]);
  if (!parsed) return null;
  const handIndex = unsignedInteger(parsed.hand_index, 0x7fff_ffff);
  const handednessScore = finiteNumberInRange(parsed.handedness_score, 0, 1);
  const joints2d = Array.isArray(parsed.joints_2d) && parsed.joints_2d.length === 21
    ? parsed.joints_2d.map((joint) => {
      const point = exactRecord(joint, ["u", "v"]);
      const values = point
        ? fixedFiniteArray([point.u, point.v], 2, -1_000_000, 1_000_000)
        : null;
      return values as [number, number] | null;
    })
    : null;
  const joints3d = Array.isArray(parsed.joints_3d) && parsed.joints_3d.length === 21
    ? parsed.joints_3d.map((joint) => {
      const point = exactRecord(joint, ["x", "y", "z"]);
      const values = point
        ? fixedFiniteArray([point.x, point.y, point.z], 3, -100, 100)
        : null;
      return values as [number, number, number] | null;
    })
    : null;
  const scores = fixedFiniteArray(parsed.kp_scores, 21, 0, 1);
  const validDepth = fixedBooleanArray(parsed.kp_valid_depth, 21);
  if (
    handIndex === null
    || handednessScore === null
    || joints2d === null
    || joints2d.some((joint) => joint === null)
    || joints3d === null
    || joints3d.some((joint) => joint === null)
    || scores === null
    || validDepth === null
    || typeof parsed.has_handedness !== "boolean"
    || typeof parsed.has_palm_6d !== "boolean"
    || typeof parsed.handedness_label !== "string"
  ) return null;
  const handednessLabel = parsed.handedness_label;
  if (
    (parsed.has_handedness && !["Left", "Right"].includes(handednessLabel))
    || (!parsed.has_handedness && (handednessLabel !== "" || handednessScore !== 0))
  ) return null;
  const joints = joints2d.map((joint2d, index) => {
    const joint3d = joints3d[index] as [number, number, number];
    return {
      index,
      u: (joint2d as [number, number])[0],
      v: (joint2d as [number, number])[1],
      x: joint3d[0],
      y: joint3d[1],
      z: joint3d[2],
      score: scores[index],
      validDepth: validDepth[index],
    };
  });
  if (joints.some((joint) => (
    joint.validDepth
      ? joint.z <= 0
      : Math.abs(joint.x) > 1e-9 || Math.abs(joint.y) > 1e-9 || Math.abs(joint.z) > 1e-9
  ))) return null;

  const palm = exactRecord(parsed.palm_6d, ["translation", "orientation", "rotation_matrix"]);
  const translationRecord = palm && exactRecord(palm.translation, ["x", "y", "z"]);
  const orientationRecord = palm && exactRecord(palm.orientation, ["x", "y", "z", "w"]);
  const translation = translationRecord
    ? fixedFiniteArray(
      [translationRecord.x, translationRecord.y, translationRecord.z],
      3,
      -100,
      100,
    )
    : null;
  const orientation = orientationRecord
    ? fixedFiniteArray(
      [orientationRecord.x, orientationRecord.y, orientationRecord.z, orientationRecord.w],
      4,
      -1.001,
      1.001,
    )
    : null;
  const rotation = palm ? fixedFiniteArray(palm.rotation_matrix, 9, -1.001, 1.001) : null;
  if (!palm || !translation || !orientation || !rotation) return null;
  if (parsed.has_palm_6d) {
    const quaternionNorm = Math.hypot(...orientation);
    const columns = [
      [rotation[0], rotation[3], rotation[6]],
      [rotation[1], rotation[4], rotation[7]],
      [rotation[2], rotation[5], rotation[8]],
    ];
    const dot = (left: number[], right: number[]) => left.reduce(
      (total, item, index) => total + item * right[index],
      0,
    );
    const quaternionMatrix = quaternionRotationMatrix(orientation);
    if (
      translation[2] <= 0
      || Math.abs(quaternionNorm - 1) > 0.005
      || columns.some((column) => Math.abs(dot(column, column) - 1) > 0.01)
      || Math.abs(dot(columns[0], columns[1])) > 0.01
      || Math.abs(dot(columns[0], columns[2])) > 0.01
      || Math.abs(dot(columns[1], columns[2])) > 0.01
      || Math.abs(rotationDeterminant(rotation) - 1) > 0.01
      || rotation.some((item, index) => Math.abs(item - quaternionMatrix[index]) > 0.02)
      || [0, 2, 9, 17].some((index) => !validDepth[index])
    ) return null;
  } else if (
    translation.some((item) => item !== 0)
    || orientation.some((item) => item !== 0)
    || rotation.some((item) => item !== 0)
  ) return null;

  return {
    handIndex,
    hasHandedness: parsed.has_handedness,
    handednessLabel: handednessLabel as DebugHandKeypoint["handednessLabel"],
    handednessScore,
    joints,
    hasPalm6d: parsed.has_palm_6d,
    palm6d: parsed.has_palm_6d ? {
      translation: { x: translation[0], y: translation[1], z: translation[2] },
      orientation: {
        x: orientation[0],
        y: orientation[1],
        z: orientation[2],
        w: orientation[3],
      },
      rotationMatrix: rotation as DebugHandKeypoint["palm6d"] extends infer Palm
        ? Palm extends { rotationMatrix: infer Matrix } ? Matrix : never
        : never,
    } : null,
  };
}

export function parseHandKeypoints(
  message: unknown,
  receivedAt: number,
): DebugHandKeypoints | null {
  const parsed = exactRecord(message, ["header", "depth_source", "hands"]);
  const header = parsed && exactRecord(parsed.header, ["stamp", "frame_id"]);
  const stampRecord = header && exactRecord(header.stamp, ["sec", "nanosec"]);
  const stamp = sourceStamp(stampRecord?.sec, stampRecord?.nanosec);
  const frameId = boundedText(header?.frame_id, 240);
  const depthSource = parsed?.depth_source;
  if (
    !parsed
    || !stamp?.key
    || !frameId
    || !["real", "mono", "2d_only"].includes(String(depthSource))
    || !Array.isArray(parsed.hands)
    || parsed.hands.length > 8
  ) return null;
  const hands = parsed.hands.map(parseHandKeypoint);
  if (
    hands.some((hand) => hand === null)
    || new Set(hands.map((hand) => hand?.handIndex)).size !== hands.length
  ) return null;
  const typedHands = hands as DebugHandKeypoint[];
  if (depthSource === "2d_only" && typedHands.some(
    (hand) => hand.hasPalm6d || hand.joints.some((joint) => joint.validDepth),
  )) return null;
  return {
    frameId,
    sourceStampSec: stamp.sec,
    sourceStampNanosec: stamp.nanosec,
    sourceStampKey: stamp.key,
    depthSource: depthSource as DebugHandDepthSource,
    hands: typedHands,
    receivedAt,
  };
}

function parseBloodInstance(value: unknown): DebugBloodInstance | null {
  const parsed = exactRecord(value, [
    "instance_id",
    "confidence",
    "bbox_xyxy_px",
    "centroid_xy_px",
    "centroid_depth_valid",
    "centroid_depth_m",
  ]);
  if (!parsed || typeof parsed.centroid_depth_valid !== "boolean") return null;
  const instanceId = unsignedInteger(parsed.instance_id, 0x7fff_ffff);
  const confidence = finiteNumberInRange(parsed.confidence, 0, 1);
  const bbox = fixedFiniteArray(parsed.bbox_xyxy_px, 4, 0, 1_000_000);
  const centroid = fixedFiniteArray(parsed.centroid_xy_px, 2, 0, 1_000_000);
  const depth = parsed.centroid_depth_m === null
    ? null
    : finiteNumberInRange(parsed.centroid_depth_m, Number.MIN_VALUE, 100);
  if (
    instanceId === null
    || confidence === null
    || bbox === null
    || centroid === null
    || (parsed.centroid_depth_valid && depth === null)
    || (!parsed.centroid_depth_valid && parsed.centroid_depth_m !== null)
    || bbox[2] < bbox[0]
    || bbox[3] < bbox[1]
  ) return null;
  return {
    instanceId,
    confidence,
    bboxXyxyPx: bbox as DebugBloodInstance["bboxXyxyPx"],
    centroidXyPx: centroid as DebugBloodInstance["centroidXyPx"],
    centroidDepthValid: parsed.centroid_depth_valid,
    centroidDepthM: depth,
  };
}

export function parseBloodSemantics(
  message: unknown,
  receivedAt: number,
): DebugBloodSemantics | null {
  const envelope = parseEnvelope(message, "taskplanner.cam4_blood_semantics.v1");
  const parsed = exactRecord(envelope, [
    "schema",
    "source",
    "provider",
    "source_stamp_sec",
    "source_stamp_ns",
    "frame_id",
    "ground_truth",
    "metric_3d_ready",
    "detections",
    "combined_centroid_xy_px",
    "combined_centroid_depth_valid",
    "combined_centroid_depth_m",
  ]);
  if (
    !parsed
    || parsed.source !== "cam4_pnu_blood"
    || parsed.provider !== PROVIDER
    || parsed.ground_truth !== false
    || typeof parsed.metric_3d_ready !== "boolean"
    || typeof parsed.combined_centroid_depth_valid !== "boolean"
    || !Array.isArray(parsed.detections)
    || parsed.detections.length > 256
  ) return null;
  const sourceStampSec = finiteNumberInRange(parsed.source_stamp_sec, Number.MIN_VALUE, 10_000_000_000);
  const sourceStampNsKey = uint64Text(parsed.source_stamp_ns);
  const frameId = boundedText(parsed.frame_id, 240);
  const detections = parsed.detections.map(parseBloodInstance);
  const combinedCentroid = parsed.combined_centroid_xy_px === null
    ? null
    : fixedFiniteArray(parsed.combined_centroid_xy_px, 2, 0, 1_000_000);
  const combinedDepth = parsed.combined_centroid_depth_m === null
    ? null
    : finiteNumberInRange(parsed.combined_centroid_depth_m, Number.MIN_VALUE, 100);
  if (
    sourceStampSec === null
    || sourceStampNsKey === null
    || Math.abs(sourceStampSec - Number(BigInt(sourceStampNsKey)) / 1_000_000_000) > 0.000_000_51
    || !frameId
    || detections.some((item) => item === null)
    || new Set(detections.map((item) => item?.instanceId)).size !== detections.length
    || (parsed.combined_centroid_depth_valid && (combinedCentroid === null || combinedDepth === null))
    || (!parsed.combined_centroid_depth_valid && parsed.combined_centroid_depth_m !== null)
    || (parsed.combined_centroid_depth_valid && !parsed.metric_3d_ready)
    || (parsed.metric_3d_ready === false && detections.some((item) => item?.centroidDepthValid))
    || (detections.length === 0 && (
      combinedCentroid !== null
      || parsed.combined_centroid_depth_valid
      || parsed.combined_centroid_depth_m !== null
    ))
  ) return null;
  return {
    frameId,
    sourceStampSec,
    sourceStampNsKey,
    metric3dReady: parsed.metric_3d_ready,
    detections: detections as DebugBloodInstance[],
    combinedCentroidXyPx: combinedCentroid as DebugBloodSemantics["combinedCentroidXyPx"],
    combinedCentroidDepthValid: parsed.combined_centroid_depth_valid,
    combinedCentroidDepthM: combinedDepth,
    receivedAt,
  };
}

function normalizedImageFormat(value: unknown, overlay: boolean): { format: string; mime: string } | null {
  const format = boundedText(value, 64).toLowerCase();
  if (overlay) return format.includes("webp") ? { format, mime: "image/webp" } : null;
  if (format.includes("jpeg") || format.includes("jpg")) return { format, mime: "image/jpeg" };
  if (format.includes("png")) return { format, mime: "image/png" };
  if (format.includes("webp")) return { format, mime: "image/webp" };
  return null;
}

function byteArrayToBase64(data: number[] | Uint8Array): string {
  let binary = "";
  const chunkSize = 0x8000;
  for (let index = 0; index < data.length; index += chunkSize) {
    const chunk = data.slice(index, index + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return window.btoa(binary);
}

function webpMagicIsValid(data: string | number[] | Uint8Array): boolean {
  try {
    const bytes = typeof data === "string"
      ? Array.from(window.atob(data.slice(0, 24)), (character) => character.charCodeAt(0))
      : Array.from(data.slice(0, 12));
    return bytes.length >= 12
      && String.fromCharCode(...bytes.slice(0, 4)) === "RIFF"
      && String.fromCharCode(...bytes.slice(8, 12)) === "WEBP";
  } catch {
    return false;
  }
}

export function parseCompressedFrame(
  message: unknown,
  topic: string,
  overlay: boolean,
  receivedAt: number,
): DebugPerceptionFrame | null {
  if (!isRecord(message)) return null;
  const header = isRecord(message.header) ? message.header : null;
  const stampRecord = header && isRecord(header.stamp) ? header.stamp : null;
  const stamp = sourceStamp(stampRecord?.sec, stampRecord?.nanosec);
  const format = normalizedImageFormat(message.format, overlay);
  const frameId = boundedText(header?.frame_id, 240);
  if (!stamp?.key || !format || !frameId) return null;
  const data = message.data;
  let base64 = "";
  let sizeBytes = 0;
  if (typeof data === "string") {
    if (!data || data.length > Math.ceil(MAX_COMPRESSED_IMAGE_BYTES * 4 / 3) + 4) return null;
    if (data.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(data)) return null;
    sizeBytes = Math.floor(data.length * 3 / 4) - (data.endsWith("==") ? 2 : data.endsWith("=") ? 1 : 0);
    base64 = data;
  } else if (data instanceof Uint8Array) {
    sizeBytes = data.length;
    base64 = byteArrayToBase64(data);
  } else if (Array.isArray(data)) {
    if (!data.every((item) => Number.isInteger(item) && item >= 0 && item <= 255)) return null;
    sizeBytes = data.length;
    base64 = byteArrayToBase64(data as number[]);
  } else return null;
  if (sizeBytes <= 0 || sizeBytes > MAX_COMPRESSED_IMAGE_BYTES) return null;
  if (overlay && !webpMagicIsValid(data as string | number[] | Uint8Array)) return null;
  return {
    src: `data:${format.mime};base64,${base64}`,
    format: format.format,
    topic,
    frameId,
    sizeBytes,
    sourceStampSec: stamp.sec,
    sourceStampNanosec: stamp.nanosec,
    sourceStampKey: stamp.key,
    receivedAt,
  };
}

function finiteNumberInRange(value: unknown, minimum: number, maximum: number): number | null {
  return typeof value === "number"
    && Number.isFinite(value)
    && value >= minimum
    && value <= maximum
    ? value
    : null;
}

function unsignedInteger(value: unknown, maximum: number): number | null {
  return typeof value === "number"
    && Number.isSafeInteger(value)
    && value >= 0
    && value <= maximum
    ? value
    : null;
}

function uint64Text(value: unknown): string | null {
  const text = typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? String(value)
    : typeof value === "string" && /^(0|[1-9][0-9]{0,19})$/.test(value)
      ? value
      : "";
  if (!text) return null;
  try {
    return BigInt(text) <= 18_446_744_073_709_551_615n ? text : null;
  } catch {
    return null;
  }
}

function fixedFiniteArray(
  value: unknown,
  length: number,
  minimum: number,
  maximum: number,
): number[] | null {
  if (!Array.isArray(value) || value.length !== length) return null;
  const parsed = value.map((item) => finiteNumberInRange(item, minimum, maximum));
  return parsed.every((item): item is number => item !== null) ? parsed : null;
}

function fixedBooleanArray(value: unknown, length: number): boolean[] | null {
  return Array.isArray(value)
    && value.length === length
    && value.every((item) => typeof item === "boolean")
    ? value
    : null;
}

function boundedUniqueStrings(value: unknown, maximumItems: number, maximumLength: number): string[] | null {
  if (!Array.isArray(value) || value.length > maximumItems) return null;
  const values = value.map((item) => boundedText(item, maximumLength));
  if (values.some((item) => !item) || new Set(values).size !== values.length) return null;
  return values;
}

function parseToolPose(value: unknown): DebugToolPose | null {
  if (!isRecord(value)) return null;
  const frameLocalInstanceId = unsignedInteger(value.frame_local_instance_id, 0xffff_ffff);
  const canonicalClassId = unsignedInteger(value.canonical_class_id, 0xffff);
  const modelClassIndex = unsignedInteger(value.model_class_index, 0xffff);
  const className = boundedText(value.class_name, 160);
  const classConfidence = finiteNumberInRange(value.class_confidence, 0, 1);
  const pose = isRecord(value.pose) ? value.pose : null;
  const positionRecord = pose && isRecord(pose.position) ? pose.position : null;
  const orientationRecord = pose && isRecord(pose.orientation) ? pose.orientation : null;
  const position = positionRecord
    ? fixedFiniteArray([positionRecord.x, positionRecord.y, positionRecord.z], 3, -100, 100)
    : null;
  const orientation = orientationRecord
    ? fixedFiniteArray(
      [orientationRecord.x, orientationRecord.y, orientationRecord.z, orientationRecord.w],
      4,
      -1.001,
      1.001,
    )
    : null;
  const poseMode = unsignedInteger(value.pose_mode, 4) as DebugToolPoseMode | null;
  const validity = unsignedInteger(value.validity, 3) as DebugToolPoseValidity | null;
  const dofObserved = fixedBooleanArray(value.dof_observed, 6);
  const observationPointUvPx = fixedFiniteArray(value.observation_point_uv_px, 2, 0, 1_000_000);
  const endpointSignConfidence = finiteNumberInRange(value.endpoint_sign_confidence, 0, 1);
  const validDepthRatio = finiteNumberInRange(value.valid_depth_ratio, 0, 1);
  const posePointCount = unsignedInteger(value.pose_point_count, 0xffff_ffff);
  const axisAnisotropy = finiteNumberInRange(value.axis_anisotropy, 0, 1_000_000);
  const supportPlaneInlierRatio = finiteNumberInRange(value.support_plane_inlier_ratio, 0, 1);
  const supportPlaneResidualP95M = finiteNumberInRange(value.support_plane_residual_p95_m, 0, 10);
  const poseConfidence = finiteNumberInRange(value.pose_confidence, 0, 1);
  const observationPointBoundaryClearancePx = finiteNumberInRange(
    value.observation_point_boundary_clearance_px,
    0,
    1_000_000,
  );
  const statusFlags = boundedUniqueStrings(value.status_flags, MAX_TOOL_STATUS_FLAGS, 160);
  if (
    frameLocalInstanceId === null
    || canonicalClassId === null
    || modelClassIndex === null
    || !className
    || classConfidence === null
    || position === null
    || orientation === null
    || poseMode === null
    || validity === null
    || dofObserved === null
    || observationPointUvPx === null
    || endpointSignConfidence === null
    || validDepthRatio === null
    || posePointCount === null
    || axisAnisotropy === null
    || supportPlaneInlierRatio === null
    || supportPlaneResidualP95M === null
    || poseConfidence === null
    || observationPointBoundaryClearancePx === null
    || statusFlags === null
    || typeof value.position_valid !== "boolean"
    || typeof value.orientation_valid !== "boolean"
    || typeof value.observation_point_inside_mask !== "boolean"
    || typeof value.observation_point_depth_valid !== "boolean"
    || typeof value.pose_confidence_calibrated !== "boolean"
  ) return null;

  const positionValid = value.position_valid;
  const orientationValid = value.orientation_valid;
  const observationPointDefinition = boundedText(value.observation_point_definition, 500);
  const observationPointSelectionMode = boundedText(value.observation_point_selection_mode, 160);
  const axisDefinition = boundedText(value.axis_definition, 500);
  const symmetryType = boundedText(value.symmetry_type, 160);
  const invalidReason = boundedText(value.invalid_reason, 500);
  if (
    (orientationValid && !positionValid)
    || (positionValid && (position[2] <= 0 || !value.observation_point_depth_valid))
    || (orientationValid && (!axisDefinition || statusFlags.includes("SUPPORT_PLANE_UNVALIDATED")))
    || (poseMode === 0 && (positionValid || orientationValid))
    || (poseMode === 1 && (!positionValid || orientationValid))
    || (poseMode === 3 && (!orientationValid || dofObserved.some((observed) => !observed)))
    || (validity === 0 && (positionValid || orientationValid))
    || (validity === 1 && !positionValid)
  ) return null;
  if (orientationValid) {
    const norm = Math.hypot(...orientation);
    if (Math.abs(norm - 1) > 0.005) return null;
  }

  return {
    frameLocalInstanceId,
    canonicalClassId,
    modelClassIndex,
    className,
    classConfidence,
    position: { x: position[0], y: position[1], z: position[2] },
    orientation: {
      x: orientation[0],
      y: orientation[1],
      z: orientation[2],
      w: orientation[3],
    },
    poseMode,
    positionValid,
    orientationValid,
    dofObserved: dofObserved as DebugToolPose["dofObserved"],
    observationPointDefinition,
    observationPointUvPx: observationPointUvPx as [number, number],
    observationPointInsideMask: value.observation_point_inside_mask,
    observationPointDepthValid: value.observation_point_depth_valid,
    observationPointSelectionMode,
    observationPointBoundaryClearancePx,
    axisDefinition,
    symmetryType,
    endpointSignConfidence,
    validDepthRatio,
    posePointCount,
    axisAnisotropy,
    supportPlaneInlierRatio,
    supportPlaneResidualP95M,
    poseConfidence,
    poseConfidenceCalibrated: value.pose_confidence_calibrated,
    validity,
    statusFlags,
    invalidReason,
  };
}

export function parseToolPoseArray(message: unknown, receivedAt: number): DebugToolPoseArray | null {
  if (!isRecord(message) || !isBoundedPayload(message)) return null;
  const header = isRecord(message.header) ? message.header : null;
  const stampRecord = header && isRecord(header.stamp) ? header.stamp : null;
  const stamp = sourceStamp(stampRecord?.sec, stampRecord?.nanosec);
  const frameId = boundedText(header?.frame_id, 240);
  const sequence = uint64Text(message.sequence);
  const schemaVersion = boundedText(message.schema_version, 120);
  const observationId = boundedText(message.observation_id, 160);
  const sourceView = boundedText(message.source_view, 32);
  const modelVersion = boundedText(message.model_version, 240);
  const ontologyVersion = boundedText(message.ontology_version, 240);
  const calibrationVersion = boundedText(message.calibration_version, 240);
  const poseConventionVersion = boundedText(message.pose_convention_version, 240);
  if (
    !stamp?.key
    || !frameId
    || sequence === null
    || schemaVersion !== TOOL_POSE_SCHEMA
    || sourceView !== "cam4"
    || !modelVersion
    || !ontologyVersion
    || !calibrationVersion
    || !poseConventionVersion
    || !Array.isArray(message.tools)
    || message.tools.length > MAX_TOOL_POSES
  ) return null;
  const expectedObservationId = `cam4:${BigInt(stamp.sec) * 1_000_000_000n + BigInt(stamp.nanosec)}`;
  if (observationId !== expectedObservationId) return null;
  const tools = message.tools.map(parseToolPose);
  if (tools.some((tool) => tool === null)) return null;
  const parsedTools = tools as DebugToolPose[];
  if (new Set(parsedTools.map((tool) => tool.frameLocalInstanceId)).size !== parsedTools.length) {
    return null;
  }
  return {
    sequence,
    schemaVersion: TOOL_POSE_SCHEMA,
    observationId,
    sourceView: "cam4",
    modelVersion,
    ontologyVersion,
    calibrationVersion,
    poseConventionVersion,
    frameId,
    sourceStampSec: stamp.sec,
    sourceStampNanosec: stamp.nanosec,
    sourceStampKey: stamp.key,
    tools: parsedTools,
    receivedAt,
  };
}
