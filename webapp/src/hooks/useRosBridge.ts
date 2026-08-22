import { useEffect, useLayoutEffect, useRef, useState, startTransition } from "react";
import ROSLIB from "roslib";

import type {
  BedRobotArmState,
  BedRobotArmStateArray,
  BTDecision,
  Cam4ToolRequestObservation,
  CompressedImageFrame,
  InputSourceStatus,
  InstrumentState,
  LiveAsrControlResult,
  LiveAsrDevice,
  LiveAsrFinal,
  LiveAsrLanHealth,
  LiveAsrRoutePolicy,
  LiveAsrStatus,
  ModelCatalogEntry,
  ModelProviderStatus,
  ModelRuntimeCommand,
  ModelSelection,
  RosTime,
  ShadowGroundTruthState,
  ShadowReplayState,
  SimulationEvent,
  SimulationState,
  SkillStatus,
  SpeechUtterance,
  SurgeonLLMDecision,
  SurgeonState,
  VLMHealth,
  VLMReducerDecision,
  VLMResult,
  WorldState,
} from "../types";
import {
  runtimeBridgeUrl,
  type TaskplannerRuntimeMode,
} from "../runtimeModes";

const DEFAULT_STATE: SimulationState = {
  procedure_id: "",
  active_bundle: "",
  running: false,
  execution_state: "idle",
  filtered_phase: "",
  robot_state: "idle",
  surgeon_intent: "",
  surgeon_request_tool: "",
  surgeon_ready_for_handover: false,
  surgeon_ready_for_retrieval: false,
  cleaner_busy: false,
  cleaner_remaining_sec: 0,
  pending_transition_tools: [],
  active_recovery_tools: [],
  right_hand_tool: "",
  left_hand_tool: "",
  prepositioned_tool: "",
  active_robot_task_id: "",
  active_robot_task_type: "",
  active_robot_task_tool_id: "",
  active_robot_task_arm: "",
  active_robot_task_source_anchor: "",
  active_robot_task_target_anchor: "",
  active_robot_task_progress: 0,
  active_robot_task_remaining_sec: 0,
  bed_robot_arms: [],
  instrument_states: [],
  recent_events: [],
  layout_json: "",
};

const AUTHORITATIVE_SIMULATION_EXECUTION_STATES = new Set([
  "idle",
  "starting",
  "running",
  "paused",
  "finishing",
  "completed",
  "halted",
  "terminated",
  "resetting",
  "stopping",
]);

const AUTHORITATIVE_SHADOW_REPLAY_STATES = new Set([
  "unavailable",
  "loading",
  "ready",
  "running",
  "paused",
  "held",
  "draining",
  "stopped",
  "blocked",
  "timed_out",
  "error",
  "completed",
]);

const AUTHORITATIVE_SHADOW_REPLAY_MODES = new Set([
  "realtime_1x",
  "elastic_demo",
]);

// A replay run ID is intentionally created by the controller when Start is
// admitted. The initial loaded/ready heartbeat therefore has no run ID yet,
// while every state that can only occur after a run starts must keep one.
const SHADOW_REPLAY_STATES_REQUIRING_RUN_ID = new Set([
  "running",
  "paused",
  "held",
  "draining",
  "blocked",
  "timed_out",
  "completed",
]);

const DEFAULT_SURGEON: SurgeonState = {
  procedure_id: "",
  phase_id: "",
  intent: "",
  requested_tool: "",
  ready_for_handover: false,
  ready_for_retrieval: false,
  scripted: true,
  voice_text: "",
  scene_note: "",
};

const DEFAULT_SURGEON_LLM_DECISION: SurgeonLLMDecision = {
  model_id: "",
  raw_json: "",
  accepted: false,
  reject_reason: "",
  action: "",
  tool: "",
  request_mode: "",
  speech: "",
  hidden_phase: "",
  latency_sec: 0,
  seed: 0,
  overlay_json: "",
};

const DEFAULT_BT_DECISION: BTDecision = {
  decision: "idle",
  selected_tool: "",
  selected_tool_lifecycle: "",
  next_required_transition: "",
  action: "",
  handover_allowed: false,
  rationale: "",
  decision_reason: "",
  blocking_guard: "",
};

const DEFAULT_SKILL_STATUS: SkillStatus = {
  command_id: "",
  action: "",
  instrument_id: "",
  state: "",
  success: false,
  message: "",
  arm: "",
  source_location_id: "",
  source_location_type: "",
  target_location_id: "",
  target_location_type: "",
  target_owner: "",
  cleaning_required: false,
  mode: "",
  progress: 0,
  elapsed_sec: 0,
  remaining_sec: 0,
};

const DEFAULT_VLM_HEALTH: VLMHealth = {
  connected: false,
  healthy: false,
  model_id: "",
  image_source: "",
  latency_sec: 0,
  prompt_chars: 0,
  output_chars: 0,
  parse_retry_count: 0,
  last_error: "",
  last_mode: "",
};

const DEFAULT_VLM_RESULT: VLMResult = {
  source: "",
  schema_version: "",
  raw_json: "",
  summary: "",
  phase_ids: [],
  phase_confidences: [],
  observed_tool_ids: [],
  observed_location_ids: [],
  observed_location_types: [],
  observed_confidences: [],
  gesture_event_type: "",
  gesture_requested_tool: "",
  gesture_hand_pose: "",
  gesture_confidence: 0,
  uncertainty: 0,
};

const DEFAULT_CAM4_TOOL_REQUEST: Cam4ToolRequestObservation = {
  available: false,
  state: "uncertain",
  requested: null,
  confidence: 0,
  sourceStampSec: 0,
  receivedAt: 0,
  onsetSourceStampSec: 0,
  onsetReceivedAt: 0,
};

const DEFAULT_SHADOW_GROUND_TRUTH: ShadowGroundTruthState = {
  available: false,
  runId: "",
  caseId: "",
  sourceTimeSec: 0,
  phase: {
    phaseId: "",
    startSec: 0,
    endSec: 0,
    active: false,
  },
  eventId: "",
  active: false,
  startSec: 0,
  endSec: 0,
  receivedAt: 0,
  eventStartReceivedAt: 0,
};

const DEFAULT_WORLD_STATE: WorldState = {
  procedure_id: "",
  running: false,
  execution_state: "idle",
  filtered_phase: "",
  phase_confidence: 0,
  phase_uncertain: true,
  phase_stability: 0,
  expected_instruments: [],
  available_instruments: [],
  right_hand_tool: "",
  left_hand_tool: "",
  prepositioned_tool: "",
  predicted_tool: "",
  predicted_tool_confidence: 0,
  predicted_tool_stability_sec: 0,
  surgeon_request_tool: "",
  explicit_request_voice_backed: false,
  bed_robot_arms: [],
};

const DEFAULT_SHADOW_REPLAY_STATE: ShadowReplayState = {
  stamp: { sec: 0, nanosec: 0 },
  run_id: "",
  case_id: "",
  procedure_id: "",
  state: "unavailable",
  mode: "elastic_demo",
  loaded: false,
  running: false,
  paused: false,
  completed: false,
  source_time_sec: 0,
  duration_sec: 0,
  image_duration_sec: 0,
  wall_elapsed_sec: 0,
  playback_rate: 1,
  elastic_hold_sec: 0,
  hold_reason: "",
  last_error: "",
  published_image_count: 0,
  published_transcript_count: 0,
  completed_vlm_count: 0,
  pending_vlm_count: 0,
  active_skill_count: 0,
};

const DEFAULT_LIVE_ASR_STATUS: LiveAsrStatus = {
  schema: "taskplanner.asr.status.v1",
  stamp_sec: 0,
  available: false,
  dependency_error: "",
  state: "UNAVAILABLE",
  server_url: "",
  topic: "/sensors/surgeon/sentence",
  device_id: null,
  device_name: "",
  devices: [],
  device_status: "NO_INPUT",
  device_message: "ASR 상태를 기다리는 중입니다.",
  connected: false,
  audio_level_dbfs: -99,
  peak_level_dbfs: -99,
  elapsed_sec: 0,
  partial_text: "",
  finals: [],
  last_error: "",
  sample_rate: 16000,
  channels: 1,
  sample_width_bits: 16,
  endpoint_id: "",
  route_policy: "cloud",
  selection_reason: "",
  lan_health: {
    enabled: false,
    state: "UNKNOWN",
    method: "websocket_handshake",
    age_ms: null,
    latency_ms: null,
    consecutive_failures: 0,
    last_error: "",
  },
};

const MAX_LIVE_ASR_STATUS_JSON_CHARS = 256 * 1024;
const MAX_LIVE_ASR_DEVICES = 64;
const MAX_LIVE_ASR_FINALS = 48;
const MAX_LIVE_ASR_DISPLAY_TEXT_CHARS = 4_096;

type RosCompressedImage = {
  header?: {
    frame_id?: string;
  };
  format?: string;
  data?: string | number[] | Uint8Array;
};

type RosString = {
  data?: string;
};

function finiteNumber(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function optionalFiniteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

const MAX_ROS_JSON_PAYLOAD_CHARS = 256 * 1024;
const MAX_ROS_PAYLOAD_COLLECTION_ITEMS = 256;
const MAX_ROS_PAYLOAD_OBJECT_KEYS = 512;
const MAX_ROS_PAYLOAD_STRING_CHARS = 64 * 1024;

function isBoundedRosPayload(value: unknown, depth = 0): boolean {
  if (depth > 8) return false;
  if (typeof value === "string") return value.length <= MAX_ROS_PAYLOAD_STRING_CHARS;
  if (Array.isArray(value)) {
    return value.length <= MAX_ROS_PAYLOAD_COLLECTION_ITEMS
      && value.every((item) => isBoundedRosPayload(item, depth + 1));
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    return entries.length <= MAX_ROS_PAYLOAD_OBJECT_KEYS
      && entries.every(([key, item]) => key.length <= MAX_ROS_PAYLOAD_STRING_CHARS
        && isBoundedRosPayload(item, depth + 1));
  }
  return true;
}

function normalizeLiveAsrRoutePolicy(value: unknown): LiveAsrRoutePolicy {
  const policy = String(value ?? "").trim().toLowerCase();
  return policy === "lan" || policy === "auto" || policy === "cloud"
    ? policy
    : "cloud";
}

function normalizeLiveAsrLanHealth(value: unknown): LiveAsrLanHealth {
  const health = value && typeof value === "object"
    ? value as Record<string, unknown>
    : {};
  const age = optionalFiniteNumber(health.age_ms);
  const latency = optionalFiniteNumber(health.latency_ms);
  return {
    enabled: Boolean(health.enabled),
    state: String(health.state ?? "UNKNOWN").toUpperCase().slice(0, 64),
    method: String(health.method ?? "websocket_handshake").slice(0, 128),
    age_ms: age === null ? null : Math.max(0, age),
    latency_ms: latency === null ? null : Math.max(0, latency),
    consecutive_failures: Math.max(0, Math.trunc(finiteNumber(health.consecutive_failures))),
    last_error: String(health.last_error ?? "").slice(0, MAX_LIVE_ASR_DISPLAY_TEXT_CHARS),
  };
}

function normalizeLiveAsrDevice(value: unknown): LiveAsrDevice | null {
  if (!value || typeof value !== "object") return null;
  const device = value as Record<string, unknown>;
  const id = Number(device.id);
  if (!Number.isInteger(id)) return null;
  return {
    id,
    name: String(device.name ?? `Input ${id}`).slice(0, 512),
    input_channels: Math.max(0, Math.trunc(finiteNumber(device.input_channels))),
    default_samplerate: Math.max(0, finiteNumber(device.default_samplerate)),
    default: Boolean(device.default),
  };
}

function normalizeLiveAsrFinal(value: unknown): LiveAsrFinal | null {
  if (!value || typeof value !== "object") return null;
  const final = value as Record<string, unknown>;
  const text = String(final.text ?? "").trim().slice(0, MAX_LIVE_ASR_DISPLAY_TEXT_CHARS);
  if (!text) return null;
  const latencyMissing = final.response_latency_ms === null
    || final.response_latency_ms === undefined
    || final.response_latency_ms === "";
  const rawLatency = latencyMissing ? Number.NaN : Number(final.response_latency_ms);
  return {
    stamp: String(final.stamp ?? "").slice(0, 128),
    text,
    response_latency_ms: Number.isFinite(rawLatency) && rawLatency >= 0 ? rawLatency : null,
    latency_basis: String(final.latency_basis ?? "unavailable").slice(0, 128),
    latency_correlated: Boolean(final.latency_correlated),
  };
}

export function normalizeLiveAsrStatus(message: unknown): LiveAsrStatus | null {
  const raw = String((message as RosString | null)?.data ?? "");
  if (raw.length > MAX_LIVE_ASR_STATUS_JSON_CHARS) return null;
  try {
    const envelope = JSON.parse(raw) as Record<string, unknown>;
    if (!isBoundedRosPayload(envelope)) return null;
    if (envelope.schema !== "taskplanner.asr.status.v1") return null;
    const snapshot = envelope.asr && typeof envelope.asr === "object"
      ? envelope.asr as Record<string, unknown>
      : {};
    const rawDevices = Array.isArray(snapshot.devices)
      ? snapshot.devices.slice(0, MAX_LIVE_ASR_DEVICES)
      : [];
    const rawFinals = Array.isArray(snapshot.finals)
      ? snapshot.finals.slice(-MAX_LIVE_ASR_FINALS)
      : [];
    const rawDeviceId = snapshot.device_id;
    return {
      ...DEFAULT_LIVE_ASR_STATUS,
      schema: "taskplanner.asr.status.v1",
      stamp_sec: finiteNumber(envelope.stamp_sec),
      available: Boolean(snapshot.available),
      dependency_error: String(snapshot.dependency_error ?? "").slice(0, MAX_LIVE_ASR_DISPLAY_TEXT_CHARS),
      state: String(snapshot.state ?? "UNAVAILABLE").toUpperCase().slice(0, 64),
      server_url: String(snapshot.server_url ?? "").slice(0, 2048),
      topic: String(snapshot.topic ?? "/sensors/surgeon/sentence").slice(0, 512),
      device_id: rawDeviceId === null || rawDeviceId === undefined || rawDeviceId === ""
        ? null
        : Number.isInteger(Number(rawDeviceId))
          ? Number(rawDeviceId)
          : null,
      device_name: String(snapshot.device_name ?? "").slice(0, 512),
      devices: rawDevices.map(normalizeLiveAsrDevice).filter((value): value is LiveAsrDevice => value !== null),
      device_status: String(snapshot.device_status ?? "NO_INPUT").slice(0, 64),
      device_message: String(snapshot.device_message ?? "").slice(0, MAX_LIVE_ASR_DISPLAY_TEXT_CHARS),
      connected: Boolean(snapshot.connected),
      audio_level_dbfs: finiteNumber(snapshot.audio_level_dbfs, -99),
      peak_level_dbfs: finiteNumber(snapshot.peak_level_dbfs, -99),
      elapsed_sec: Math.max(0, finiteNumber(snapshot.elapsed_sec)),
      partial_text: String(snapshot.partial_text ?? "").slice(0, MAX_LIVE_ASR_DISPLAY_TEXT_CHARS),
      finals: rawFinals.map(normalizeLiveAsrFinal).filter((value): value is LiveAsrFinal => value !== null),
      last_error: String(snapshot.last_error ?? "").slice(0, MAX_LIVE_ASR_DISPLAY_TEXT_CHARS),
      sample_rate: Math.max(0, finiteNumber(snapshot.sample_rate, 16000)),
      channels: Math.max(0, Math.trunc(finiteNumber(snapshot.channels, 1))),
      sample_width_bits: Math.max(0, Math.trunc(finiteNumber(snapshot.sample_width_bits, 16))),
      endpoint_id: String(snapshot.endpoint_id ?? "").slice(0, 64),
      route_policy: normalizeLiveAsrRoutePolicy(snapshot.route_policy),
      selection_reason: String(snapshot.selection_reason ?? "").slice(0, MAX_LIVE_ASR_DISPLAY_TEXT_CHARS),
      lan_health: normalizeLiveAsrLanHealth(snapshot.lan_health),
    };
  } catch {
    return null;
  }
}

const CAM4_TOOL_REQUEST_STATES = new Set<
  Cam4ToolRequestObservation["state"]
>(["request", "not_request", "hand_with_tool", "uncertain"]);

export function normalizeCam4ToolRequest(
  message: unknown,
  receivedAt = Date.now(),
): Cam4ToolRequestObservation {
  const raw = String((message as RosString | null)?.data ?? "");
  if (raw.length > MAX_ROS_JSON_PAYLOAD_CHARS) return DEFAULT_CAM4_TOOL_REQUEST;
  try {
    const payload = JSON.parse(raw) as Record<string, unknown>;
    if (!isBoundedRosPayload(payload)) return DEFAULT_CAM4_TOOL_REQUEST;
    if (payload.schema !== "taskplanner.cam4_semantics.v1") {
      return DEFAULT_CAM4_TOOL_REQUEST;
    }
    const sourceStampSec = Number(payload.source_stamp_sec);
    const request =
      payload.tool_request && typeof payload.tool_request === "object"
        ? (payload.tool_request as Record<string, unknown>)
        : {};
    const candidateState = String(request.state ?? "uncertain") as
      Cam4ToolRequestObservation["state"];
    const state = CAM4_TOOL_REQUEST_STATES.has(candidateState)
      ? candidateState
      : "uncertain";
    const rawConfidence = Number(request.confidence);
    const confidence = Number.isFinite(rawConfidence)
      ? Math.max(0, Math.min(1, rawConfidence))
      : 0;
    return {
      available: Number.isFinite(sourceStampSec),
      state,
      requested:
        state === "request"
          ? true
          : state === "not_request"
            ? false
            : null,
      confidence,
      sourceStampSec: Number.isFinite(sourceStampSec) ? sourceStampSec : 0,
      receivedAt,
      onsetSourceStampSec: 0,
      onsetReceivedAt: 0,
    };
  } catch {
    return DEFAULT_CAM4_TOOL_REQUEST;
  }
}

function normalizeShadowGroundTruth(
  message: unknown,
  receivedAt = Date.now(),
): ShadowGroundTruthState {
  const raw = String((message as RosString | null)?.data ?? "");
  if (raw.length > MAX_ROS_JSON_PAYLOAD_CHARS) return DEFAULT_SHADOW_GROUND_TRUTH;
  try {
    const payload = JSON.parse(raw) as Record<string, unknown>;
    if (!isBoundedRosPayload(payload)) return DEFAULT_SHADOW_GROUND_TRUTH;
    if (
      payload.schema !== "taskplanner.shadow_ground_truth.v1" &&
      payload.schema !== "taskplanner.shadow_ground_truth.v2"
    ) {
      return DEFAULT_SHADOW_GROUND_TRUTH;
    }
    const request =
      payload.implicit_tool_request &&
      typeof payload.implicit_tool_request === "object"
        ? (payload.implicit_tool_request as Record<string, unknown>)
        : {};
    const phase =
      payload.phase && typeof payload.phase === "object"
        ? (payload.phase as Record<string, unknown>)
        : {};
    const finite = (value: unknown) => {
      const number = Number(value);
      return Number.isFinite(number) ? number : 0;
    };
    return {
      available: Boolean(payload.available),
      runId: String(payload.run_id ?? ""),
      caseId: String(payload.case_id ?? ""),
      sourceTimeSec: finite(payload.source_time_sec),
      phase: {
        phaseId: String(phase.phase_id ?? ""),
        startSec: finite(phase.start_sec),
        endSec: finite(phase.end_sec),
        active: Boolean(phase.active ?? phase.phase_id),
      },
      eventId: String(request.event_id ?? ""),
      active: Boolean(request.active),
      startSec: finite(request.start_sec),
      endSec: finite(request.end_sec),
      receivedAt,
      eventStartReceivedAt: 0,
    };
  } catch {
    return DEFAULT_SHADOW_GROUND_TRUTH;
  }
}

// Keep only the freshest not-yet-serialized frame per image subscription.
// rosbridge otherwise accepts an unbounded stream (queue_length=0) and can
// accumulate large outgoing WebSocket writes when a browser falls behind.
const ROSBRIDGE_IMAGE_QUEUE_LENGTH = 1;
const ROSBRIDGE_IMAGE_COMPRESSION = "cbor";
const MAX_COMPRESSED_IMAGE_BYTES = 12 * 1024 * 1024;
const ROSBRIDGE_PREVIEW_IMAGE_QOS = {
  history: "keep_last",
  depth: 1,
  reliability: "best_effort",
  durability: "volatile",
} as const;

// The browser is an operator preview, not the acquisition recorder. Cap every
// image stream at 5 FPS and keep queue_length=1 so five synchronized previews
// plus derived overlays cannot build a rosbridge serialization backlog when a
// tab is briefly slow or hidden. ROS/CV consumers still receive the native
// 15 FPS topics directly.
const CAMERA_FRAME_THROTTLE_MS = 180;
const CAMERA_STALE_AFTER_MS = 3000;
const RUNTIME_STATE_MAX_AGE_MS = 4000;
// An idle SimulationState checkpoint can arrive about every 2.5 seconds once
// DDS discovery and rosbridge subscription setup are included. Allow more than
// two observed periods before rebuilding an otherwise-open transport.
const RUNTIME_STATE_INITIAL_TIMEOUT_MS = 8000;
// A single delayed heartbeat fails closed immediately at MAX_AGE, but a
// prolonged silent subscription should rebuild itself instead of leaving the
// operator in a permanent stale state.
const RUNTIME_STATE_RECOVERY_TIMEOUT_MS = 4000;

type RawCameraTopicMap = Record<"cam1" | "cam2" | "cam3" | "cam4" | "flir", string>;

const INTERNAL_CAMERA_TOPICS: RawCameraTopicMap = {
  cam1: "/surgery/images/cam1/compressed",
  cam2: "/surgery/images/cam2/compressed",
  cam3: "/surgery/images/cam3/compressed",
  cam4: "/surgery/images/cam4/compressed",
  flir: "/surgery/images/flir/compressed",
};

const EXTERNAL_CAMERA_TOPICS: RawCameraTopicMap = {
  cam1: import.meta.env.VITE_EXTERNAL_CAM1_TOPIC?.trim()
    || "/preview/cam_1/color/image_raw/compressed",
  cam2: import.meta.env.VITE_EXTERNAL_CAM2_TOPIC?.trim()
    || "/preview/cam_2/color/image_raw/compressed",
  cam3: import.meta.env.VITE_EXTERNAL_CAM3_TOPIC?.trim()
    || "/preview/cam_3/color/image_raw/compressed",
  cam4: import.meta.env.VITE_EXTERNAL_CAM4_TOPIC?.trim()
    || "/preview/cam_4/color/image_raw/compressed",
  flir: import.meta.env.VITE_EXTERNAL_FLIR_TOPIC?.trim()
    || "/preview/flir/color/image_raw/compressed",
};

function rawCameraTopicsForMode(runtimeMode: TaskplannerRuntimeMode): RawCameraTopicMap {
  return runtimeMode === "live" || runtimeMode === "llm"
    ? EXTERNAL_CAMERA_TOPICS
    : INTERNAL_CAMERA_TOPICS;
}

function configurePreviewImageSubscription(topic: any): void {
  // roslib 1.4.1 does not expose rosbridge's per-subscription QoS option.
  // Decorate its connection command so the same preview QoS request is also
  // replayed after a WebSocket reconnect. This is used only for the five
  // browser-only physical-camera previews.  Acquisition and perception retain
  // their full-rate `/synced` subscriptions outside rosbridge.
  const sendOnConnection = topic.callForSubscribeAndAdvertise.bind(topic);
  topic.callForSubscribeAndAdvertise = (request: Record<string, unknown>) => {
    sendOnConnection(
      request.op === "subscribe"
        ? { ...request, qos: ROSBRIDGE_PREVIEW_IMAGE_QOS }
        : request,
    );
  };
}

export type PerceptionLayerHealth = {
  received: boolean;
  enabled: boolean;
  connected: boolean;
  status: string;
  latencyMs: number;
  lastError: string;
};

const DEFAULT_PERCEPTION_HEALTH: PerceptionLayerHealth = {
  received: false,
  enabled: false,
  connected: false,
  status: "unavailable",
  latencyMs: 0,
  lastError: "",
};

function normalizePerceptionHealth(
  message: unknown,
): PerceptionLayerHealth {
  const raw = (message as RosString | null)?.data;
  if (!raw || raw.length > MAX_ROS_JSON_PAYLOAD_CHARS) return DEFAULT_PERCEPTION_HEALTH;
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (!isBoundedRosPayload(parsed)) return DEFAULT_PERCEPTION_HEALTH;
    if (parsed.schema !== "taskplanner.rfdetr_health.v1") {
      return DEFAULT_PERCEPTION_HEALTH;
    }
    return {
      received: true,
      enabled: Boolean(parsed.enabled),
      connected: Boolean(parsed.connected),
      status: String(parsed.status || "unknown"),
      latencyMs: Number.isFinite(Number(parsed.latency_ms))
        ? Number(parsed.latency_ms)
        : 0,
      lastError: String(parsed.last_error || ""),
    };
  } catch {
    return DEFAULT_PERCEPTION_HEALTH;
  }
}

type ShadowTranscriptHistory = {
  runId: string;
  utterances: SpeechUtterance[];
};

function speechSourceTime(utterance: SpeechUtterance): number {
  return (
    Number(utterance.start_stamp?.sec ?? 0) +
    Number(utterance.start_stamp?.nanosec ?? 0) / 1_000_000_000
  );
}

function mergeShadowTranscripts(
  current: SpeechUtterance[],
  incoming: SpeechUtterance[],
): SpeechUtterance[] {
  const byId = new Map<string, SpeechUtterance>();
  for (const utterance of [...current, ...incoming]) {
    if (
      !utterance.utterance_id ||
      !utterance.is_final ||
      !utterance.text?.trim()
    ) {
      continue;
    }
    byId.set(utterance.utterance_id, utterance);
  }
  return [...byId.values()]
    .sort((left, right) => {
      const timeDifference =
        speechSourceTime(right) - speechSourceTime(left);
      return (
        timeDifference ||
        right.utterance_id.localeCompare(left.utterance_id)
      );
    })
    .slice(0, 48);
}

function transcriptRunId(source: string): string {
  const parts = source.split(":");
  return parts.length >= 3 && parts[0] === "recorded_transcript"
    ? parts.slice(2).join(":")
    : "";
}

function normalizeShadowTranscriptHistory(
  message: unknown,
): ShadowTranscriptHistory {
  const raw = (message as RosString | null)?.data;
  if (!raw || raw.length > MAX_ROS_JSON_PAYLOAD_CHARS) return { runId: "", utterances: [] };
  try {
    const parsed = JSON.parse(raw) as {
      schema?: string;
      run_id?: string;
      utterances?: Partial<SpeechUtterance>[];
    };
    if (!isBoundedRosPayload(parsed)) return { runId: "", utterances: [] };
    if (
      parsed.schema !== "taskplanner.shadow_transcript_history.v1" ||
      !Array.isArray(parsed.utterances)
    ) {
      return { runId: "", utterances: [] };
    }
    const utterances = parsed.utterances
      .filter(
        (item) =>
          Boolean(item?.utterance_id) &&
          Boolean(item?.is_final) &&
          Boolean(item?.text?.trim()),
      )
      .map((item) => ({
        stamp: item.stamp ?? { sec: 0, nanosec: 0 },
        start_stamp: item.start_stamp ?? { sec: 0, nanosec: 0 },
        end_stamp: item.end_stamp ?? { sec: 0, nanosec: 0 },
        utterance_id: String(item.utterance_id),
        text: String(item.text),
        is_final: true,
        has_confidence: Boolean(item.has_confidence),
        confidence: Number(item.confidence ?? 0),
        speaker_role: String(item.speaker_role || "surgeon"),
        language: String(item.language || ""),
        source: String(item.source || ""),
      }))
      .slice(0, 48);
    return {
      runId: String(parsed.run_id || ""),
      utterances,
    };
  } catch {
    return { runId: "", utterances: [] };
  }
}

type RosServiceResponseMessage = {
  result?: boolean;
  values?: Record<string, unknown> | string;
};

type RosServiceConnection = {
  idCounter?: number;
  isConnected?: boolean;
  on: (event: string, callback: (message: RosServiceResponseMessage) => void) => void;
  off?: (event: string, callback: (message: RosServiceResponseMessage) => void) => void;
  removeListener?: (event: string, callback: (message: RosServiceResponseMessage) => void) => void;
  callOnConnection: (message: Record<string, unknown>) => void;
};

function unsubscribeWhileConnected(ros: any, topics: any[]): void {
  if (!ros?.isConnected || ros.socket?.readyState !== WebSocket.OPEN) return;
  topics.forEach((topic) => {
    try {
      topic.unsubscribe();
    } catch {
      // A closed bridge already discarded all subscriptions.
    }
  });
}

function normalizeBedRobotArmState(message: unknown): BedRobotArmState | null {
  if (!message || typeof message !== "object") return null;
  const arm = message as Partial<BedRobotArmState>;
  const armId = String(arm.arm_id || "").trim();
  if (!armId || String(arm.role || "").trim().toLowerCase() !== "retraction") {
    return null;
  }
  return {
    arm_id: armId,
    role: "retraction",
    role_instance_id: String(arm.role_instance_id || "").trim(),
    state: String(arm.state || "unknown").trim().toLowerCase(),
    direct_teach_active: Boolean(arm.direct_teach_active),
    reason_code: String(arm.reason_code || "").trim(),
  };
}

function normalizeBedRobotArmStates(message: unknown): BedRobotArmState[] {
  if (!Array.isArray(message)) return [];
  return message
    .map((arm) => normalizeBedRobotArmState(arm))
    .filter((arm): arm is BedRobotArmState => arm !== null);
}

const BED_ROBOT_ARM_STATES = new Set([
  "standby",
  "direct_teach",
  "retracting",
  "changing_tool",
  "moving_to_standby",
  "fault",
  "protective_stop",
  "unknown",
]);

const BED_ROBOT_PROCEDURE_LAYOUTS: Record<string, ReadonlySet<string>> = {
  thyroidectomy: new Set(["army_navy"]),
  nephrectomy: new Set(["left_malleable", "right_malleable"]),
};

const BED_ROBOT_STATUS_MAX_AGE_MS = 3000;

type ValidatedBedRobotArmStatus = {
  stampMs: number;
  receivedAtMs: number;
  revision: number;
  procedureType: string;
  arms: BedRobotArmState[];
};

function canonicalBedRobotProcedure(procedureId: string): string {
  const normalized = procedureId.trim().toLowerCase();
  if (normalized === "thyroidectomy" || normalized === "thyroidectomy_demo") {
    return "thyroidectomy";
  }
  return normalized === "nephrectomy" ? normalized : "";
}

function rosTimeToMilliseconds(stamp: BedRobotArmStateArray["stamp"] | undefined): number | null {
  const sec = Number(stamp?.sec);
  const nanosec = Number(stamp?.nanosec);
  if (
    !Number.isSafeInteger(sec) ||
    sec < 0 ||
    !Number.isInteger(nanosec) ||
    nanosec < 0 ||
    nanosec >= 1_000_000_000
  ) {
    return null;
  }
  return sec * 1000 + nanosec / 1_000_000;
}

function normalizeBedRobotArmStatus(message: unknown): ValidatedBedRobotArmStatus | null {
  if (!isBoundedRosPayload(message) || !message || typeof message !== "object") return null;
  const status = message as Partial<BedRobotArmStateArray>;
  const procedureType = String(status.procedure_type || "").trim().toLowerCase();
  const expectedRoles = BED_ROBOT_PROCEDURE_LAYOUTS[procedureType];
  const stampMs = rosTimeToMilliseconds(status.stamp);
  const revision = Number(status.revision);
  if (
    !expectedRoles ||
    !Array.isArray(status.arms) ||
    stampMs === null ||
    stampMs <= 0 ||
    !Number.isSafeInteger(revision) ||
    revision < 0
  ) {
    return null;
  }

  const arms = normalizeBedRobotArmStates(status.arms);
  if (arms.length !== status.arms.length || arms.length !== expectedRoles.size) {
    return null;
  }
  const armIds = new Set<string>();
  const roles = new Set<string>();
  for (const arm of arms) {
    if (
      !new Set(["arm_1", "arm_2"]).has(arm.arm_id) ||
      armIds.has(arm.arm_id) ||
      !expectedRoles.has(arm.role_instance_id) ||
      roles.has(arm.role_instance_id) ||
      !BED_ROBOT_ARM_STATES.has(arm.state) ||
      arm.direct_teach_active !== (arm.state === "direct_teach")
    ) {
      return null;
    }
    armIds.add(arm.arm_id);
    roles.add(arm.role_instance_id);
  }
  return roles.size === expectedRoles.size
    ? { stampMs, receivedAtMs: Date.now(), revision, procedureType, arms }
    : null;
}

function sameBedRobotArmState(
  left: BedRobotArmState[],
  right: BedRobotArmState[],
): boolean {
  if (left.length !== right.length) return false;
  const leftById = new Map(left.map((arm) => [arm.arm_id, arm]));
  return right.every((arm) => {
    const previous = leftById.get(arm.arm_id);
    return Boolean(
      previous &&
      previous.role === arm.role &&
      previous.role_instance_id === arm.role_instance_id &&
      previous.state === arm.state &&
      previous.direct_teach_active === arm.direct_teach_active &&
      previous.reason_code === arm.reason_code,
    );
  });
}

function normalizeSimulationState(message: unknown): SimulationState {
  if (!isBoundedRosPayload(message)) return DEFAULT_STATE;
  const state = message && typeof message === "object" ? (message as Partial<SimulationState>) : {};
  return {
    ...DEFAULT_STATE,
    ...state,
    bed_robot_arms: normalizeBedRobotArmStates(state.bed_robot_arms),
    instrument_states: Array.isArray(state.instrument_states) ? state.instrument_states : [],
    recent_events: Array.isArray(state.recent_events) ? state.recent_events : [],
  };
}

function isAuthoritativeInstrumentState(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const instrument = value as Partial<InstrumentState>;
  const requiredStrings = [
    instrument.instrument_id,
    instrument.home_location_type,
    instrument.home_location_id,
    instrument.location_type,
    instrument.location_id,
    instrument.owner,
    instrument.status,
    instrument.cleanliness_state,
    instrument.lifecycle_stage,
  ];
  const optionalStrings = [
    instrument.instance_id,
    instrument.reserved_for,
    instrument.last_holder,
    instrument.next_required_transition,
    instrument.visual_anchor_id,
    instrument.preposition_origin_location_type,
    instrument.preposition_origin_location_id,
    instrument.preposition_origin_lifecycle_stage,
  ];
  return requiredStrings.every(
    (field) => typeof field === "string" && field.trim().length > 0,
  ) &&
    optionalStrings.every(
      (field) => field === undefined || typeof field === "string",
    ) &&
    isFiniteUnitInterval(instrument.confidence) &&
    typeof instrument.contaminated === "boolean";
}

function areAuthoritativeInstrumentStates(value: unknown): value is InstrumentState[] {
  if (!Array.isArray(value)) return false;
  const instanceIds = new Set<string>();
  return value.every((item) => {
    if (!isAuthoritativeInstrumentState(item)) return false;
    const rawInstanceId = (item as Partial<InstrumentState>).instance_id;
    const instanceId = typeof rawInstanceId === "string"
      ? rawInstanceId.trim()
      : "";
    if (!instanceId) return true;
    if (instanceIds.has(instanceId)) return false;
    instanceIds.add(instanceId);
    return true;
  });
}

function isFiniteNonNegativeNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function isFiniteUnitInterval(value: unknown): value is number {
  return isFiniteNonNegativeNumber(value) && value <= 1;
}

function isNonNegativeInteger(value: unknown): value is number {
  return isFiniteNonNegativeNumber(value) && Number.isInteger(value);
}

function isRosTime(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const stamp = value as Partial<RosTime>;
  return typeof stamp.sec === "number"
    && Number.isSafeInteger(stamp.sec)
    && stamp.sec >= 0
    && isNonNegativeInteger(stamp.nanosec)
    && stamp.nanosec < 1_000_000_000;
}

function isAuthoritativeSimulationState(message: unknown): boolean {
  if (!isBoundedRosPayload(message) || !message || typeof message !== "object" || Array.isArray(message)) return false;
  const state = message as Partial<SimulationState>;
  return (
    typeof state.procedure_id === "string" &&
    state.procedure_id.trim().length > 0 &&
    typeof state.active_bundle === "string" &&
    state.active_bundle.trim().length > 0 &&
    typeof state.running === "boolean" &&
    typeof state.execution_state === "string" &&
    AUTHORITATIVE_SIMULATION_EXECUTION_STATES.has(state.execution_state.trim().toLowerCase()) &&
    typeof state.filtered_phase === "string" &&
    state.filtered_phase.trim().length > 0 &&
    areAuthoritativeInstrumentStates(state.instrument_states)
  );
}

function isAuthoritativeShadowReplayState(message: unknown): boolean {
  if (!isBoundedRosPayload(message) || !message || typeof message !== "object" || Array.isArray(message)) return false;
  const state = message as Partial<ShadowReplayState>;
  const normalizedState = typeof state.state === "string"
    ? state.state.trim().toLowerCase()
    : "";
  return (
    isRosTime(state.stamp) &&
    typeof state.run_id === "string" &&
    (
      !SHADOW_REPLAY_STATES_REQUIRING_RUN_ID.has(normalizedState) ||
      state.run_id.trim().length > 0
    ) &&
    typeof state.case_id === "string" &&
    state.case_id.trim().length > 0 &&
    typeof state.procedure_id === "string" &&
    state.procedure_id.trim().length > 0 &&
    typeof state.state === "string" &&
    AUTHORITATIVE_SHADOW_REPLAY_STATES.has(normalizedState) &&
    typeof state.mode === "string" &&
    AUTHORITATIVE_SHADOW_REPLAY_MODES.has(state.mode.trim().toLowerCase()) &&
    typeof state.loaded === "boolean" &&
    typeof state.running === "boolean" &&
    typeof state.paused === "boolean" &&
    typeof state.completed === "boolean" &&
    (state.loaded || (!state.running && !state.paused && !state.completed)) &&
    isFiniteNonNegativeNumber(state.source_time_sec) &&
    isFiniteNonNegativeNumber(state.duration_sec) &&
    isFiniteNonNegativeNumber(state.image_duration_sec) &&
    isFiniteNonNegativeNumber(state.wall_elapsed_sec) &&
    isFiniteNonNegativeNumber(state.playback_rate) &&
    isFiniteNonNegativeNumber(state.elastic_hold_sec) &&
    typeof state.hold_reason === "string" &&
    typeof state.last_error === "string" &&
    isNonNegativeInteger(state.published_image_count) &&
    isNonNegativeInteger(state.published_transcript_count) &&
    isNonNegativeInteger(state.completed_vlm_count) &&
    isNonNegativeInteger(state.pending_vlm_count) &&
    isNonNegativeInteger(state.active_skill_count)
  );
}

function normalizeWorldState(message: unknown): WorldState {
  if (!isBoundedRosPayload(message)) return DEFAULT_WORLD_STATE;
  const state = message && typeof message === "object" ? (message as Partial<WorldState>) : {};
  return {
    ...DEFAULT_WORLD_STATE,
    ...state,
    bed_robot_arms: normalizeBedRobotArmStates(state.bed_robot_arms),
  };
}

export type OverrideAck = {
  eventType: string;
  toolId: string;
  message: string;
  voiceText?: string;
};

export type OverridePayload = {
  eventType: "request_tool" | "voice_request" | "return_tool";
  requestedTool: string;
  voiceText: string;
  toolLabel: string;
};

export type ControlCommand = "start" | "pause" | "resume" | "stop" | "reset";
export type ShadowReplayMode = "realtime_1x" | "elastic_demo";
export type RuntimeAuthorityStatus =
  | "checking"
  | "connecting"
  | "waiting"
  | "ready"
  | "invalid"
  | "stale"
  | "reconnecting"
  | "offline";

const ROS_PARAM_BOOL = 1;
const ROS_PARAM_STRING = 4;

function mimeTypeFromCompressedFormat(format: string): string {
  const normalized = format.toLowerCase();
  if (normalized.includes("png")) return "image/png";
  if (normalized.includes("webp")) return "image/webp";
  return "image/jpeg";
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

function compressedImageToFrame(message: RosCompressedImage, topic: string): CompressedImageFrame | null {
  const data = message.data;
  if (!data) return null;
  const sizeBytes = typeof data === "string"
    ? Math.round((data.length * 3) / 4)
    : data.length;
  if (!Number.isSafeInteger(sizeBytes) || sizeBytes <= 0 || sizeBytes > MAX_COMPRESSED_IMAGE_BYTES) {
    return null;
  }
  const format = message.format || "jpeg";
  const mimeType = mimeTypeFromCompressedFormat(format);
  const base64 = typeof data === "string" ? data : byteArrayToBase64(data);
  const src = base64.startsWith("data:") ? base64 : `data:${mimeType};base64,${base64}`;
  return {
    src,
    format,
    topic,
    frameId: message.header?.frame_id || "",
    sizeBytes,
    receivedAt: Date.now(),
  };
}

function runtimeStatusMessage(state: SimulationState): string {
  if (state.execution_state === "running" && state.running) {
    return `simulation running on ${state.active_bundle}`;
  }
  if (state.execution_state === "paused" && state.running) {
    return `simulation paused on ${state.active_bundle}`;
  }
  if (state.execution_state === "idle" && !state.running) {
    return "simulation runtime reset to idle";
  }
  if (state.execution_state === "halted" && !state.running) {
    return "simulation stopped";
  }
  return "";
}

function normalizeProviderStatus(value: unknown): ModelProviderStatus | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  const providerId = String(row.provider_id || "").trim();
  if (!providerId) return null;
  return {
    provider_id: providerId.slice(0, 512),
    provider_name: String(row.provider_name || providerId).slice(0, 512),
    endpoint: String(row.endpoint || "").slice(0, 2_048),
    reachable: Boolean(row.reachable),
    status: String(row.status || "").slice(0, 128),
    detail: String(row.detail || "").slice(0, 4_096),
    latency_sec: Number(row.latency_sec || 0),
    model_count: Number(row.model_count || 0),
  };
}

function normalizeModelEntry(value: unknown): ModelCatalogEntry | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  const providerId = String(row.provider_id || "").trim();
  const modelId = String(row.model_id || "").trim();
  if (!providerId || !modelId) return null;
  const availableActions = Array.isArray(row.available_actions)
    ? row.available_actions
        .map((command) => String(command))
        .filter(
          (command): command is ModelRuntimeCommand =>
            command === "load" ||
            command === "unload" ||
            command === "sleep" ||
            command === "wake",
        )
    : [];
  return {
    provider_id: providerId.slice(0, 512),
    provider_name: String(row.provider_name || providerId).slice(0, 512),
    model_id: modelId.slice(0, 512),
    display_name: String(row.display_name || modelId).slice(0, 1_024),
    capability: String(row.capability || "unknown").slice(0, 128),
    load_state: String(row.load_state || "unknown").slice(0, 128),
    selectable: row.selectable === undefined ? true : Boolean(row.selectable),
    detail: String(row.detail || "").slice(0, 4_096),
    runtime_managed: Boolean(row.runtime_managed),
    available_actions: availableActions,
  };
}

function legacyCatalog(modelIds: string[], providerName: string) {
  const provider: ModelProviderStatus = {
    provider_id: "legacy",
    provider_name: providerName,
    endpoint: "",
    reachable: true,
    status: "online",
    detail: "Legacy single-provider catalog",
    latency_sec: 0,
    model_count: modelIds.length,
  };
  const models: ModelCatalogEntry[] = modelIds.map((modelId) => ({
    provider_id: provider.provider_id,
    provider_name: provider.provider_name,
    model_id: modelId,
    display_name: modelId,
    capability: "unknown",
    load_state: "unknown",
    selectable: true,
    detail: "",
    runtime_managed: false,
    available_actions: [],
  }));
  return { provider, models };
}

export function useRosBridge(
  runtimeMode: TaskplannerRuntimeMode,
  connectEnabled = true,
  connectionPending = false,
) {
  const [url, setUrl] = useState(() => runtimeBridgeUrl(runtimeMode));
  const [transportConnected, setTransportConnected] = useState(false);
  const [connected, setConnected] = useState(false);
  const [runtimeAuthorityStatus, setRuntimeAuthorityStatus] =
    useState<RuntimeAuthorityStatus>(connectEnabled ? "connecting" : "offline");
  const [bundle, setBundle] = useState("");
  const [startPhase, setStartPhase] = useState("");
  const [simulationState, setSimulationState] = useState<SimulationState>(DEFAULT_STATE);
  const [worldState, setWorldState] = useState<WorldState>(DEFAULT_WORLD_STATE);
  const [externalBedRobotArmStatus, setExternalBedRobotArmStatus] =
    useState<ValidatedBedRobotArmStatus | null>(null);
  const [surgeonState, setSurgeonState] = useState<SurgeonState>(DEFAULT_SURGEON);
  const [surgeonLlmDecision, setSurgeonLlmDecision] = useState<SurgeonLLMDecision>(DEFAULT_SURGEON_LLM_DECISION);
  const [btDecision, setBtDecision] = useState<BTDecision>(DEFAULT_BT_DECISION);
  const [skillStatus, setSkillStatus] = useState<SkillStatus>(DEFAULT_SKILL_STATUS);
  const [vlmHealth, setVlmHealth] = useState<VLMHealth>(DEFAULT_VLM_HEALTH);
  const [inputSourceStatuses, setInputSourceStatuses] = useState<
    Record<string, InputSourceStatus>
  >({});
  const [vlmResult, setVlmResult] = useState<VLMResult>(DEFAULT_VLM_RESULT);
  const [cam4ToolRequest, setCam4ToolRequest] =
    useState<Cam4ToolRequestObservation>(DEFAULT_CAM4_TOOL_REQUEST);
  const [vlmReducerDecisions, setVlmReducerDecisions] = useState<VLMReducerDecision[]>([]);
  const [vlmImage, setVlmImage] = useState<CompressedImageFrame | null>(null);
  const [vlmCompositeImage, setVlmCompositeImage] =
    useState<CompressedImageFrame | null>(null);
  const [cam1Image, setCam1Image] = useState<CompressedImageFrame | null>(null);
  const [cam2Image, setCam2Image] = useState<CompressedImageFrame | null>(null);
  const [cam3Image, setCam3Image] = useState<CompressedImageFrame | null>(null);
  const [cam4Image, setCam4Image] = useState<CompressedImageFrame | null>(null);
  const [flirImage, setFlirImage] = useState<CompressedImageFrame | null>(null);
  const [cam4PerceptionImage, setCam4PerceptionImage] =
    useState<CompressedImageFrame | null>(null);
  const [flirPerceptionImage, setFlirPerceptionImage] =
    useState<CompressedImageFrame | null>(null);
  const [cam4PerceptionOverlay, setCam4PerceptionOverlay] =
    useState<CompressedImageFrame | null>(null);
  const [flirPerceptionOverlay, setFlirPerceptionOverlay] =
    useState<CompressedImageFrame | null>(null);
  const [perceptionHealth, setPerceptionHealth] =
    useState<PerceptionLayerHealth>(DEFAULT_PERCEPTION_HEALTH);
  const [vlmHealthReceivedAt, setVlmHealthReceivedAt] = useState<number | null>(null);
  const [vlmResultReceivedAt, setVlmResultReceivedAt] = useState<number | null>(null);
  const [vlmModelOptions, setVlmModelOptions] = useState<ModelCatalogEntry[]>([]);
  const [vlmProviderStatuses, setVlmProviderStatuses] = useState<ModelProviderStatus[]>([]);
  const [vlmModelSelection, setVlmModelSelection] = useState<ModelSelection | null>(null);
  const [vlmModelCatalogStatus, setVlmModelCatalogStatus] = useState("loading");
  const [actorModelOptions, setActorModelOptions] = useState<ModelCatalogEntry[]>([]);
  const [actorProviderStatuses, setActorProviderStatuses] = useState<ModelProviderStatus[]>([]);
  const [actorModelSelection, setActorModelSelection] = useState<ModelSelection | null>(null);
  const [actorModelCatalogStatus, setActorModelCatalogStatus] = useState("loading");
  const [events, setEvents] = useState<SimulationEvent[]>([]);
  const [actionPending, setActionPending] = useState("");
  const [actionMessage, setActionMessage] = useState("Ready.");
  const [overrideAck, setOverrideAck] = useState<OverrideAck | null>(null);
  const [actorEnabled, setActorEnabledState] = useState(false);
  const [actorEnabledKnown, setActorEnabledKnown] = useState(false);
  const [shadowReplayState, setShadowReplayState] = useState<ShadowReplayState>(
    DEFAULT_SHADOW_REPLAY_STATE,
  );
  const [shadowTranscript, setShadowTranscript] = useState<SpeechUtterance[]>([]);
  const [shadowGroundTruth, setShadowGroundTruth] =
    useState<ShadowGroundTruthState>(DEFAULT_SHADOW_GROUND_TRUTH);
  const [liveAsrStatus, setLiveAsrStatus] = useState<LiveAsrStatus>(
    DEFAULT_LIVE_ASR_STATUS,
  );
  const [liveAsrStatusReceivedAt, setLiveAsrStatusReceivedAt] = useState<number | null>(null);
  const [liveAsrStatusBridgeUrl, setLiveAsrStatusBridgeUrl] = useState("");
  const [liveAsrControlPending, setLiveAsrControlPending] = useState("");
  const [liveAsrControlMessage, setLiveAsrControlMessage] = useState("");

  const rosRef = useRef<unknown>(null);
  const simulationStateRef = useRef<SimulationState>(DEFAULT_STATE);
  const shadowReplayStateRef = useRef<ShadowReplayState>(
    DEFAULT_SHADOW_REPLAY_STATE,
  );
  const perceptionHealthReceivedRef = useRef(false);
  const perceptionEnabledRef = useRef(false);
  const cam4ToolRequestRef = useRef<Cam4ToolRequestObservation>(
    DEFAULT_CAM4_TOOL_REQUEST,
  );
  const shadowGroundTruthRef = useRef<ShadowGroundTruthState>(
    DEFAULT_SHADOW_GROUND_TRUTH,
  );
  const reconnectTimerRef = useRef<number | null>(null);
  const bridgeGenerationRef = useRef(0);
  const commandReadyGenerationRef = useRef(0);
  const simulationStateReceivedAtRef = useRef(0);
  const shadowReplayStateReceivedAtRef = useRef(0);
  const pendingServiceCancelsRef = useRef(
    new Set<(reason: string) => void>(),
  );
  const bundleDirtyRef = useRef(false);
  const eventSequenceRef = useRef(0);
  const bedRobotArmStatusRef = useRef<ValidatedBedRobotArmStatus | null>(null);
  const suppressEventsUntilRef = useRef(0);
  const actionRunIdRef = useRef(0);
  const actionInFlightRef = useRef(false);
  const actorPolicyRevisionRef = useRef(0);
  const controlRunIdRef = useRef(0);
  const controlInFlightRef = useRef(false);
  const bundleApplyRunIdRef = useRef(0);
  const liveAsrControlRunIdRef = useRef(0);
  const liveAsrControlInFlightRef = useRef(false);
  const pendingCameraFramesRef = useRef(
    new Map<
      (frame: CompressedImageFrame | null) => void,
      CompressedImageFrame
    >(),
  );
  const cameraFlushFrameRef = useRef<number | null>(null);

  const activeBundle = bundle || simulationState.active_bundle;

  useEffect(() => {
    if (runtimeMode === "live") return;
    liveAsrControlRunIdRef.current += 1;
    liveAsrControlInFlightRef.current = false;
    setLiveAsrStatus(DEFAULT_LIVE_ASR_STATUS);
    setLiveAsrStatusReceivedAt(null);
    setLiveAsrStatusBridgeUrl("");
    setLiveAsrControlPending("");
    setLiveAsrControlMessage("");
  }, [runtimeMode]);

  useLayoutEffect(() => {
    // The runtime controller is the authority for which bridge is safe to
    // observe. Do not briefly connect to a stale locally stored mode while the
    // controller is still checking (or while it is switching modes).
    if (!connectEnabled || url !== runtimeBridgeUrl(runtimeMode)) return;

    let disposed = false;
    let connectionTimer: number | null = null;
    let bedRobotArmExpiryTimer: number | null = null;
    let runtimeStateFreshnessTimer: number | null = null;
    let runtimeStateInitialTimer: number | null = null;
    let runtimeStateRecoveryTimer: number | null = null;
    const generation = bridgeGenerationRef.current + 1;
    bridgeGenerationRef.current = generation;
    commandReadyGenerationRef.current = 0;
    simulationStateReceivedAtRef.current = 0;
    shadowReplayStateReceivedAtRef.current = 0;
    for (const cancel of Array.from(pendingServiceCancelsRef.current)) {
      cancel("ROS bridge changed before the service response arrived.");
    }
    actionRunIdRef.current += 1;
    actionInFlightRef.current = false;
    controlRunIdRef.current += 1;
    bundleApplyRunIdRef.current += 1;
    liveAsrControlRunIdRef.current += 1;
    liveAsrControlInFlightRef.current = false;
    simulationStateRef.current = DEFAULT_STATE;
    shadowReplayStateRef.current = DEFAULT_SHADOW_REPLAY_STATE;
    bedRobotArmStatusRef.current = null;
    cam4ToolRequestRef.current = DEFAULT_CAM4_TOOL_REQUEST;
    shadowGroundTruthRef.current = DEFAULT_SHADOW_GROUND_TRUTH;
    perceptionHealthReceivedRef.current = false;
    perceptionEnabledRef.current = false;
    bundleDirtyRef.current = false;
    setTransportConnected(false);
    setConnected(false);
    setRuntimeAuthorityStatus("connecting");
    setBundle("");
    setStartPhase("");
    setSimulationState(DEFAULT_STATE);
    setWorldState(DEFAULT_WORLD_STATE);
    setExternalBedRobotArmStatus(null);
    setSurgeonState(DEFAULT_SURGEON);
    setSurgeonLlmDecision(DEFAULT_SURGEON_LLM_DECISION);
    setBtDecision(DEFAULT_BT_DECISION);
    setSkillStatus(DEFAULT_SKILL_STATUS);
    setVlmHealth(DEFAULT_VLM_HEALTH);
    setInputSourceStatuses({});
    setVlmResult(DEFAULT_VLM_RESULT);
    setVlmReducerDecisions([]);
    setVlmHealthReceivedAt(null);
    setVlmResultReceivedAt(null);
    setEvents([]);
    setActionPending("");
    setActionMessage("Connecting to ROS bridge...");
    setOverrideAck(null);
    setShadowReplayState(DEFAULT_SHADOW_REPLAY_STATE);
    setShadowTranscript([]);
    setShadowGroundTruth(DEFAULT_SHADOW_GROUND_TRUTH);
    setCam1Image(null);
    setCam2Image(null);
    setCam3Image(null);
    setCam4Image(null);
    setFlirImage(null);
    setVlmImage(null);
    setVlmCompositeImage(null);
    setCam4PerceptionImage(null);
    setFlirPerceptionImage(null);
    setCam4PerceptionOverlay(null);
    setFlirPerceptionOverlay(null);
    setPerceptionHealth(DEFAULT_PERCEPTION_HEALTH);
    setCam4ToolRequest(DEFAULT_CAM4_TOOL_REQUEST);
    const ros = new ROSLIB.Ros();
    const isCurrentGeneration = () =>
      !disposed && bridgeGenerationRef.current === generation;
    const clearBedRobotArmExpiry = () => {
      if (bedRobotArmExpiryTimer !== null) {
        window.clearTimeout(bedRobotArmExpiryTimer);
        bedRobotArmExpiryTimer = null;
      }
    };
    const clearBedRobotArmStatus = () => {
      clearBedRobotArmExpiry();
      bedRobotArmStatusRef.current = null;
      setExternalBedRobotArmStatus(null);
    };
    const clearRuntimeStateFreshnessTimer = () => {
      if (runtimeStateFreshnessTimer !== null) {
        window.clearTimeout(runtimeStateFreshnessTimer);
        runtimeStateFreshnessTimer = null;
      }
    };
    const clearRuntimeStateInitialTimer = () => {
      if (runtimeStateInitialTimer !== null) {
        window.clearTimeout(runtimeStateInitialTimer);
        runtimeStateInitialTimer = null;
      }
    };
    const clearRuntimeStateRecoveryTimer = () => {
      if (runtimeStateRecoveryTimer !== null) {
        window.clearTimeout(runtimeStateRecoveryTimer);
        runtimeStateRecoveryTimer = null;
      }
    };
    const scheduleBedRobotArmExpiry = (status: ValidatedBedRobotArmStatus) => {
      clearBedRobotArmExpiry();
      bedRobotArmExpiryTimer = window.setTimeout(() => {
        bedRobotArmExpiryTimer = null;
        if (!isCurrentGeneration()) return;
        if (bedRobotArmStatusRef.current !== status) return;
        bedRobotArmStatusRef.current = null;
        setExternalBedRobotArmStatus(null);
      }, BED_ROBOT_STATUS_MAX_AGE_MS);
    };
    const scheduleReconnect = () => {
      if (!isCurrentGeneration() || reconnectTimerRef.current !== null) return;
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null;
        if (isCurrentGeneration()) {
          ros.connect(url);
        }
      }, 1500);
    };
    const invalidateConnectedSnapshot = (message: string) => {
      clearRuntimeStateFreshnessTimer();
      clearRuntimeStateInitialTimer();
      clearRuntimeStateRecoveryTimer();
      commandReadyGenerationRef.current = 0;
      actionRunIdRef.current += 1;
      actionInFlightRef.current = false;
      controlRunIdRef.current += 1;
      controlInFlightRef.current = false;
      bundleApplyRunIdRef.current += 1;
      liveAsrControlRunIdRef.current += 1;
      liveAsrControlInFlightRef.current = false;
      for (const cancel of Array.from(pendingServiceCancelsRef.current)) {
        cancel(message);
      }
      simulationStateRef.current = DEFAULT_STATE;
      shadowReplayStateRef.current = DEFAULT_SHADOW_REPLAY_STATE;
      simulationStateReceivedAtRef.current = 0;
      shadowReplayStateReceivedAtRef.current = 0;
      setTransportConnected(false);
      setConnected(false);
      setRuntimeAuthorityStatus("reconnecting");
      setBundle("");
      setStartPhase("");
      setSimulationState(DEFAULT_STATE);
      setWorldState(DEFAULT_WORLD_STATE);
      setShadowReplayState(DEFAULT_SHADOW_REPLAY_STATE);
      setEvents([]);
      setActionPending("");
      setCam1Image(null);
      setCam2Image(null);
      setCam3Image(null);
      setCam4Image(null);
      setFlirImage(null);
      clearBedRobotArmStatus();
      perceptionHealthReceivedRef.current = false;
      perceptionEnabledRef.current = false;
      setPerceptionHealth(DEFAULT_PERCEPTION_HEALTH);
      setCam4PerceptionImage(null);
      setFlirPerceptionImage(null);
      setCam4PerceptionOverlay(null);
      setFlirPerceptionOverlay(null);
      setCam4ToolRequest(DEFAULT_CAM4_TOOL_REQUEST);
      cam4ToolRequestRef.current = DEFAULT_CAM4_TOOL_REQUEST;
      setShadowGroundTruth(DEFAULT_SHADOW_GROUND_TRUTH);
      shadowGroundTruthRef.current = DEFAULT_SHADOW_GROUND_TRUTH;
      setLiveAsrStatus(DEFAULT_LIVE_ASR_STATUS);
      setLiveAsrStatusReceivedAt(null);
      setLiveAsrStatusBridgeUrl("");
      setLiveAsrControlPending("");
      setActionMessage(message);
    };
    const forceRuntimeStateReconnect = () => {
      if (!isCurrentGeneration() || commandReadyGenerationRef.current === generation) return;
      invalidateConnectedSnapshot(
        "Fresh runtime state did not arrive. Reconnecting to the ROS bridge...",
      );
      try {
        ros.close();
      } finally {
        scheduleReconnect();
      }
    };
    const scheduleInitialRuntimeStateTimeout = () => {
      clearRuntimeStateInitialTimer();
      runtimeStateInitialTimer = window.setTimeout(() => {
        runtimeStateInitialTimer = null;
        forceRuntimeStateReconnect();
      }, RUNTIME_STATE_INITIAL_TIMEOUT_MS);
    };
    const scheduleRuntimeStateRecovery = () => {
      clearRuntimeStateRecoveryTimer();
      runtimeStateRecoveryTimer = window.setTimeout(() => {
        runtimeStateRecoveryTimer = null;
        forceRuntimeStateReconnect();
      }, RUNTIME_STATE_RECOVERY_TIMEOUT_MS);
    };
    let invalidRuntimeStateNotified = false;
    const invalidateRuntimeAuthority = (message: string) => {
      clearRuntimeStateFreshnessTimer();
      commandReadyGenerationRef.current = 0;
      actionRunIdRef.current += 1;
      actionInFlightRef.current = false;
      controlRunIdRef.current += 1;
      controlInFlightRef.current = false;
      bundleApplyRunIdRef.current += 1;
      liveAsrControlRunIdRef.current += 1;
      liveAsrControlInFlightRef.current = false;
      for (const cancel of Array.from(pendingServiceCancelsRef.current)) {
        cancel(message);
      }
      simulationStateReceivedAtRef.current = 0;
      shadowReplayStateReceivedAtRef.current = 0;
      simulationStateRef.current = DEFAULT_STATE;
      shadowReplayStateRef.current = DEFAULT_SHADOW_REPLAY_STATE;
      setConnected(false);
      setRuntimeAuthorityStatus("invalid");
      setBundle("");
      setStartPhase("");
      setSimulationState(DEFAULT_STATE);
      setWorldState(DEFAULT_WORLD_STATE);
      setShadowReplayState(DEFAULT_SHADOW_REPLAY_STATE);
      setSurgeonState(DEFAULT_SURGEON);
      setActionPending("");
      setActionMessage(message);
      scheduleRuntimeStateRecovery();
    };
    const expireRuntimeState = () => {
      runtimeStateFreshnessTimer = null;
      if (!isCurrentGeneration() || commandReadyGenerationRef.current !== generation) return;
      const receivedAt = runtimeMode === "shadow"
        ? shadowReplayStateReceivedAtRef.current
        : simulationStateReceivedAtRef.current;
      if (receivedAt > 0 && Date.now() - receivedAt <= RUNTIME_STATE_MAX_AGE_MS) {
        scheduleRuntimeStateFreshness();
        return;
      }
      commandReadyGenerationRef.current = 0;
      actionRunIdRef.current += 1;
      actionInFlightRef.current = false;
      controlRunIdRef.current += 1;
      controlInFlightRef.current = false;
      bundleApplyRunIdRef.current += 1;
      liveAsrControlRunIdRef.current += 1;
      liveAsrControlInFlightRef.current = false;
      for (const cancel of Array.from(pendingServiceCancelsRef.current)) {
        cancel("Runtime state heartbeat expired before the service response arrived.");
      }
      setConnected(false);
      setRuntimeAuthorityStatus("stale");
      setActionPending("");
      setActionMessage("Runtime state heartbeat expired. Waiting for a fresh state...");
      if (runtimeMode === "shadow") {
        shadowReplayStateRef.current = DEFAULT_SHADOW_REPLAY_STATE;
        setShadowReplayState(DEFAULT_SHADOW_REPLAY_STATE);
      } else {
        simulationStateRef.current = DEFAULT_STATE;
        setSimulationState(DEFAULT_STATE);
      }
      scheduleRuntimeStateRecovery();
    };
    const scheduleRuntimeStateFreshness = () => {
      clearRuntimeStateFreshnessTimer();
      clearRuntimeStateInitialTimer();
      clearRuntimeStateRecoveryTimer();
      if (!isCurrentGeneration() || commandReadyGenerationRef.current !== generation) return;
      const receivedAt = runtimeMode === "shadow"
        ? shadowReplayStateReceivedAtRef.current
        : simulationStateReceivedAtRef.current;
      if (receivedAt <= 0) return;
      const ageMs = Math.max(0, Date.now() - receivedAt);
      runtimeStateFreshnessTimer = window.setTimeout(
        expireRuntimeState,
        Math.max(0, RUNTIME_STATE_MAX_AGE_MS - ageMs) + 1,
      );
    };

    ros.on("connection", () => {
      if (!isCurrentGeneration()) return;
      commandReadyGenerationRef.current = 0;
      simulationStateReceivedAtRef.current = 0;
      shadowReplayStateReceivedAtRef.current = 0;
      setTransportConnected(true);
      setConnected(false);
      setRuntimeAuthorityStatus("waiting");
      setActionMessage("ROS bridge connected. Waiting for fresh runtime state...");
      scheduleInitialRuntimeStateTimeout();
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    });
    ros.on("close", () => {
      if (!isCurrentGeneration()) return;
      invalidateConnectedSnapshot("ROS bridge disconnected. Reconnecting...");
      scheduleReconnect();
    });
    ros.on("error", () => {
      if (!isCurrentGeneration()) return;
      invalidateConnectedSnapshot("ROS bridge error. Retrying connection...");
      scheduleReconnect();
    });

    const simulationTopic = new ROSLIB.Topic({
      ros,
      name: "/simulation/state",
      messageType: "surgical_msgs/msg/SimulationState",
    });
    const worldTopic = new ROSLIB.Topic({
      ros,
      name: "/twin/world_state",
      messageType: "surgical_msgs/msg/WorldState",
    });
    const bedRobotArmStatusTopic = new ROSLIB.Topic({
      ros,
      name: "/external/bed_robot_arms/status",
      messageType: "surgical_interop_msgs/msg/BedRobotArmStateArray",
    });
    const eventTopic = new ROSLIB.Topic({
      ros,
      name: "/simulation/event",
      messageType: "surgical_msgs/msg/SimulationEvent",
    });
    const surgeonTopic = new ROSLIB.Topic({
      ros,
      name: "/surgeon/state",
      messageType: "surgical_msgs/msg/SurgeonState",
    });
    const surgeonLlmDecisionTopic = new ROSLIB.Topic({
      ros,
      name: "/surgeon/llm_decision",
      messageType: "surgical_msgs/msg/SurgeonLLMDecision",
    });
    const btDecisionTopic = new ROSLIB.Topic({
      ros,
      name: "/bt/decision",
      messageType: "surgical_msgs/msg/BTDecision",
    });
    const skillStatusTopic = new ROSLIB.Topic({
      ros,
      name: "/skill/status",
      messageType: "surgical_msgs/msg/SkillStatus",
    });
    const vlmHealthTopic = new ROSLIB.Topic({
      ros,
      name: "/vlm/health",
      messageType: "surgical_msgs/msg/VLMHealth",
    });
    const vlmResultTopic = new ROSLIB.Topic({
      ros,
      name: "/vlm/result",
      messageType: "surgical_msgs/msg/VLMResult",
    });
    const inputSourceStatusTopics = ["flir", "cam4", "vlm", "speech"].map(
      (sourceId) =>
        new ROSLIB.Topic({
          ros,
          name: `/input/${sourceId}/status`,
          messageType: "surgical_msgs/msg/InputSourceStatus",
        }),
    );
    const vlmReducerTopic = new ROSLIB.Topic({
      ros,
      name: "/vlm/reducer_decisions",
      messageType: "surgical_msgs/msg/VLMReducerDecision",
    });
    const vlmFieldImageTopic = new ROSLIB.Topic({
      ros,
      name: "/surgery/images/field/compressed",
      messageType: "sensor_msgs/msg/CompressedImage",
      compression: ROSBRIDGE_IMAGE_COMPRESSION,
      throttle_rate: 100,
      queue_length: ROSBRIDGE_IMAGE_QUEUE_LENGTH,
    });
    const rawCameraTopics = rawCameraTopicsForMode(runtimeMode);
    const useExternalPreviews = runtimeMode === "live" || runtimeMode === "llm";
    const externalPreviewNames = new Set(Object.values(rawCameraTopics));
    const cameraTopics = [
      {
        name: rawCameraTopics.cam1,
        setter: setCam1Image,
      },
      {
        name: rawCameraTopics.cam2,
        setter: setCam2Image,
      },
      {
        name: rawCameraTopics.cam3,
        setter: setCam3Image,
      },
      {
        name: rawCameraTopics.cam4,
        setter: setCam4Image,
      },
      {
        name: rawCameraTopics.flir,
        setter: setFlirImage,
      },
      {
        name: "/surgery/images/cam4/detected/compressed",
        setter: setCam4PerceptionImage,
      },
      {
        name: "/surgery/images/flir/segmented/compressed",
        setter: setFlirPerceptionImage,
      },
      {
        name: "/surgery/images/cam4/detection_overlay/compressed",
        setter: setCam4PerceptionOverlay,
      },
      {
        name: "/surgery/images/flir/segmentation_overlay/compressed",
        setter: setFlirPerceptionOverlay,
      },
      {
        name: "/surgery/images/vlm/composite/compressed",
        setter: setVlmCompositeImage,
      },
    ].map(({ name, setter }) => {
      const topic = new ROSLIB.Topic({
        ros,
        name,
        messageType: "sensor_msgs/msg/CompressedImage",
        compression: ROSBRIDGE_IMAGE_COMPRESSION,
        throttle_rate: CAMERA_FRAME_THROTTLE_MS,
        queue_length: ROSBRIDGE_IMAGE_QUEUE_LENGTH,
      });
      if (useExternalPreviews && externalPreviewNames.has(name)) {
        configurePreviewImageSubscription(topic);
      }
      topic.subscribe((message: unknown) => {
        if (!isCurrentGeneration()) return;
        if (
          (name === "/surgery/images/cam4/detected/compressed" ||
            name === "/surgery/images/flir/segmented/compressed" ||
            name ===
              "/surgery/images/cam4/detection_overlay/compressed" ||
            name ===
              "/surgery/images/flir/segmentation_overlay/compressed") &&
          perceptionHealthReceivedRef.current &&
          !perceptionEnabledRef.current
        ) {
          return;
        }
        const frame = compressedImageToFrame(
          message as RosCompressedImage,
          name,
        );
        if (!frame) return;
        pendingCameraFramesRef.current.set(setter, frame);
        if (cameraFlushFrameRef.current === null) {
          cameraFlushFrameRef.current = window.requestAnimationFrame(() => {
            cameraFlushFrameRef.current = null;
            const pending = Array.from(
              pendingCameraFramesRef.current.entries(),
            );
            pendingCameraFramesRef.current.clear();
            for (const [applyFrame, nextFrame] of pending) {
              applyFrame(nextFrame);
            }
          });
        }
      });
      return topic;
    });
    const shadowReplayStateTopic = new ROSLIB.Topic({
      ros,
      name: "/shadow/replay_state",
      messageType: "surgical_msgs/msg/ShadowReplayState",
    });
    const perceptionHealthTopic = new ROSLIB.Topic({
      ros,
      name: "/surgery/perception/rfdetr/health",
      messageType: "std_msgs/msg/String",
    });
    const cam4SemanticsTopic = new ROSLIB.Topic({
      ros,
      name: "/surgery/perception/cam4/semantics/json",
      messageType: "std_msgs/msg/String",
    });
    const shadowTranscriptTopic = new ROSLIB.Topic({
      ros,
      name: "/shadow/speech/utterance",
      messageType: "surgical_msgs/msg/SpeechUtterance",
    });
    const shadowTranscriptHistoryTopic = new ROSLIB.Topic({
      ros,
      name: "/shadow/speech/history",
      messageType: "std_msgs/msg/String",
    });
    const shadowGroundTruthTopic = new ROSLIB.Topic({
      ros,
      name: "/shadow/ground_truth/state",
      messageType: "std_msgs/msg/String",
    });
    const liveAsrStatusTopic = new ROSLIB.Topic({
      ros,
      name: "/input/asr/runtime_status",
      messageType: "std_msgs/msg/String",
    });

    simulationTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (runtimeMode !== "shadow") {
        if (!isAuthoritativeSimulationState(message)) {
          if (!invalidRuntimeStateNotified) {
            invalidRuntimeStateNotified = true;
            invalidateRuntimeAuthority("Runtime state payload was invalid. Waiting for a valid state...");
          }
          return;
        }
        invalidRuntimeStateNotified = false;
        simulationStateReceivedAtRef.current = Date.now();
      }
      const receivedState = normalizeSimulationState(message);
      const nextState =
        !receivedState.running && receivedState.execution_state === "idle" && receivedState.recent_events.length
          ? { ...receivedState, recent_events: [] }
          : receivedState;
      simulationStateRef.current = nextState;
      setSimulationState(nextState);
      if (runtimeMode !== "shadow" && ros.isConnected) {
        commandReadyGenerationRef.current = generation;
        setConnected(true);
        setRuntimeAuthorityStatus("ready");
        setActionMessage("ROS bridge connected.");
        scheduleRuntimeStateFreshness();
      }
    });
    worldTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      startTransition(() => {
        setWorldState(normalizeWorldState(message));
      });
    });
    bedRobotArmStatusTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      const status = normalizeBedRobotArmStatus(message);
      if (status === null) return;
      const expectedProcedure = canonicalBedRobotProcedure(
        simulationStateRef.current.active_bundle,
      );
      if (expectedProcedure && status.procedureType !== expectedProcedure) return;
      const current = bedRobotArmStatusRef.current;
      if (
        current &&
        (status.stampMs < current.stampMs ||
          (status.stampMs === current.stampMs &&
            status.revision <= current.revision))
      ) {
        return;
      }
      bedRobotArmStatusRef.current = status;
      scheduleBedRobotArmExpiry(status);
      if (
        current &&
        current.procedureType === status.procedureType &&
        sameBedRobotArmState(current.arms, status.arms)
      ) {
        return;
      }
      startTransition(() => {
        setExternalBedRobotArmStatus(status);
      });
    });
    eventTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (!isBoundedRosPayload(message)) return;
      const receivedAt = Date.now();
      if (receivedAt < suppressEventsUntilRef.current) return;
      eventSequenceRef.current += 1;
      const event = message as SimulationEvent;
      const eventWithUiId = {
        ...event,
        ui_id: [
          receivedAt,
          eventSequenceRef.current,
          event.event_type || "event",
          event.instrument_id || "none",
        ].join("-"),
      };
      startTransition(() => {
        setEvents((current) => [eventWithUiId, ...current].slice(0, 32));
      });
    });
    surgeonTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (!isBoundedRosPayload(message)) return;
      startTransition(() => {
        setSurgeonState(message as SurgeonState);
      });
    });
    surgeonLlmDecisionTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (!isBoundedRosPayload(message)) return;
      startTransition(() => {
        setSurgeonLlmDecision(message as SurgeonLLMDecision);
      });
    });
    btDecisionTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (!isBoundedRosPayload(message)) return;
      startTransition(() => {
        setBtDecision(message as BTDecision);
      });
    });
    skillStatusTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (!isBoundedRosPayload(message)) return;
      startTransition(() => {
        setSkillStatus(message as SkillStatus);
      });
    });
    vlmHealthTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (!isBoundedRosPayload(message)) return;
      setVlmHealth(message as VLMHealth);
      setVlmHealthReceivedAt(Date.now());
    });
    vlmResultTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (!isBoundedRosPayload(message)) return;
      startTransition(() => {
        setVlmResult(message as VLMResult);
        setVlmResultReceivedAt(Date.now());
      });
    });
    inputSourceStatusTopics.forEach((topic) => {
      topic.subscribe((message: unknown) => {
        if (!isCurrentGeneration()) return;
        if (!isBoundedRosPayload(message)) return;
        const status = message as InputSourceStatus;
        const sourceId = String(status.source_id || "").trim().toLowerCase();
        if (!sourceId) return;
        startTransition(() => {
          setInputSourceStatuses((current) => ({
            ...current,
            [sourceId]: status,
          }));
        });
      });
    });
    vlmReducerTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (!isBoundedRosPayload(message)) return;
      startTransition(() => {
        setVlmReducerDecisions((current) => [message as VLMReducerDecision, ...current].slice(0, 8));
      });
    });
    vlmFieldImageTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (
        perceptionHealthReceivedRef.current &&
        !perceptionEnabledRef.current
      ) {
        return;
      }
      const frame = compressedImageToFrame(message as RosCompressedImage, "/surgery/images/field/compressed");
      if (!frame) return;
      startTransition(() => {
        setVlmImage(frame);
      });
    });
    shadowReplayStateTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (!isBoundedRosPayload(message)) return;
      if (runtimeMode === "shadow") {
        if (!isAuthoritativeShadowReplayState(message)) {
          if (!invalidRuntimeStateNotified) {
            invalidRuntimeStateNotified = true;
            invalidateRuntimeAuthority("Replay state payload was invalid. Waiting for a valid state...");
          }
          return;
        }
        invalidRuntimeStateNotified = false;
        shadowReplayStateReceivedAtRef.current = Date.now();
      }
      const next = {
        ...DEFAULT_SHADOW_REPLAY_STATE,
        ...(message as Partial<ShadowReplayState>),
      };
      const previous = shadowReplayStateRef.current;
      const runChanged =
        Boolean(next.run_id) &&
        Boolean(previous.run_id) &&
        next.run_id !== previous.run_id;
      const replayRewound =
        previous.loaded &&
        next.loaded &&
        next.source_time_sec + 0.25 < previous.source_time_sec;
      shadowReplayStateRef.current = next;
      if (runtimeMode === "shadow" && ros.isConnected) {
        commandReadyGenerationRef.current = generation;
        setConnected(true);
        setRuntimeAuthorityStatus("ready");
        setActionMessage("ROS bridge connected.");
        scheduleRuntimeStateFreshness();
      }
      startTransition(() => {
        if (runChanged || replayRewound) {
          setShadowTranscript([]);
          setEvents([]);
          setVlmReducerDecisions([]);
          setVlmResult(DEFAULT_VLM_RESULT);
          setVlmResultReceivedAt(0);
          setVlmImage(null);
          setVlmCompositeImage(null);
          setCam1Image(null);
          setCam2Image(null);
          setCam3Image(null);
          setCam4Image(null);
          setFlirImage(null);
          setCam4PerceptionImage(null);
          setFlirPerceptionImage(null);
          setCam4PerceptionOverlay(null);
          setFlirPerceptionOverlay(null);
          setCam4ToolRequest(DEFAULT_CAM4_TOOL_REQUEST);
          cam4ToolRequestRef.current = DEFAULT_CAM4_TOOL_REQUEST;
          setShadowGroundTruth(DEFAULT_SHADOW_GROUND_TRUTH);
          shadowGroundTruthRef.current = DEFAULT_SHADOW_GROUND_TRUTH;
        }
        setShadowReplayState(next);
        if (next.loaded && next.procedure_id) {
          bundleDirtyRef.current = false;
          setBundle(next.procedure_id);
        }
      });
    });
    perceptionHealthTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      const health = normalizePerceptionHealth(message);
      const wasEnabled = perceptionEnabledRef.current;
      perceptionHealthReceivedRef.current = health.received;
      perceptionEnabledRef.current = health.received && health.enabled;
      setPerceptionHealth(health);
      if (!health.received) return;
      if (!health.enabled) {
        setCam4PerceptionImage(null);
        setFlirPerceptionImage(null);
        setCam4PerceptionOverlay(null);
        setFlirPerceptionOverlay(null);
        setCam4ToolRequest(DEFAULT_CAM4_TOOL_REQUEST);
        cam4ToolRequestRef.current = DEFAULT_CAM4_TOOL_REQUEST;
        return;
      }
      if (health.status !== "ready" && !wasEnabled) {
        setVlmHealth((current) => ({
          ...current,
          healthy: false,
          image_source: "",
          latency_sec: 0,
          last_error: health.lastError || "waiting for fresh RF-DETR frame",
          last_mode: "waiting_for_perception",
        }));
        setVlmHealthReceivedAt(Date.now());
      }
    });
    cam4SemanticsTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      const parsed = normalizeCam4ToolRequest(message);
      const previous = cam4ToolRequestRef.current;
      const startsRequest =
        parsed.available &&
        parsed.state === "request" &&
        previous.state !== "request";
      const observation = {
        ...parsed,
        onsetSourceStampSec:
          parsed.state === "request"
            ? startsRequest
              ? parsed.sourceStampSec
              : previous.onsetSourceStampSec
            : previous.onsetSourceStampSec,
        onsetReceivedAt:
          parsed.state === "request"
            ? startsRequest
              ? parsed.receivedAt
              : previous.onsetReceivedAt
            : previous.onsetReceivedAt,
      };
      cam4ToolRequestRef.current = observation;
      startTransition(() => {
        setCam4ToolRequest(observation);
      });
    });
    shadowGroundTruthTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      const parsed = normalizeShadowGroundTruth(message);
      const previous = shadowGroundTruthRef.current;
      const sameEvent =
        Boolean(parsed.eventId) && parsed.eventId === previous.eventId;
      const observation = {
        ...parsed,
        eventStartReceivedAt:
          sameEvent && previous.eventStartReceivedAt > 0
            ? previous.eventStartReceivedAt
            : parsed.active
              ? parsed.receivedAt
              : 0,
      };
      shadowGroundTruthRef.current = observation;
      startTransition(() => {
        setShadowGroundTruth(observation);
      });
    });
    if (runtimeMode === "live") {
      liveAsrStatusTopic.subscribe((message: unknown) => {
        if (!isCurrentGeneration()) return;
        const parsed = normalizeLiveAsrStatus(message);
        if (!parsed) return;
        setLiveAsrStatus(parsed);
        setLiveAsrStatusReceivedAt(Date.now());
        setLiveAsrStatusBridgeUrl(url);
      });
    }
    shadowTranscriptTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (!isBoundedRosPayload(message)) return;
      const utterance = message as SpeechUtterance;
      if (!utterance.is_final || !utterance.text?.trim()) return;
      const messageRunId = transcriptRunId(utterance.source || "");
      const activeRunId = shadowReplayStateRef.current.run_id;
      if (
        messageRunId &&
        activeRunId &&
        messageRunId !== activeRunId
      ) {
        return;
      }
      startTransition(() => {
        setShadowTranscript((current) =>
          mergeShadowTranscripts(current, [utterance]),
        );
      });
    });
    shadowTranscriptHistoryTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      const history = normalizeShadowTranscriptHistory(message);
      const activeRunId = shadowReplayStateRef.current.run_id;
      if (
        history.runId &&
        activeRunId &&
        history.runId !== activeRunId
      ) {
        return;
      }
      startTransition(() => {
        setShadowTranscript((current) =>
          history.utterances.length === 0
            ? []
            : mergeShadowTranscripts(current, history.utterances),
        );
      });
    });

    rosRef.current = ros;
    connectionTimer = window.setTimeout(() => {
      connectionTimer = null;
      if (isCurrentGeneration()) ros.connect(url);
    }, 0);

    return () => {
      disposed = true;
      if (bridgeGenerationRef.current === generation) {
        commandReadyGenerationRef.current = 0;
        actionInFlightRef.current = false;
        controlInFlightRef.current = false;
        liveAsrControlRunIdRef.current += 1;
        liveAsrControlInFlightRef.current = false;
        simulationStateReceivedAtRef.current = 0;
        shadowReplayStateReceivedAtRef.current = 0;
        simulationStateRef.current = DEFAULT_STATE;
        shadowReplayStateRef.current = DEFAULT_SHADOW_REPLAY_STATE;
        rosRef.current = null;
        setTransportConnected(false);
        setConnected(false);
        setRuntimeAuthorityStatus("offline");
        setSimulationState(DEFAULT_STATE);
        setShadowReplayState(DEFAULT_SHADOW_REPLAY_STATE);
        setActionPending("");
        setActorEnabledKnown(false);
        for (const cancel of Array.from(pendingServiceCancelsRef.current)) {
          cancel("ROS bridge changed before the service response arrived.");
        }
      }
      if (connectionTimer !== null) window.clearTimeout(connectionTimer);
      clearRuntimeStateFreshnessTimer();
      clearRuntimeStateInitialTimer();
      clearRuntimeStateRecoveryTimer();
      clearBedRobotArmExpiry();
      unsubscribeWhileConnected(ros, [
        simulationTopic,
        worldTopic,
        bedRobotArmStatusTopic,
        eventTopic,
        surgeonTopic,
        surgeonLlmDecisionTopic,
        btDecisionTopic,
        skillStatusTopic,
        vlmHealthTopic,
        vlmResultTopic,
        ...inputSourceStatusTopics,
        vlmReducerTopic,
        vlmFieldImageTopic,
        ...cameraTopics,
        shadowReplayStateTopic,
        perceptionHealthTopic,
        cam4SemanticsTopic,
        shadowTranscriptTopic,
        shadowTranscriptHistoryTopic,
        shadowGroundTruthTopic,
        ...(runtimeMode === "live" ? [liveAsrStatusTopic] : []),
      ]);
      pendingCameraFramesRef.current.clear();
      if (cameraFlushFrameRef.current !== null) {
        window.cancelAnimationFrame(cameraFlushFrameRef.current);
        cameraFlushFrameRef.current = null;
      }
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      ros.close();
      if (rosRef.current === ros) rosRef.current = null;
    };
  }, [connectEnabled, runtimeMode, url]);

  useEffect(() => {
    const clearIfStale = (frame: CompressedImageFrame | null) =>
      frame && Date.now() - frame.receivedAt > CAMERA_STALE_AFTER_MS
        ? null
        : frame;
    const staleSweep = window.setInterval(() => {
      setCam1Image(clearIfStale);
      setCam2Image(clearIfStale);
      setCam3Image(clearIfStale);
      setCam4Image(clearIfStale);
      setFlirImage(clearIfStale);
      // VLM field/composite frames are also operator-facing evidence. Do not
      // leave the last model input looking current after that publisher stops.
      setVlmImage(clearIfStale);
      setVlmCompositeImage(clearIfStale);
      // Derived perception frames are also operator-facing camera previews.
      // Clear them on the same heartbeat as raw feeds so a stopped detector
      // cannot leave the last segmentation/detection image looking current.
      setCam4PerceptionImage(clearIfStale);
      setFlirPerceptionImage(clearIfStale);
      setCam4PerceptionOverlay(clearIfStale);
      setFlirPerceptionOverlay(clearIfStale);
    }, 1000);
    return () => window.clearInterval(staleSweep);
  }, []);

  useEffect(() => {
    simulationStateRef.current = simulationState;
  }, [simulationState]);

  useEffect(() => {
    shadowReplayStateRef.current = shadowReplayState;
  }, [shadowReplayState]);

  useEffect(() => {
    if (!bundleDirtyRef.current && simulationState.active_bundle && bundle !== simulationState.active_bundle) {
      setBundle(simulationState.active_bundle);
    }
  }, [simulationState.active_bundle, bundle]);

  useEffect(() => {
    if (simulationState.execution_state !== "idle" || simulationState.running) return;
    setEvents([]);
    setSurgeonState({
      ...DEFAULT_SURGEON,
      procedure_id: simulationState.active_bundle || bundle,
      phase_id: simulationState.filtered_phase,
    });
  }, [simulationState.execution_state, simulationState.running, simulationState.active_bundle, simulationState.filtered_phase, bundle]);

  useEffect(() => {
    setOverrideAck(null);
    const expectedProcedure = canonicalBedRobotProcedure(activeBundle);
    const currentStatus = bedRobotArmStatusRef.current;
    if (
      currentStatus &&
      expectedProcedure &&
      currentStatus.procedureType !== expectedProcedure
    ) {
      bedRobotArmStatusRef.current = null;
      setExternalBedRobotArmStatus(null);
    }
  }, [activeBundle]);

  function setBundleSelection(nextBundle: string) {
    bundleDirtyRef.current = true;
    setBundle(nextBundle);
  }

  function runtimeStateIsFresh(): boolean {
    const receivedAt = runtimeMode === "shadow"
      ? shadowReplayStateReceivedAtRef.current
      : simulationStateReceivedAtRef.current;
    return receivedAt > 0 && Date.now() - receivedAt <= RUNTIME_STATE_MAX_AGE_MS;
  }

  async function callService(
    name: string,
    serviceType: string,
    request: Record<string, unknown>,
    timeoutMs = 20000,
  ) {
    const generation = bridgeGenerationRef.current;
    const ros = rosRef.current as RosServiceConnection;
    if (
      !ros ||
      !ros.isConnected ||
      commandReadyGenerationRef.current !== generation ||
      !runtimeStateIsFresh()
    ) {
      throw new Error("ROS bridge is waiting for a fresh runtime state.");
    }
    return new Promise<Record<string, unknown>>((resolve, reject) => {
      const serviceCallId = `call_service:${name}:${Number(ros.idCounter ?? 0) + 1}`;
      ros.idCounter = Number(ros.idCounter ?? 0) + 1;
      const timeoutSec = Math.max(1, timeoutMs / 1000);
      let timeout = 0;
      let settled = false;
      let cancel = (_reason: string) => {};
      const cleanup = (handler: (message: RosServiceResponseMessage) => void) => {
        window.clearTimeout(timeout);
        pendingServiceCancelsRef.current.delete(cancel);
        if (typeof ros.off === "function") {
          ros.off(serviceCallId, handler);
        } else if (typeof ros.removeListener === "function") {
          ros.removeListener(serviceCallId, handler);
        }
      };
      const handler = (message: RosServiceResponseMessage) => {
        if (settled) return;
        if (
          generation !== bridgeGenerationRef.current ||
          commandReadyGenerationRef.current !== generation ||
          rosRef.current !== ros ||
          !runtimeStateIsFresh()
        ) {
          cancel("ROS bridge changed before the service response arrived.");
          return;
        }
        settled = true;
        cleanup(handler);
        if (message.result === false) {
          reject(new Error(String(message.values || `Service call failed for ${name}.`)));
          return;
        }
        if (typeof message.values !== "object" || message.values === null) {
          resolve({});
          return;
        }
        if (!isBoundedRosPayload(message.values)) {
          reject(new Error(`Service response from ${name} exceeded the UI payload bound.`));
          return;
        }
        resolve(message.values);
      };
      cancel = (reason: string) => {
        if (settled) return;
        settled = true;
        cleanup(handler);
        reject(new Error(reason));
      };
      pendingServiceCancelsRef.current.add(cancel);
      timeout = window.setTimeout(() => {
        cancel(`Timed out waiting for service response from ${name}`);
      }, timeoutMs);
      ros.on(serviceCallId, handler);
      try {
        ros.callOnConnection({
          op: "call_service",
          id: serviceCallId,
          service: name,
          type: serviceType,
          args: new ROSLIB.ServiceRequest(request),
          timeout: timeoutSec,
        });
      } catch (error) {
        cancel(error instanceof Error ? error.message : String(error));
      }
    });
  }

  useEffect(() => {
    let disposed = false;
    let refreshing = false;

    async function refreshVlmModels() {
      if (!connected || refreshing) return;
      refreshing = true;
      try {
        let response: Record<string, unknown>;
        try {
          response = await callService(
            "/real_vlm_node/list_model_catalog",
            "surgical_msgs/srv/ListModelCatalog",
            {},
            10000,
          );
        } catch {
          const legacyResponse = await callService(
            "/real_vlm_node/list_models",
            "surgical_msgs/srv/ListModels",
            {},
            10000,
          );
          if (!Boolean(legacyResponse.success)) {
            throw new Error(String(legacyResponse.message || "VLM model catalog unavailable."));
          }
          const modelIds = Array.isArray(legacyResponse.model_ids)
            ? legacyResponse.model_ids
              .slice(0, MAX_ROS_PAYLOAD_COLLECTION_ITEMS)
              .map((modelId) => String(modelId).slice(0, 512))
              .filter(Boolean)
            : [];
          const fallback = legacyCatalog(modelIds, "OpenAI compatible");
          if (disposed) return;
          setVlmModelOptions(fallback.models);
          setVlmProviderStatuses([fallback.provider]);
          setVlmModelSelection(
            modelIds[0] ? { provider_id: "legacy", model_id: modelIds[0] } : null,
          );
          setVlmModelCatalogStatus(
            String(legacyResponse.message || (modelIds.length ? "connected" : "empty")).slice(0, 4_096),
          );
          return;
        }
        const providers = Array.isArray(response.providers)
          ? response.providers
              .map(normalizeProviderStatus)
              .filter((row): row is ModelProviderStatus => row !== null)
          : [];
        const models = Array.isArray(response.models)
          ? response.models
              .map(normalizeModelEntry)
              .filter((row): row is ModelCatalogEntry => row !== null)
          : [];
        const activeProviderId = String(response.active_provider_id || "").trim().slice(0, 512);
        const activeModelId = String(response.active_model_id || "").trim().slice(0, 512);
        if (disposed) return;
        setVlmModelOptions(models);
        setVlmProviderStatuses(providers);
        setVlmModelSelection(
          activeProviderId && activeModelId
            ? { provider_id: activeProviderId, model_id: activeModelId }
            : null,
        );
        setVlmModelCatalogStatus(
          String(response.message || (models.length ? "connected" : "empty")).slice(0, 4_096),
        );
      } catch (error) {
        if (disposed) return;
        setVlmModelCatalogStatus(error instanceof Error ? error.message : "VLM model catalog unavailable.");
      } finally {
        refreshing = false;
      }
    }

    if (!connected) {
      setVlmModelOptions([]);
      setVlmProviderStatuses([]);
      setVlmModelSelection(null);
      setVlmModelCatalogStatus(
        transportConnected || connectionPending ? "ROS state pending" : "ROS bridge offline",
      );
      return () => {
        disposed = true;
      };
    }

    setVlmModelCatalogStatus("loading");
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void refreshVlmModels();
    };
    refreshWhenVisible();
    const timer = window.setInterval(refreshWhenVisible, 5000);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [connected, connectionPending, transportConnected, url]);

  useEffect(() => {
    let disposed = false;
    let refreshing = false;
    actorPolicyRevisionRef.current += 1;
    setActorEnabledKnown(false);
    if (!connected || runtimeMode === "shadow") {
      return () => {
        disposed = true;
      };
    }

    async function refreshActorEnabled() {
      if (disposed || refreshing || actionInFlightRef.current) return;
      refreshing = true;
      const revision = actorPolicyRevisionRef.current;
      try {
        const response = await callService(
          "/surgeon_actor/get_parameters",
          "rcl_interfaces/srv/GetParameters",
          { names: ["enabled"] },
          10000,
        );
        if (disposed || revision !== actorPolicyRevisionRef.current) return;
        const values = Array.isArray(response.values) ? response.values : [];
        const value = values[0];
        if (
          !value ||
          typeof value !== "object" ||
          Number((value as { type?: unknown }).type) !== ROS_PARAM_BOOL ||
          typeof (value as { bool_value?: unknown }).bool_value !== "boolean"
        ) {
          setActorEnabledKnown(false);
          return;
        }
        setActorEnabledState((value as { bool_value: boolean }).bool_value);
        setActorEnabledKnown(true);
      } catch {
        if (!disposed) setActorEnabledKnown(false);
      } finally {
        refreshing = false;
      }
    }

    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void refreshActorEnabled();
    };
    refreshWhenVisible();
    const timer = window.setInterval(refreshWhenVisible, 10_000);
    document.addEventListener("visibilitychange", refreshWhenVisible);

    return () => {
      disposed = true;
      actorPolicyRevisionRef.current += 1;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [connected, runtimeMode, url]);

  useEffect(() => {
    let disposed = false;
    let refreshing = false;

    async function refreshActorModels() {
      if (!connected || refreshing) return;
      refreshing = true;
      try {
        let response: Record<string, unknown>;
        try {
          response = await callService(
            "/surgeon_actor/list_model_catalog",
            "surgical_msgs/srv/ListModelCatalog",
            {},
            10000,
          );
        } catch {
          const legacyResponse = await callService(
            "/surgeon_actor/list_models",
            "surgical_msgs/srv/ListModels",
            {},
            10000,
          );
          if (!Boolean(legacyResponse.success)) {
            throw new Error(String(legacyResponse.message || "Actor model catalog unavailable."));
          }
          const modelIds = Array.isArray(legacyResponse.model_ids)
            ? legacyResponse.model_ids
              .slice(0, MAX_ROS_PAYLOAD_COLLECTION_ITEMS)
              .map((modelId) => String(modelId).slice(0, 512))
              .filter(Boolean)
            : [];
          const fallback = legacyCatalog(modelIds, "OpenAI compatible");
          if (disposed) return;
          setActorModelOptions(fallback.models);
          setActorProviderStatuses([fallback.provider]);
          setActorModelSelection(
            modelIds[0] ? { provider_id: "legacy", model_id: modelIds[0] } : null,
          );
          setActorModelCatalogStatus(
            String(legacyResponse.message || (modelIds.length ? "connected" : "empty")).slice(0, 4_096),
          );
          return;
        }
        const providers = Array.isArray(response.providers)
          ? response.providers
              .map(normalizeProviderStatus)
              .filter((row): row is ModelProviderStatus => row !== null)
          : [];
        const models = Array.isArray(response.models)
          ? response.models
              .map(normalizeModelEntry)
              .filter((row): row is ModelCatalogEntry => row !== null)
          : [];
        const activeProviderId = String(response.active_provider_id || "").trim().slice(0, 512);
        const activeModelId = String(response.active_model_id || "").trim().slice(0, 512);
        if (disposed) return;
        setActorModelOptions(models);
        setActorProviderStatuses(providers);
        setActorModelSelection(
          activeProviderId && activeModelId
            ? { provider_id: activeProviderId, model_id: activeModelId }
            : null,
        );
        setActorModelCatalogStatus(
          String(response.message || (models.length ? "connected" : "empty")).slice(0, 4_096),
        );
      } catch (error) {
        if (disposed) return;
        setActorModelCatalogStatus(error instanceof Error ? error.message : "Actor model catalog unavailable.");
      } finally {
        refreshing = false;
      }
    }

    if (!connected || runtimeMode !== "llm") {
      setActorModelOptions([]);
      setActorProviderStatuses([]);
      setActorModelSelection(null);
      setActorModelCatalogStatus(
        runtimeMode !== "llm"
          ? "disabled in this runtime mode"
          : transportConnected || connectionPending
            ? "ROS state pending"
            : "ROS bridge offline",
      );
      return () => {
        disposed = true;
      };
    }

    setActorModelCatalogStatus("loading");
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void refreshActorModels();
    };
    refreshWhenVisible();
    const timer = window.setInterval(refreshWhenVisible, 5000);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [connected, connectionPending, runtimeMode, transportConnected, url]);

  function stringParameter(name: string, value: string) {
    return {
      name,
      value: {
        type: ROS_PARAM_STRING,
        string_value: value,
      },
    };
  }

  function boolParameter(name: string, value: boolean) {
    return {
      name,
      value: {
        type: ROS_PARAM_BOOL,
        bool_value: value,
      },
    };
  }

  async function setNodeParameters(
    nodeName: string,
    parameters: Array<ReturnType<typeof stringParameter> | ReturnType<typeof boolParameter>>,
  ) {
    const response = await callService(
      `/${nodeName}/set_parameters`,
      "rcl_interfaces/srv/SetParameters",
      { parameters },
      10000,
    );
    const results = Array.isArray(response.results) ? response.results : [];
    const failed = results.find((result) => result && typeof result === "object" && !(result as { successful?: boolean }).successful);
    if (failed) {
      const reason = String((failed as { reason?: string }).reason || "parameter update rejected");
      throw new Error(reason);
    }
  }

  async function runAction(label: string, work: () => Promise<void>) {
    // UI disabled states can be bypassed by a stale click or automation; keep
    // every side-effecting Mission action single-flight at the bridge boundary.
    if (actionInFlightRef.current) return;
    actionInFlightRef.current = true;
    const runId = actionRunIdRef.current + 1;
    actionRunIdRef.current = runId;
    setActionPending(label);
    setActionMessage(`${label}...`);
    try {
      await work();
    } catch (error) {
      if (actionRunIdRef.current === runId) {
        setActionMessage(error instanceof Error ? error.message : String(error));
      }
    } finally {
      if (actionRunIdRef.current === runId) {
        setActionPending("");
        actionInFlightRef.current = false;
      }
    }
  }

  function clearEventLog(options: { suppressMs?: number } = {}) {
    if (options.suppressMs) {
      suppressEventsUntilRef.current = Date.now() + options.suppressMs;
    }
    eventSequenceRef.current = 0;
    setEvents([]);
    setSimulationState((current) => {
      if (!current.recent_events.length) return current;
      const next = { ...current, recent_events: [] };
      simulationStateRef.current = next;
      return next;
    });
  }

  async function waitForControlTarget(
    command: ControlCommand,
    timeoutMs: number,
    expectedControlRunId = controlRunIdRef.current,
  ) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (controlRunIdRef.current !== expectedControlRunId) {
        throw new Error("Runtime state changed before the control result was confirmed.");
      }
      const current = simulationStateRef.current;
      const reachedTarget =
        (command === "start" && current.running && current.execution_state === "running") ||
        (command === "pause" && current.running && current.execution_state === "paused") ||
        (command === "resume" && current.running && current.execution_state === "running") ||
        (command === "reset" && !current.running && current.execution_state === "idle") ||
        (command === "stop" && !current.running && current.execution_state === "halted");
      if (reachedTarget) {
        return runtimeStatusMessage(current);
      }
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
    throw new Error(
      command === "start"
        ? "Start was accepted, but the runtime did not reach running state."
        : command === "pause"
          ? "Pause was accepted, but the runtime did not reach paused state."
          : command === "resume"
            ? "Resume was accepted, but the runtime did not reach running state."
            : command === "reset"
              ? "Reset was accepted, but the runtime did not reach idle state."
              : "Stop was accepted, but the runtime did not reach halted state.",
    );
  }

  function applyShadowReplayStateJson(
    value: unknown,
    fallbackCaseId = "",
  ): ShadowReplayState | null {
    const stateJson = String(value ?? "").trim();
    if (!stateJson || stateJson.length > MAX_ROS_PAYLOAD_STRING_CHARS) return null;
    try {
      const parsed = JSON.parse(stateJson) as Partial<ShadowReplayState>;
      if (!parsed || typeof parsed !== "object" || !isBoundedRosPayload(parsed)) return null;
      const next: ShadowReplayState = {
        ...DEFAULT_SHADOW_REPLAY_STATE,
        ...parsed,
        case_id: String(
          parsed.case_id ||
            fallbackCaseId ||
            shadowReplayStateRef.current.case_id,
        ),
      };
      shadowReplayStateRef.current = next;
      setShadowReplayState(next);
      if (next.loaded && next.procedure_id) {
        bundleDirtyRef.current = false;
        setBundle(next.procedure_id);
      }
      return next;
    } catch {
      return null;
    }
  }

  async function callShadowReplayControl(
    command: "start" | "pause" | "resume" | "restart" | "stop" | "status",
    options: {
      mode?: ShadowReplayMode;
      playbackRate?: number;
    } = {},
  ) {
    const response = await callService(
      "/shadow/control_replay",
      "surgical_msgs/srv/ControlShadowReplay",
      {
        command,
        mode: options.mode ?? "",
        playback_rate: options.playbackRate ?? 0,
        seek_sec: 0,
      },
      10000,
    );
    if (!Boolean(response.success)) {
      throw new Error(
        String(response.message || `Shadow replay ${command} failed.`),
      );
    }
    applyShadowReplayStateJson(response.state_json);
    return response;
  }

  async function configureShadowReplay(
    mode: ShadowReplayMode,
    playbackRate: number,
  ) {
    await runAction("Updating shadow replay", async () => {
      const response = await callShadowReplayControl("status", {
        mode,
        playbackRate,
      });
      setActionMessage(String(response.message || "Shadow replay updated."));
    });
  }

  async function selectShadowCase(caseId: string) {
    const normalizedCaseId = caseId.trim();
    if (!normalizedCaseId) return;
    await runAction("Selecting shadow case", async () => {
      const response = await callService(
        "/shadow/select_case",
        "surgical_msgs/srv/SelectShadowCase",
        { case_id: normalizedCaseId },
        15000,
      );
      if (!Boolean(response.success)) {
        throw new Error(
          String(response.message || `Unable to select ${normalizedCaseId}.`),
        );
      }

      applyShadowReplayStateJson(response.state_json, normalizedCaseId);

      setShadowTranscript([]);
      setEvents([]);
      setVlmReducerDecisions([]);
      setVlmResult(DEFAULT_VLM_RESULT);
      setVlmResultReceivedAt(0);
      setVlmImage(null);
      setVlmCompositeImage(null);
      setCam1Image(null);
      setCam2Image(null);
      setCam3Image(null);
      setCam4Image(null);
      setFlirImage(null);
      setCam4PerceptionImage(null);
      setFlirPerceptionImage(null);
      setCam4PerceptionOverlay(null);
      setFlirPerceptionOverlay(null);
      setCam4ToolRequest(DEFAULT_CAM4_TOOL_REQUEST);
      cam4ToolRequestRef.current = DEFAULT_CAM4_TOOL_REQUEST;
      setActionMessage(
        String(response.message || `Shadow case ${normalizedCaseId} selected.`),
      );
    });
  }

  async function prepareShadowControl(command: ControlCommand) {
    if (!shadowReplayStateRef.current.loaded) return;
    if (command === "pause") {
      await callShadowReplayControl("pause");
    } else if (command === "reset") {
      await callShadowReplayControl("restart");
      setShadowTranscript([]);
    } else if (command === "stop") {
      await callShadowReplayControl("stop");
    }
  }

  async function finalizeShadowControl(
    command: ControlCommand,
    expectedControlRunId = controlRunIdRef.current,
  ) {
    if (!shadowReplayStateRef.current.loaded) return;
    if (command === "start") {
      await waitForControlTarget("start", 45000, expectedControlRunId);
      if (controlRunIdRef.current !== expectedControlRunId) return;
      await callShadowReplayControl("start");
      setShadowTranscript([]);
    } else if (command === "resume") {
      if (controlRunIdRef.current !== expectedControlRunId) return;
      await callShadowReplayControl("resume");
    }
  }

  async function applyBundle(targetBundle = bundle) {
    const selectedBundle = targetBundle || bundle;
    if (!selectedBundle) return;
    const applyRunId = bundleApplyRunIdRef.current + 1;
    bundleApplyRunIdRef.current = applyRunId;
    const stateAtRequest = simulationStateRef.current;
    setBundle(selectedBundle);
    bundleDirtyRef.current = true;
    await runAction("Applying bundle", async () => {
      const response = await callService(
        "/simulation/select_bundle",
        "surgical_msgs/srv/SelectSimulationBundle",
        {
          bundle_name: selectedBundle,
          restart_if_running: stateAtRequest.running,
        },
        stateAtRequest.running ? 22000 : 12000,
      );
      const success = response.success === undefined ? true : Boolean(response.success);
      if (!success) {
        throw new Error(String(response.message || `Failed to apply ${selectedBundle}.`));
      }
      if (bundleApplyRunIdRef.current !== applyRunId) {
        return;
      }
      const appliedBundle = String(response.active_bundle || selectedBundle);
      bundleDirtyRef.current = false;
      setBundle(appliedBundle);
      setStartPhase("");
      setOverrideAck(null);
      clearEventLog({ suppressMs: 500 });
      setSimulationState((current) => ({
        ...current,
        active_bundle: appliedBundle,
        procedure_id: appliedBundle,
      }));
      setActionMessage(String(response.message || `Bundle switched to ${appliedBundle}.`));
    });
  }

  async function control(command: ControlCommand) {
    // The buttons are disabled while a command is pending, but keep a ref-level
    // single-flight guard as well so rapid clicks or DOM-driven retries cannot
    // submit two conflicting runtime commands before React re-renders.
    if (controlInFlightRef.current) return;
    controlInFlightRef.current = true;
    const controlRunId = controlRunIdRef.current + 1;
    controlRunIdRef.current = controlRunId;
    const label =
      command === "start"
        ? "Starting simulation"
        : command === "pause"
          ? "Pausing simulation"
          : command === "resume"
            ? "Resuming simulation"
            : command === "stop"
              ? "Stopping simulation"
              : "Resetting simulation";
    try {
      await runAction(label, async () => {
        if (command === "start") {
          suppressEventsUntilRef.current = 0;
          clearEventLog();
        }
        if (command === "reset") {
          clearEventLog({ suppressMs: 1200 });
        }
        await prepareShadowControl(command);
        try {
          const response = await callService(
            "/simulation/control",
            "surgical_msgs/srv/ControlSimulation",
            { command, start_phase_id: command === "start" ? startPhase : "" },
            command === "start" ? 45000 : command === "reset" ? 30000 : 20000,
          );
        const success = response.success === undefined ? true : Boolean(response.success);
        if (!success) {
          throw new Error(String(response.message || `${label} failed.`));
        }
        setOverrideAck(null);
        if (command === "reset") {
          clearEventLog({ suppressMs: 1200 });
          setSurgeonState({
            ...DEFAULT_SURGEON,
            procedure_id: simulationState.active_bundle || bundle,
            phase_id: simulationState.filtered_phase,
          });
        }
        const fallbackMessage =
          command === "start"
            ? "simulation started"
            : command === "pause"
              ? "simulation paused"
              : command === "resume"
                ? "simulation resumed"
                : command === "stop"
                  ? "simulation stopped"
                  : "simulation runtime reset to idle";
        const rawMessage = String(response.message || fallbackMessage);
        setActionMessage(rawMessage === "ok" ? fallbackMessage : rawMessage);
        if (rawMessage.endsWith("requested") && command !== "start") {
          const stableMessage = await waitForControlTarget(
            command,
            command === "reset" ? 30000 : 20000,
            controlRunId,
          );
          if (controlRunIdRef.current !== controlRunId) return;
          await finalizeShadowControl(command, controlRunId);
          if (stableMessage) {
            setActionMessage(stableMessage);
          }
          return;
        }
        if (controlRunIdRef.current !== controlRunId) return;
        await finalizeShadowControl(command, controlRunId);
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          if (
            message.includes("Timed out waiting for service response") ||
            message.includes("Timeout exceeded while waiting for service response")
          ) {
            try {
              const stableMessage = await waitForControlTarget(
                command,
                command === "start" ? 45000 : command === "reset" ? 30000 : 20000,
                controlRunId,
              );
              if (command === "reset") {
                setOverrideAck(null);
                clearEventLog({ suppressMs: 1200 });
                const current = simulationStateRef.current;
                setSurgeonState({
                  ...DEFAULT_SURGEON,
                  procedure_id: current.active_bundle || bundle,
                  phase_id: current.filtered_phase,
                });
              }
              if (stableMessage) {
                if (controlRunIdRef.current !== controlRunId) return;
                await finalizeShadowControl(command, controlRunId);
                setActionMessage(stableMessage);
                return;
              }
            } catch {
              // Fall through to the original timeout error if the state topic never reaches the target.
            }
          }
          throw error;
        }
      });
    } finally {
      if (controlRunIdRef.current === controlRunId) {
        controlInFlightRef.current = false;
      }
    }
  }

  async function sendOverride(payload: OverridePayload) {
    await runAction(payload.eventType === "voice_request" ? "Sending voice override" : "Sending surgeon override", async () => {
      const response = await callService(
        "/simulation/inject_surgeon_override",
        "surgical_msgs/srv/InjectSurgeonOverride",
        {
          event_type: payload.eventType,
          requested_tool: payload.requestedTool,
          voice_text: payload.eventType === "voice_request" ? payload.voiceText : "",
          ready_for_handover: payload.eventType !== "return_tool",
          ready_for_retrieval: payload.eventType === "return_tool",
          clear_pending_requests: true,
        },
        12000,
      );
      const success = response.success === undefined ? true : Boolean(response.success);
      if (!success) {
        throw new Error(String(response.message || "Override request failed."));
      }
      const message =
        payload.eventType === "return_tool"
          ? `${payload.toolLabel} return/recovery transaction requested.`
          : payload.eventType === "voice_request"
            ? `${payload.toolLabel} voice handover requested.`
            : `${payload.toolLabel} handover requested.`;
      setOverrideAck({
        eventType: payload.eventType,
        toolId: payload.requestedTool,
        message,
        voiceText: payload.eventType === "voice_request" ? payload.voiceText : "",
      });
      setActionMessage(String(response.message || "Override accepted."));
    });
  }

  async function setVlmModel(selection: ModelSelection) {
    await runAction("Updating VLM model", async () => {
      const selectedEntry = vlmModelOptions.find(
        (entry) =>
          entry.provider_id === selection.provider_id &&
          entry.model_id === selection.model_id,
      );
      const transitionState =
        selectedEntry?.runtime_managed &&
        ["unloaded", "error", "configured", "unknown"].includes(
          selectedEntry.load_state,
        )
          ? "loading"
          : selectedEntry?.load_state === "sleeping"
            ? "waking"
            : "";
      if (transitionState) {
        updateSharedModelRuntimeState(selection, transitionState);
      }
      try {
        if (selection.provider_id === "legacy") {
          await setNodeParameters("real_vlm_node", [
            stringParameter("model_id", selection.model_id),
          ]);
        } else {
          const response = await callService(
            "/real_vlm_node/select_model_provider",
            "surgical_msgs/srv/SelectModelProvider",
            selection,
            900000,
          );
          if (!Boolean(response.success)) {
            throw new Error(
              String(response.message || "VLM provider selection failed."),
            );
          }
        }
      } catch (error) {
        if (transitionState) {
          updateSharedModelRuntimeState(selection, "error");
        }
        throw error;
      }
      setVlmModelSelection(selection);
      setVlmHealth((current) => ({ ...current, model_id: selection.model_id }));
      setActionMessage(`VLM set to ${selection.provider_id} / ${selection.model_id}.`);
    });
  }

  async function setActorModel(selection: ModelSelection) {
    await runAction("Updating LLM surgeon model", async () => {
      const selectedEntry = actorModelOptions.find(
        (entry) =>
          entry.provider_id === selection.provider_id &&
          entry.model_id === selection.model_id,
      );
      const transitionState =
        selectedEntry?.runtime_managed &&
        ["unloaded", "error", "configured", "unknown"].includes(
          selectedEntry.load_state,
        )
          ? "loading"
          : selectedEntry?.load_state === "sleeping"
            ? "waking"
            : "";
      if (transitionState) {
        updateSharedModelRuntimeState(selection, transitionState);
      }
      try {
        if (selection.provider_id === "legacy") {
          await setNodeParameters("surgeon_actor", [
            stringParameter("model_id", selection.model_id),
          ]);
        } else {
          const response = await callService(
            "/surgeon_actor/select_model_provider",
            "surgical_msgs/srv/SelectModelProvider",
            selection,
            900000,
          );
          if (!Boolean(response.success)) {
            throw new Error(
              String(
                response.message ||
                  "LLM surgeon provider selection failed.",
              ),
            );
          }
        }
      } catch (error) {
        if (transitionState) {
          updateSharedModelRuntimeState(selection, "error");
        }
        throw error;
      }
      setActorModelSelection(selection);
      setSurgeonLlmDecision((current) => ({
        ...current,
        model_id: selection.model_id,
      }));
      setActionMessage(`LLM surgeon set to ${selection.provider_id} / ${selection.model_id}.`);
    });
  }

  function updateSharedModelRuntimeState(
    selection: ModelSelection,
    state: string,
  ) {
    const update = (entries: ModelCatalogEntry[]) =>
      entries.map((entry) =>
        entry.provider_id === selection.provider_id &&
        entry.model_id === selection.model_id
          ? {
              ...entry,
              load_state: state,
              available_actions: ["loading", "suspending", "waking", "unloading"].includes(
                state,
              )
                ? []
                : entry.available_actions,
            }
          : entry,
      );
    setVlmModelOptions(update);
    setActorModelOptions(update);
  }

  async function controlModelRuntime(
    nodeName: "real_vlm_node" | "surgeon_actor",
    roleLabel: "VLM" | "actor",
    selection: ModelSelection,
    command: ModelRuntimeCommand,
  ) {
    await runAction(`Updating ${roleLabel} runtime: ${command}`, async () => {
      const response = await callService(
        `/${nodeName}/control_model_runtime`,
        "surgical_msgs/srv/ControlModelRuntime",
        {
          provider_id: selection.provider_id,
          model_id: selection.model_id,
          command,
        },
        900000,
      );
      if (!Boolean(response.success)) {
        throw new Error(String(response.message || `${roleLabel} runtime command failed.`));
      }
      const state = String(response.state || "unknown");
      updateSharedModelRuntimeState(selection, state);
      setActionMessage(
        `${selection.provider_id} / ${selection.model_id}: ${command} accepted (${state}).`,
      );
    });
  }

  async function controlVlmModelRuntime(
    selection: ModelSelection,
    command: ModelRuntimeCommand,
  ) {
    await controlModelRuntime(
      "real_vlm_node",
      "VLM",
      selection,
      command,
    );
  }

  async function controlActorModelRuntime(
    selection: ModelSelection,
    command: ModelRuntimeCommand,
  ) {
    await controlModelRuntime(
      "surgeon_actor",
      "actor",
      selection,
      command,
    );
  }

  async function setActorEnabled(enabled: boolean) {
    if (!connected || !actorEnabledKnown || actionInFlightRef.current) return;
    const revision = actorPolicyRevisionRef.current + 1;
    actorPolicyRevisionRef.current = revision;
    await runAction(enabled ? "Enabling LLM surgeon" : "Disabling LLM surgeon", async () => {
      await setNodeParameters("surgeon_actor", [boolParameter("enabled", enabled)]);
      if (revision !== actorPolicyRevisionRef.current) return;
      setActorEnabledState(enabled);
      setActorEnabledKnown(true);
      setActionMessage(enabled ? "LLM surgeon enabled." : "LLM surgeon disabled.");
    });
  }

  async function setPerceptionEnabled(enabled: boolean) {
    await runAction(
      enabled ? "Enabling object recognition" : "Disabling object recognition",
      async () => {
        setCam4PerceptionImage(null);
        setFlirPerceptionImage(null);
        setCam4PerceptionOverlay(null);
        setFlirPerceptionOverlay(null);
        const response = await callService(
          "/rfdetr_perception_bridge/set_enabled",
          "std_srvs/srv/SetBool",
          { data: enabled },
          10000,
        );
        if (!Boolean(response.success)) {
          throw new Error(
            String(
              response.message ||
                "Object recognition control request was rejected.",
            ),
          );
        }
        setPerceptionHealth((current) => ({
          ...current,
          received: true,
          enabled,
          connected: false,
          status: enabled ? "waiting_for_frame" : "disabled",
          lastError: "",
        }));
        setActionMessage(
          String(
            response.message ||
              (enabled
                ? "Object recognition enabled."
                : "Object recognition disabled."),
          ),
        );
      },
    );
  }

  async function controlLiveAsr(
    operation: "refresh_devices" | "set_route_policy" | "start" | "stop",
    deviceId = -1,
    routePolicy = "",
  ): Promise<LiveAsrControlResult> {
    if (runtimeMode !== "live") {
      return { accepted: false, message: "ASR control is available only in live integration mode." };
    }
    // The panel disables itself while an operation is pending, but a stale DOM
    // event can arrive before React commits that disabled state. Keep the
    // bridge-side guard synchronous and invalidate its completion on a mode or
    // connection generation change so an old ASR response cannot update a new
    // live session.
    if (liveAsrControlInFlightRef.current) {
      return { accepted: false, message: "Another ASR control request is still pending." };
    }
    const generation = bridgeGenerationRef.current;
    if (url !== runtimeBridgeUrl(runtimeMode)) {
      return { accepted: false, message: "ASR bridge is not ready for the live runtime." };
    }
    liveAsrControlInFlightRef.current = true;
    const runId = liveAsrControlRunIdRef.current + 1;
    liveAsrControlRunIdRef.current = runId;
    const isCurrentRun = () =>
      bridgeGenerationRef.current === generation &&
      liveAsrControlRunIdRef.current === runId &&
      runtimeMode === "live" &&
      url === runtimeBridgeUrl(runtimeMode);
    setLiveAsrControlPending(operation);
    setLiveAsrControlMessage("");
    try {
      const response = await callService(
        "/input/asr/control",
        "surgical_msgs/srv/AsrControl",
        {
          operation,
          device_id: deviceId,
          // The browser never selects an endpoint by URL.  The reviewed
          // policy identifier is validated by the Live ASR node, which then
          // chooses a concrete cloud/LAN URL from deployment configuration.
          server_url: "",
          route_policy: routePolicy,
        },
        20000,
      );
      const accepted = Boolean(response.accepted);
      const message = String(response.message || (accepted ? "ASR request accepted." : "ASR request rejected."));
      const rawResult = String(response.result_json ?? "").trim();
      if (rawResult && isCurrentRun()) {
        const parsed = normalizeLiveAsrStatus({ data: rawResult });
        if (parsed) {
          setLiveAsrStatus(parsed);
          setLiveAsrStatusReceivedAt(Date.now());
          setLiveAsrStatusBridgeUrl(url);
        }
      }
      if (isCurrentRun()) setLiveAsrControlMessage(message);
      return { accepted, message };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (isCurrentRun()) setLiveAsrControlMessage(message);
      return { accepted: false, message };
    } finally {
      if (isCurrentRun()) {
        setLiveAsrControlPending("");
        liveAsrControlInFlightRef.current = false;
      }
    }
  }

  const runtimeMessage = runtimeStatusMessage(simulationState);
  const simulationReady = connected && (
    runtimeMode === "shadow"
      ? shadowReplayState.loaded
      : simulationState.instrument_states.length > 0
  );
  const shouldPreferRuntimeMessage =
    !actionPending && Boolean(runtimeMessage) && (actionMessage === "Ready." || actionMessage === "ROS bridge connected.");
  const displayActionMessage = shouldPreferRuntimeMessage ? runtimeMessage : actionMessage;

  return {
    url,
    setUrl,
    transportConnected,
    connected,
    runtimeAuthorityStatus: connectionPending
      ? "checking" as const
      : connectEnabled
        ? runtimeAuthorityStatus
        : "offline" as const,
    bundle,
    setBundleSelection,
    startPhase,
    setStartPhase,
    activeBundle,
    simulationState,
    worldState,
    bedRobotArms: externalBedRobotArmStatus?.arms ?? [],
    surgeonState,
    surgeonLlmDecision,
    btDecision,
    skillStatus,
    vlmHealth,
    inputSourceStatuses,
    vlmResult,
    cam4ToolRequest,
    vlmReducerDecisions,
    vlmImage: vlmCompositeImage ?? vlmImage,
    vlmCompositeImage,
    cam1Image,
    cam2Image,
    cam3Image,
    cam4Image,
    flirImage,
    cam4PerceptionImage,
    flirPerceptionImage,
    cam4PerceptionOverlay,
    flirPerceptionOverlay,
    perceptionHealth,
    vlmHealthReceivedAt,
    vlmResultReceivedAt,
    vlmModelOptions,
    vlmProviderStatuses,
    vlmModelSelection,
    vlmModelCatalogStatus,
    actorModelOptions,
    actorProviderStatuses,
    actorModelSelection,
    actorModelCatalogStatus,
    events,
    actionPending,
    actionMessage: displayActionMessage,
    runtimeMessage,
    simulationReady,
    overrideAck,
    actorEnabled,
    actorEnabledKnown,
    shadowReplayState,
    shadowTranscript,
    shadowGroundTruth,
    liveAsrStatus,
    liveAsrStatusReceivedAt: liveAsrStatusBridgeUrl === url ? liveAsrStatusReceivedAt : null,
    liveAsrControlPending,
    liveAsrControlMessage,
    applyBundle,
    control,
    sendOverride,
    setVlmModel,
    setActorModel,
    controlVlmModelRuntime,
    controlActorModelRuntime,
    setActorEnabled,
    setPerceptionEnabled,
    controlLiveAsr,
    selectShadowCase,
    configureShadowReplay,
  };
}
