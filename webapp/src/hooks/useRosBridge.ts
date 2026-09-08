import { useEffect, useLayoutEffect, useRef, useState, startTransition } from "react";
import ROSLIB from "roslib";

import type {
  BTDecision,
  Cam4ToolRequestObservation,
  CompressedImageFrame,
  ExecutionTrace,
  InputSourceStatus,
  InstrumentState,
  LiveAsrControlResult,
  LiveAsrStatus,
  ModelCatalogEntry,
  ModelProviderStatus,
  ModelRuntimeCommand,
  ModelSelection,
  RankedToolPrediction,
  RosTime,
  ShadowGroundTruthState,
  ShadowReplayState,
  SimulationEvent,
  SimulationState,
  SkillStatus,
  SpeechUtterance,
  SurgeonLLMDecision,
  SurgeonState,
  TtsPlaybackStatus,
  VLMHealth,
  VLMReducerDecision,
  VLMResult,
  WorldState,
} from "../types";
import {
  runtimeBridgeUrl,
  type TaskplannerRuntimeMode,
} from "../runtimeModes";
import type { MissionObservationProfile } from "../runtimeFeatures";
import {
  missionSubscriptionPlan,
  type MissionCameraId,
} from "../ros/missionSubscriptionPlan";
import {
  cameraPreviewContractsForMode,
  externalCameraPreviewContracts,
} from "../ros/cameraPreviewContracts";
import { useLiveCameraMediaBridge } from "./useLiveCameraMediaBridge";
import {
  rfdetrToolViewConfigs,
} from "../ros/rfdetrObservationSources";
import type { VlmRequestToolDetectionEvidence } from "../ros/toolObservationMessages";
import {
  createTypedRfdetrObservationStore,
} from "../ros/typedRfdetrObservationStore";
import {
  appendOperationTrace,
  emptyOperationTraceFeed,
  resetOperationTraceFeed,
  type OperationTraceFeed,
} from "../presentation/operationPresentation";
import {
  normalizeExecutionEndpointSource,
  normalizeExecutionRouteCommandResult,
  normalizeExecutionRouteState,
  normalizeIntegrationReadiness,
  type ExecutionEndpointSource,
  type ExecutionRouteState,
  type IntegrationReadiness,
} from "../ros/runtimeAdmissionMessages";
import { executionRouteSwitchBlockReason } from "../ros/executionRouteControl";
import {
  isBoundedRosPayload,
  MAX_ROS_JSON_PAYLOAD_CHARS,
  MAX_ROS_PAYLOAD_STRING_CHARS,
} from "../ros/rosMessageBounds";
import {
  DEFAULT_LIVE_ASR_STATUS,
  mergeExternalAsrTranscripts,
  normalizeExternalAsrFinal,
  normalizeLiveAsrStatus,
} from "../ros/liveAsrMessages";
import {
  normalizeTtsPlaybackStatus,
  TTS_PLAYBACK_STATUS_TOPIC,
} from "../ros/ttsPlaybackMessages";
import {
  activeToolPolicyStatus,
  configureToolPolicySubscription,
  normalizeToolPolicyStatus,
  TOOL_POLICY_STATUS_TOPIC,
  type ToolPolicyStatus,
} from "../ros/toolPolicyMessages";
import {
  normalizeSurgeryRecordReceipt,
  sameSurgeryRecordReceipt,
  SURGERY_RECORD_RECEIPT_TOPIC,
  type SurgeryRecordReceipt,
} from "../ros/surgeryRecordMessages";
import {
  normalizeRosbagRecordingStatus,
  ROSBAG_RECORDING_CONTROL_SERVICE,
  ROSBAG_RECORDING_CONTROL_SERVICE_TYPE,
  ROSBAG_RECORDING_STATUS_TOPIC,
  sameRosbagRecordingStatus,
  type RosbagRecordingStatus,
} from "../ros/rosbagRecordingMessages";
import {
  createRosbagUiAuditMessage,
  ROSBAG_UI_AUDIT_TOPIC,
  serializeRosbagUiAuditMessage,
  type RosbagUiAuditEventName,
  type RosbagUiPresentation,
} from "../ros/rosbagUiAuditMessages";
import {
  BED_ROBOT_STATUS_MAX_AGE_MS,
  canonicalBedRobotProcedure,
  normalizeBedRobotArmStates,
  normalizeBedRobotArmStatus,
  sameBedRobotArmState,
  type ValidatedBedRobotArmStatus,
} from "../ros/bedRobotArmMessages";
import {
  assertSetParametersAccepted,
  boolParameter,
  parseModelCatalogResponse,
  ROS_PARAMETER_BOOL,
  setParametersRequest,
  stringParameter,
  type ModelCatalogProjection,
  type RosParameter,
} from "../ros/modelCatalogMessages";
import type { ToolBeliefRuntime, ToolBeliefSnapshot } from "../ros/toolBeliefMessages";
import {
  EMPTY_SCENARIO_REVISION_STATE,
  SELECT_BUNDLE_SERVICE,
  SELECT_BUNDLE_SERVICE_TYPE,
  parseScenarioRevisionResult,
  scenarioRevisionApplyAdmission as computeScenarioRevisionApplyAdmission,
  scenarioRevisionApplyRequest,
  scenarioRevisionPreviewRequest,
  type ScenarioRevisionState,
} from "../ros/scenarioRevision";

export {
  INTEGRATION_READINESS_MAX_AGE_MS,
  normalizeExecutionRouteState,
  normalizeIntegrationReadiness,
} from "../ros/runtimeAdmissionMessages";
export type {
  ExecutionEndpointSource,
  ExecutionRouteInitializationState,
  ExecutionRouteSourceEndpointReadiness,
  ExecutionRouteState,
  IntegrationReadiness,
  IntegrationReadinessChecklistItem,
  IntegrationReadinessChecklistStatus,
} from "../ros/runtimeAdmissionMessages";
export type {
  ScenarioRevisionApplyAdmission,
  ScenarioRevisionResult,
  ScenarioRevisionState,
} from "../ros/scenarioRevision";
export { normalizeLiveAsrStatus } from "../ros/liveAsrMessages";
export type { ToolBeliefSnapshot } from "../ros/toolBeliefMessages";
export type {
  TypedRfdetrToolDetectionFrame,
  TypedRfdetrToolDetectionInstance,
  TypedRfdetrToolDetections,
  TypedRfdetrToolViewId,
  VlmRequestToolDetectionEvidence,
  VlmToolDetectionFreshness,
  VlmToolDetectionInstance,
  VlmToolDetectionView,
  VlmToolDetectionViewId,
  VlmToolDetectionVisualAlignment,
} from "../ros/toolObservationMessages";

const DEFAULT_STATE: SimulationState = {
  procedure_run_id: "",
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

// Model state is observational; one low-frequency refresh keeps it current
// without creating a second fast polling lane for optional controls.
const VLM_MODEL_REFRESH_MS = 15_000;
// A start request is accepted asynchronously by the state core.  Keep the
// operator-facing lifecycle responsive across the one or two stale idle
// heartbeats that can already be in rosbridge when the click occurs.  This is
// presentation only: the first non-idle authoritative frame still wins, and
// an unsuccessful request restores the prior observed state immediately.
const OPTIMISTIC_START_STATE_MAX_MS = 1_200;

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
  procedure_run_id: "",
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
  procedure_run_id: "",
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
  procedure_run_id: "",
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
  ranked_tool_predictions: [],
  surgeon_request_tool: "",
  explicit_request_voice_backed: false,
  implicit_request_visible: false,
  implicit_request_tool: "",
  implicit_request_hand_pose: "",
  implicit_request_confidence: 0,
  implicit_request_stability_sec: 0,
  implicit_request_generation: 0,
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

export type ExecutionRouteTransition = {
  state: "idle" | "switching" | "ready" | "failed";
  message: string;
};

const DEFAULT_INTEGRATION_READINESS: IntegrationReadiness | null = null;

const MAX_SKILL_STATUS_HISTORY = 64;

type RosCompressedImage = {
  header?: {
    stamp?: RosTime;
    frame_id?: string;
  };
  format?: string;
  data?: string | number[] | Uint8Array;
};


type RosString = {
  data?: string;
};

const EXECUTION_TRACE_TRANSPORTS = new Set(["action", "service"]);
const EXECUTION_TRACE_STAGES = new Set([
  "sent",
  "accepted",
  "rejected",
  "completed",
  "failed",
  "canceled",
  "cancelled",
  "unknown",
]);

function boundedTraceText(value: unknown, maxLength: number): string {
  return typeof value === "string" ? value.trim().slice(0, maxLength) : "";
}

/** The UI accepts command/result telemetry only from this exact live interval. */
function activeProcedureRunId(state: SimulationState): string {
  return state.running && state.execution_state === "running"
    ? boundedTraceText(state.procedure_run_id, 64)
    : "";
}

function belongsToActiveProcedureRun(
  message: object | null | undefined,
  activeRunId: string,
): boolean {
  if (!activeRunId || !message || typeof message !== "object") return false;
  return boundedTraceText(
    (message as { procedure_run_id?: unknown }).procedure_run_id,
    64,
  ) === activeRunId;
}

/** Reject malformed transport telemetry instead of fabricating dispatch state. */
function normalizeExecutionTrace(message: unknown): ExecutionTrace | null {
  if (!isBoundedRosPayload(message) || !message || typeof message !== "object" || Array.isArray(message)) {
    return null;
  }
  const trace = message as Partial<ExecutionTrace>;
  const sequence = Number(trace.sequence);
  const transport = boundedTraceText(trace.transport, 16).toLowerCase();
  const stage = boundedTraceText(trace.stage, 16).toLowerCase();
  const retractionCommand = Number(trace.retraction_command);
  const retractionTargetSide = Number(trace.retraction_target_side);
  const retractionDistanceM = Number(trace.retraction_distance_m);
  if (!Number.isSafeInteger(sequence) || sequence < 1) return null;
  if (!EXECUTION_TRACE_TRANSPORTS.has(transport) || !EXECUTION_TRACE_STAGES.has(stage)) return null;
  if (typeof trace.dispatch_submitted !== "boolean" || typeof trace.terminal !== "boolean") return null;
  return {
    stamp: trace.stamp,
    sequence,
    command_id: boundedTraceText(trace.command_id, 128),
    procedure_run_id: boundedTraceText(trace.procedure_run_id, 64),
    route: boundedTraceText(trace.route, 48),
    transport,
    endpoint: boundedTraceText(trace.endpoint, 192),
    endpoint_source: boundedTraceText(trace.endpoint_source, 32),
    stage,
    dispatch_submitted: trace.dispatch_submitted,
    terminal: trace.terminal,
    evidence: boundedTraceText(trace.evidence, 48),
    reason_code: boundedTraceText(trace.reason_code, 128),
    retraction_command: Number.isInteger(retractionCommand)
      && retractionCommand >= 1
      && retractionCommand <= 8
      ? retractionCommand
      : 0,
    retraction_target_side: Number.isInteger(retractionTargetSide)
      && retractionTargetSide >= 0
      && retractionTargetSide <= 3
      ? retractionTargetSide
      : 0,
    retraction_distance_m: Number.isFinite(retractionDistanceM)
      && Math.abs(retractionDistanceM) <= 1
      ? retractionDistanceM
      : 0,
    receivedAt: Date.now(),
  };
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

// The surgical-stage views are the operator's live visual confirmation path.
// Keep queue_length=1 to discard stale work if a browser falls behind, while
// leaving throttling disabled so the source's native 15 Hz reaches the stage.
const CAMERA_FRAME_THROTTLE_MS = 0;
// Typed detector messages carry lossless masks that the browser never renders.
// Operator preview pixels remain native-rate on the isolated media lane; the
// optional vector/status projection only needs a bounded latest-value cadence.
const TYPED_RFDETR_UI_THROTTLE_MS = 100;
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

const EXTERNAL_CAMERA_PREVIEW_CONTRACTS = externalCameraPreviewContracts({
  cam1: import.meta.env.VITE_EXTERNAL_CAM1_TOPIC,
  cam2: import.meta.env.VITE_EXTERNAL_CAM2_TOPIC,
  cam3OperatorOverlay:
    import.meta.env.VITE_EXTERNAL_CAM3_OPERATOR_OVERLAY_TOPIC,
  cam4OperatorOverlay:
    import.meta.env.VITE_EXTERNAL_CAM4_OPERATOR_OVERLAY_TOPIC,
  flir: import.meta.env.VITE_EXTERNAL_FLIR_TOPIC,
});

function configurePreviewImageSubscription(topic: any): void {
  // roslib 1.4.1 does not expose rosbridge's per-subscription QoS option.
  // Decorate its connection command so the same preview QoS request is also
  // replayed after a WebSocket reconnect. This is used only for the five
  // browser-only physical-camera previews. Acquisition and perception keep
  // their own independent `/synced` subscriptions; CAM3/CAM4 display the
  // remote final operator overlays without feeding those pixels back into the
  // planner or VLM.
  const sendOnConnection = topic.callForSubscribeAndAdvertise.bind(topic);
  topic.callForSubscribeAndAdvertise = (request: Record<string, unknown>) => {
    sendOnConnection(
      request.op === "subscribe"
        ? { ...request, qos: ROSBRIDGE_PREVIEW_IMAGE_QOS }
        : request,
    );
  };
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

type RosTopicPublisher = {
  advertise?: () => void;
  publish: (message: unknown) => void;
  unadvertise?: () => void;
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
    instrument.mayo_placement_evidence,
    instrument.mayo_evidence_source,
  ];
  const optionalUnitIntervals = [
    instrument.mayo_reuse_confidence,
    instrument.mayo_recovery_confidence,
  ];
  const optionalNonNegativeNumbers = [
    instrument.mayo_reuse_stability_sec,
    instrument.mayo_recovery_stability_sec,
  ];
  return requiredStrings.every(
    (field) => typeof field === "string" && field.trim().length > 0,
  ) &&
    optionalStrings.every(
      (field) => field === undefined || typeof field === "string",
    ) &&
    (instrument.procedure_future_use_expected === undefined || typeof instrument.procedure_future_use_expected === "boolean") &&
    optionalUnitIntervals.every(
      (field) => field === undefined || isFiniteUnitInterval(field),
    ) &&
    optionalNonNegativeNumbers.every(
      (field) => field === undefined || isFiniteNonNegativeNumber(field),
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

function isRosTime(value: unknown): value is RosTime {
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
  const implicitRequestConfidence = Number(state.implicit_request_confidence);
  const implicitRequestStabilitySec = Number(state.implicit_request_stability_sec);
  const implicitRequestGeneration = Number(state.implicit_request_generation);
  return {
    ...DEFAULT_WORLD_STATE,
    ...state,
    implicit_request_visible: state.implicit_request_visible === true,
    implicit_request_tool: typeof state.implicit_request_tool === "string"
      ? state.implicit_request_tool.trim()
      : "",
    implicit_request_hand_pose: typeof state.implicit_request_hand_pose === "string"
      ? state.implicit_request_hand_pose.trim()
      : "",
    implicit_request_confidence:
      Number.isFinite(implicitRequestConfidence)
      && implicitRequestConfidence >= 0
      && implicitRequestConfidence <= 1
        ? implicitRequestConfidence
        : 0,
    implicit_request_stability_sec:
      Number.isFinite(implicitRequestStabilitySec) && implicitRequestStabilitySec >= 0
        ? implicitRequestStabilitySec
        : 0,
    implicit_request_generation:
      Number.isSafeInteger(implicitRequestGeneration) && implicitRequestGeneration >= 0
        ? implicitRequestGeneration
        : 0,
    bed_robot_arms: normalizeBedRobotArmStates(state.bed_robot_arms),
    ranked_tool_predictions: normalizeRankedToolPredictions(state.ranked_tool_predictions),
  };
}

function normalizeRankedToolPredictions(message: unknown): RankedToolPrediction[] {
  if (!Array.isArray(message)) return [];
  const seenRanks = new Set<number>();
  const seenTools = new Set<string>();
  const rows: RankedToolPrediction[] = [];
  for (const value of message.slice(0, 24)) {
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    const row = value as Partial<RankedToolPrediction>;
    const rank = Number(row.rank);
    const instrumentId = String(row.instrument_id || "").trim();
    const confidence = Number(row.confidence);
    const stabilitySec = Number(row.stability_sec);
    if (
      !Number.isInteger(rank)
      || rank < 1
      || rank > 3
      || !instrumentId
      || !Number.isFinite(confidence)
      || confidence < 0
      || confidence > 1
      || !Number.isFinite(stabilitySec)
      || stabilitySec < 0
      || seenRanks.has(rank)
      || seenTools.has(instrumentId)
    ) {
      continue;
    }
    seenRanks.add(rank);
    seenTools.add(instrumentId);
    rows.push({
      rank,
      instrument_id: instrumentId,
      confidence,
      stability_sec: stabilitySec,
    });
  }
  return rows.sort((left, right) => left.rank - right.rank).slice(0, 3);
}

export type ControlCommand = "start" | "pause" | "resume" | "stop" | "reset";
export type ShadowReplayMode = "realtime_1x" | "elastic_demo";
export type RuntimeAuthorityStatus =
  | "checking"
  | "connecting"
  | "waiting"
  | "ready"
  // The runtime controller has deliberately withheld the bridge connection
  // because the selected UI mode does not match the running runtime.
  | "blocked"
  | "invalid"
  | "stale"
  | "reconnecting"
  | "offline";

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
  const headerStamp = message.header?.stamp;
  const sourceStampSec = isRosTime(headerStamp)
    ? headerStamp.sec + headerStamp.nanosec / 1_000_000_000
    : undefined;
  return {
    src,
    format,
    topic,
    frameId: message.header?.frame_id || "",
    sourceStampSec,
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

const DEFAULT_ROSBAG_UI_PRESENTATION: RosbagUiPresentation = {
  language: "ko",
  workspace: "mission",
  stageSurgicalBedCamera: "flir",
  stageSurgicalBedInspecting: false,
  stageIndependentCamera: "cam1",
  stageIndependentInspecting: false,
  stageCam3Inspecting: false,
  surgeryRecordVisible: false,
  surgeryRecordTab: "record",
};

type RosbagUiAuditPublishOptions = {
  value?: string;
  bundle?: string;
  startPhase?: string;
  presentation?: RosbagUiPresentation;
  allowWhenInactive?: boolean;
};

type RosbagPresentationReplayOptions = {
  replayPresentationOnly?: boolean;
  rosbagUiPresentation?: RosbagUiPresentation;
};

export function useRosBridge(
  runtimeMode: TaskplannerRuntimeMode,
  connectEnabled = true,
  connectionPending = false,
  connectionBlocked = false,
  observationProfile: MissionObservationProfile = "extended",
  replayOptions: RosbagPresentationReplayOptions = {},
) {
  const replayPresentationOnly = replayOptions.replayPresentationOnly === true;
  const rosbagUiPresentation =
    replayOptions.rosbagUiPresentation ?? DEFAULT_ROSBAG_UI_PRESENTATION;
  const subscriptionPlan = missionSubscriptionPlan(runtimeMode, observationProfile);
  const cameraPreviewContracts = cameraPreviewContractsForMode(
    runtimeMode,
    EXTERNAL_CAMERA_PREVIEW_CONTRACTS,
  );
  // Live camera pixels have a dedicated, loopback-only latest-frame lane.
  // It is read-only and deliberately remains available through a transient
  // command/runtime-controller status check. The command/state bridge remains
  // authoritative for every non-image topic.
  const liveCameraMedia = useLiveCameraMediaBridge({
    // A rosbag replay republishes the recorded camera topics on the regular
    // rosbridge lane. Keep the production latest-frame bridge out of that
    // read-only replay so the original recorded frames reach the stage.
    enabled: runtimeMode === "live" && !replayPresentationOnly,
  });
  const [url, setUrl] = useState(() => runtimeBridgeUrl(runtimeMode));
  const [transportConnected, setTransportConnected] = useState(false);
  const [connected, setConnected] = useState(false);
  const [runtimeAuthorityStatus, setRuntimeAuthorityStatus] =
    useState<RuntimeAuthorityStatus>(
      connectEnabled ? "connecting" : connectionBlocked ? "blocked" : "offline",
    );
  const [bundle, setBundle] = useState("");
  const [startPhase, setStartPhaseState] = useState("");
  const [simulationState, setSimulationState] = useState<SimulationState>(DEFAULT_STATE);
  const [worldState, setWorldState] = useState<WorldState>(DEFAULT_WORLD_STATE);
  const [toolPolicyStatus, setToolPolicyStatus] = useState<ToolPolicyStatus | null>(null);
  const [externalBedRobotArmStatus, setExternalBedRobotArmStatus] =
    useState<ValidatedBedRobotArmStatus | null>(null);
  const [surgeonState, setSurgeonState] = useState<SurgeonState>(DEFAULT_SURGEON);
  const [surgeonLlmDecision, setSurgeonLlmDecision] = useState<SurgeonLLMDecision>(DEFAULT_SURGEON_LLM_DECISION);
  const [btDecision, setBtDecision] = useState<BTDecision>(DEFAULT_BT_DECISION);
  const [skillStatus, setSkillStatus] = useState<SkillStatus>(DEFAULT_SKILL_STATUS);
  const [skillStatusByCommand, setSkillStatusByCommand] = useState<
    Record<string, SkillStatus>
  >({});
  const [executionTraceFeed, setExecutionTraceFeed] = useState<OperationTraceFeed>(
    emptyOperationTraceFeed,
  );
  const executionTraces = executionTraceFeed.traces;
  const [vlmHealth, setVlmHealth] = useState<VLMHealth>(DEFAULT_VLM_HEALTH);
  const [inputSourceStatuses, setInputSourceStatuses] = useState<
    Record<string, InputSourceStatus>
  >({});
  const [vlmResult, setVlmResult] = useState<VLMResult>(DEFAULT_VLM_RESULT);
  const [vlmRequestToolDetectionEvidence, setVlmRequestToolDetectionEvidence] =
    useState<VlmRequestToolDetectionEvidence | null>(null);
  const [typedRfdetrObservationStore] = useState(
    createTypedRfdetrObservationStore,
  );
  const [toolBeliefs, setToolBeliefs] = useState<ToolBeliefRuntime | null>(null);
  const [cam4ToolRequest, setCam4ToolRequest] =
    useState<Cam4ToolRequestObservation>(DEFAULT_CAM4_TOOL_REQUEST);
  const [vlmReducerDecisions, setVlmReducerDecisions] = useState<VLMReducerDecision[]>([]);
  const [vlmCompositeImage, setVlmCompositeImage] =
    useState<CompressedImageFrame | null>(null);
  const [cam1Image, setCam1Image] = useState<CompressedImageFrame | null>(null);
  const [cam2Image, setCam2Image] = useState<CompressedImageFrame | null>(null);
  const [cam3Image, setCam3Image] = useState<CompressedImageFrame | null>(null);
  const [cam4Image, setCam4Image] = useState<CompressedImageFrame | null>(null);
  const [flirImage, setFlirImage] = useState<CompressedImageFrame | null>(null);
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
  const [ttsPlaybackStatus, setTtsPlaybackStatus] = useState<TtsPlaybackStatus | null>(null);
  const [surgeryRecordReceipt, setSurgeryRecordReceipt] =
    useState<SurgeryRecordReceipt | null>(null);
  const [rosbagRecording, setRosbagRecording] = useState<RosbagRecordingStatus | null>(null);
  const [rosbagRecordingControlPending, setRosbagRecordingControlPending] = useState("");
  const [rosbagRecordingControlMessage, setRosbagRecordingControlMessage] = useState("");
  const [integrationReadiness, setIntegrationReadiness] =
    useState<IntegrationReadiness | null>(DEFAULT_INTEGRATION_READINESS);
  const [integrationReadinessReceivedAt, setIntegrationReadinessReceivedAt] =
    useState<number | null>(null);
  const [integrationReadinessBridgeUrl, setIntegrationReadinessBridgeUrl] =
    useState("");
  const [executionRouteState, setExecutionRouteState] =
    useState<ExecutionRouteState | null>(null);
  const [executionRouteStateReceivedAt, setExecutionRouteStateReceivedAt] =
    useState<number | null>(null);
  const [executionRouteTransition, setExecutionRouteTransition] =
    useState<ExecutionRouteTransition>({ state: "idle", message: "" });
  const [scenarioRevision, setScenarioRevision] = useState<ScenarioRevisionState>(
    EMPTY_SCENARIO_REVISION_STATE,
  );

  const rosRef = useRef<unknown>(null);
  const simulationStateRef = useRef<SimulationState>(DEFAULT_STATE);
  const activeProcedureRunIdRef = useRef("");
  const optimisticStartControlRef = useRef<{
    runId: number;
    previous: SimulationState;
    expiresAt: number;
  } | null>(null);
  const shadowReplayStateRef = useRef<ShadowReplayState>(
    DEFAULT_SHADOW_REPLAY_STATE,
  );
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
  const runtimeBoundServiceCancelsRef = useRef(
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
  const priorityStopInFlightRef = useRef(false);
  const bundleApplyRunIdRef = useRef(0);
  const scenarioRevisionRef = useRef<ScenarioRevisionState>(
    EMPTY_SCENARIO_REVISION_STATE,
  );
  const bundlePreviewInFlightRef = useRef(false);
  const liveAsrControlRunIdRef = useRef(0);
  const liveAsrControlInFlightRef = useRef(false);
  const liveAsrStatusRef = useRef<LiveAsrStatus>(DEFAULT_LIVE_ASR_STATUS);
  const liveAsrStatusReceivedAtRef = useRef(0);
  const surgeryRecordReceiptRef = useRef<SurgeryRecordReceipt | null>(null);
  const rosbagRecordingRef = useRef<RosbagRecordingStatus | null>(null);
  const rosbagRecordingControlRunIdRef = useRef(0);
  const rosbagRecordingControlInFlightRef = useRef(false);
  const rosbagRecordingAuditTopicRef = useRef<RosTopicPublisher | null>(null);
  const rosbagUiPresentationRef = useRef<RosbagUiPresentation>(rosbagUiPresentation);
  const integrationReadinessRef = useRef<IntegrationReadiness | null>(
    DEFAULT_INTEGRATION_READINESS,
  );
  const integrationReadinessReceivedAtRef = useRef(0);
  const executionRouteStateRef = useRef<ExecutionRouteState | null>(null);
  const executionRouteStateReceivedAtRef = useRef(0);
  const executionRouteRunIdRef = useRef(0);
  const pendingCameraFramesRef = useRef(
    new Map<(frame: CompressedImageFrame | null) => void, CompressedImageFrame>(),
  );
  const cameraFlushFrameRef = useRef<number | null>(null);

  useEffect(() => {
    rosbagUiPresentationRef.current = rosbagUiPresentation;
  }, [rosbagUiPresentation]);

  // A locally selected preview candidate must never replace the bundle that
  // the server says is active on the stage or execution projections.
  const activeBundle =
    simulationState.active_bundle || simulationState.procedure_id || "";

  function updateScenarioRevision(next: ScenarioRevisionState) {
    scenarioRevisionRef.current = next;
    setScenarioRevision(next);
  }

  useEffect(() => {
    if (connected) return;
    bundleApplyRunIdRef.current += 1;
    bundlePreviewInFlightRef.current = false;
    updateScenarioRevision(EMPTY_SCENARIO_REVISION_STATE);
  }, [connected, url]);

  useEffect(() => {
    if (runtimeMode === "live") return;
    liveAsrControlRunIdRef.current += 1;
    liveAsrControlInFlightRef.current = false;
    rosbagRecordingControlRunIdRef.current += 1;
    rosbagRecordingControlInFlightRef.current = false;
    rosbagRecordingRef.current = null;
    liveAsrStatusRef.current = DEFAULT_LIVE_ASR_STATUS;
    liveAsrStatusReceivedAtRef.current = 0;
    setLiveAsrStatus(DEFAULT_LIVE_ASR_STATUS);
    setLiveAsrStatusReceivedAt(null);
    setLiveAsrStatusBridgeUrl("");
    setLiveAsrControlPending("");
    setLiveAsrControlMessage("");
    setTtsPlaybackStatus(null);
    surgeryRecordReceiptRef.current = null;
    setSurgeryRecordReceipt(null);
    setRosbagRecording(null);
    setRosbagRecordingControlPending("");
    setRosbagRecordingControlMessage("");
  }, [runtimeMode]);

  useEffect(() => {
    if (runtimeMode === "live") return;
    integrationReadinessRef.current = DEFAULT_INTEGRATION_READINESS;
    integrationReadinessReceivedAtRef.current = 0;
    setIntegrationReadiness(DEFAULT_INTEGRATION_READINESS);
    setIntegrationReadinessReceivedAt(null);
    setIntegrationReadinessBridgeUrl("");
    executionRouteRunIdRef.current += 1;
    executionRouteStateRef.current = null;
    executionRouteStateReceivedAtRef.current = 0;
    setExecutionRouteState(null);
    setExecutionRouteStateReceivedAt(null);
    setExecutionRouteTransition({ state: "idle", message: "" });
  }, [runtimeMode]);

  useLayoutEffect(() => {
    // The runtime controller is the authority for which bridge is safe to
    // observe. Do not briefly connect to a stale locally stored mode while the
    // controller is still checking (or while it is switching modes).
    if (!connectEnabled || url !== runtimeBridgeUrl(runtimeMode)) return;

    const activeSubscriptionPlan = missionSubscriptionPlan(
      runtimeMode,
      observationProfile,
    );
    let disposed = false;
    let connectionTimer: number | null = null;
    let bedRobotArmExpiryTimer: number | null = null;
    let runtimeStateFreshnessTimer: number | null = null;
    let runtimeStateInitialTimer: number | null = null;
    let runtimeStateRecoveryTimer: number | null = null;
    let observedAsrFinals: LiveAsrStatus["finals"] = [];
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
    controlInFlightRef.current = false;
    priorityStopInFlightRef.current = false;
    bundleApplyRunIdRef.current += 1;
    liveAsrControlRunIdRef.current += 1;
    liveAsrControlInFlightRef.current = false;
    rosbagRecordingControlRunIdRef.current += 1;
    rosbagRecordingControlInFlightRef.current = false;
    rosbagRecordingRef.current = null;
    rosbagRecordingAuditTopicRef.current = null;
    liveAsrStatusRef.current = DEFAULT_LIVE_ASR_STATUS;
    liveAsrStatusReceivedAtRef.current = 0;
    executionRouteRunIdRef.current += 1;
    activeProcedureRunIdRef.current = "";
    simulationStateRef.current = DEFAULT_STATE;
    shadowReplayStateRef.current = DEFAULT_SHADOW_REPLAY_STATE;
    integrationReadinessRef.current = DEFAULT_INTEGRATION_READINESS;
    integrationReadinessReceivedAtRef.current = 0;
    executionRouteStateRef.current = null;
    executionRouteStateReceivedAtRef.current = 0;
    bedRobotArmStatusRef.current = null;
    cam4ToolRequestRef.current = DEFAULT_CAM4_TOOL_REQUEST;
    shadowGroundTruthRef.current = DEFAULT_SHADOW_GROUND_TRUTH;
    bundleDirtyRef.current = false;
    setTransportConnected(false);
    setConnected(false);
    setRuntimeAuthorityStatus("connecting");
    setBundle("");
    setStartPhaseState("");
    setSimulationState(DEFAULT_STATE);
    setWorldState(DEFAULT_WORLD_STATE);
    setToolPolicyStatus(null);
    setExternalBedRobotArmStatus(null);
    setSurgeonState(DEFAULT_SURGEON);
    setSurgeonLlmDecision(DEFAULT_SURGEON_LLM_DECISION);
    setBtDecision(DEFAULT_BT_DECISION);
    setSkillStatus(DEFAULT_SKILL_STATUS);
    setSkillStatusByCommand({});
    setExecutionTraceFeed(emptyOperationTraceFeed());
    setVlmHealth(DEFAULT_VLM_HEALTH);
    setTtsPlaybackStatus(null);
    setRosbagRecording(null);
    setRosbagRecordingControlPending("");
    setRosbagRecordingControlMessage("");
    setInputSourceStatuses({});
    setVlmResult(DEFAULT_VLM_RESULT);
    setVlmRequestToolDetectionEvidence(null);
    typedRfdetrObservationStore.clear();
    setVlmReducerDecisions([]);
    setVlmHealthReceivedAt(null);
    setVlmResultReceivedAt(null);
    setEvents([]);
    setActionPending("");
    setActionMessage("Connecting to ROS bridge...");
    setShadowReplayState(DEFAULT_SHADOW_REPLAY_STATE);
    setShadowTranscript([]);
    setShadowGroundTruth(DEFAULT_SHADOW_GROUND_TRUTH);
    setCam1Image(null);
    setCam2Image(null);
    setCam3Image(null);
    setCam4Image(null);
    setFlirImage(null);
    setVlmCompositeImage(null);
    setCam4ToolRequest(DEFAULT_CAM4_TOOL_REQUEST);
    setIntegrationReadiness(DEFAULT_INTEGRATION_READINESS);
    setIntegrationReadinessReceivedAt(null);
    setIntegrationReadinessBridgeUrl("");
    setExecutionRouteState(null);
    setExecutionRouteStateReceivedAt(null);
    setExecutionRouteTransition({ state: "idle", message: "" });
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
      priorityStopInFlightRef.current = false;
      bundleApplyRunIdRef.current += 1;
      liveAsrControlRunIdRef.current += 1;
      liveAsrControlInFlightRef.current = false;
      rosbagRecordingControlRunIdRef.current += 1;
      rosbagRecordingControlInFlightRef.current = false;
      rosbagRecordingRef.current = null;
      liveAsrStatusRef.current = DEFAULT_LIVE_ASR_STATUS;
      liveAsrStatusReceivedAtRef.current = 0;
      executionRouteRunIdRef.current += 1;
      activeProcedureRunIdRef.current = "";
      for (const cancel of Array.from(pendingServiceCancelsRef.current)) {
        cancel(message);
      }
      simulationStateRef.current = DEFAULT_STATE;
      shadowReplayStateRef.current = DEFAULT_SHADOW_REPLAY_STATE;
      integrationReadinessRef.current = DEFAULT_INTEGRATION_READINESS;
      integrationReadinessReceivedAtRef.current = 0;
      executionRouteStateRef.current = null;
      executionRouteStateReceivedAtRef.current = 0;
      simulationStateReceivedAtRef.current = 0;
      shadowReplayStateReceivedAtRef.current = 0;
      setTransportConnected(false);
      setConnected(false);
      setRuntimeAuthorityStatus("reconnecting");
      setBundle("");
      setStartPhaseState("");
      setSimulationState(DEFAULT_STATE);
      setWorldState(DEFAULT_WORLD_STATE);
      setToolPolicyStatus(null);
      setShadowReplayState(DEFAULT_SHADOW_REPLAY_STATE);
      setEvents([]);
      setVlmRequestToolDetectionEvidence(null);
      typedRfdetrObservationStore.clear();
      setSkillStatusByCommand({});
      setExecutionTraceFeed((current) => resetOperationTraceFeed(current));
      setActionPending("");
      setRosbagRecording(null);
      setRosbagRecordingControlPending("");
      setRosbagRecordingControlMessage("");
      setCam1Image(null);
      setCam2Image(null);
      setCam3Image(null);
      setCam4Image(null);
      setFlirImage(null);
      clearBedRobotArmStatus();
      setCam4ToolRequest(DEFAULT_CAM4_TOOL_REQUEST);
      cam4ToolRequestRef.current = DEFAULT_CAM4_TOOL_REQUEST;
      setShadowGroundTruth(DEFAULT_SHADOW_GROUND_TRUTH);
      shadowGroundTruthRef.current = DEFAULT_SHADOW_GROUND_TRUTH;
      setLiveAsrStatus(DEFAULT_LIVE_ASR_STATUS);
      setLiveAsrStatusReceivedAt(null);
      setLiveAsrStatusBridgeUrl("");
      observedAsrFinals = [];
      setLiveAsrControlPending("");
      setTtsPlaybackStatus(null);
      setIntegrationReadiness(DEFAULT_INTEGRATION_READINESS);
      setIntegrationReadinessReceivedAt(null);
      setIntegrationReadinessBridgeUrl("");
      setExecutionRouteState(null);
      setExecutionRouteStateReceivedAt(null);
      setExecutionRouteTransition({ state: "idle", message: "" });
      setActionMessage(message);
    };
    const markRuntimeStateUnavailable = () => {
      if (!isCurrentGeneration() || commandReadyGenerationRef.current === generation) return;
      setConnected(false);
      setRuntimeAuthorityStatus("stale");
      if (!actionInFlightRef.current) {
        setActionMessage("ROS transport is online. Waiting for fresh runtime state...");
      }
    };
    const scheduleInitialRuntimeStateTimeout = () => {
      clearRuntimeStateInitialTimer();
      runtimeStateInitialTimer = window.setTimeout(() => {
        runtimeStateInitialTimer = null;
        markRuntimeStateUnavailable();
      }, RUNTIME_STATE_INITIAL_TIMEOUT_MS);
    };
    const scheduleRuntimeStateRecovery = () => {
      clearRuntimeStateRecoveryTimer();
      runtimeStateRecoveryTimer = window.setTimeout(() => {
        runtimeStateRecoveryTimer = null;
        markRuntimeStateUnavailable();
      }, RUNTIME_STATE_RECOVERY_TIMEOUT_MS);
    };
    let invalidRuntimeStateNotified = false;
    const invalidateRuntimeAuthority = (message: string) => {
      clearRuntimeStateFreshnessTimer();
      commandReadyGenerationRef.current = 0;
      controlRunIdRef.current += 1;
      controlInFlightRef.current = false;
      priorityStopInFlightRef.current = false;
      bundleApplyRunIdRef.current += 1;
      executionRouteRunIdRef.current += 1;
      activeProcedureRunIdRef.current = "";
      for (const cancel of Array.from(runtimeBoundServiceCancelsRef.current)) {
        cancel(message);
      }
      simulationStateReceivedAtRef.current = 0;
      shadowReplayStateReceivedAtRef.current = 0;
      simulationStateRef.current = DEFAULT_STATE;
      shadowReplayStateRef.current = DEFAULT_SHADOW_REPLAY_STATE;
      setConnected(false);
      setRuntimeAuthorityStatus("invalid");
      setBundle("");
      setStartPhaseState("");
      setSimulationState(DEFAULT_STATE);
      setWorldState(DEFAULT_WORLD_STATE);
      setToolPolicyStatus(null);
      setShadowReplayState(DEFAULT_SHADOW_REPLAY_STATE);
      setSurgeonState(DEFAULT_SURGEON);
      setVlmRequestToolDetectionEvidence(null);
      if (!actionInFlightRef.current) setActionMessage(message);
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
      controlRunIdRef.current += 1;
      controlInFlightRef.current = false;
      priorityStopInFlightRef.current = false;
      bundleApplyRunIdRef.current += 1;
      activeProcedureRunIdRef.current = "";
      for (const cancel of Array.from(runtimeBoundServiceCancelsRef.current)) {
        cancel("Runtime state heartbeat expired before the service response arrived.");
      }
      setConnected(false);
      setRuntimeAuthorityStatus("stale");
      setVlmRequestToolDetectionEvidence(null);
      if (!actionInFlightRef.current) {
        setActionMessage("Runtime state heartbeat expired. Waiting for a fresh state...");
      }
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
      // Observations belong to their owners, not to /simulation/state. A
      // missing scenario heartbeat must not prevent camera/detector startup.
      startHeavySubscriptions();
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
    const toolPolicyTopic = new ROSLIB.Topic({
      ros,
      name: TOOL_POLICY_STATUS_TOPIC,
      messageType: "std_msgs/msg/String",
      queue_length: 1,
    });
    configureToolPolicySubscription(toolPolicyTopic);
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
    const surgeonLlmDecisionTopic = activeSubscriptionPlan.surgeonLlmDecision
      ? new ROSLIB.Topic({
          ros,
          name: "/surgeon/llm_decision",
          messageType: "surgical_msgs/msg/SurgeonLLMDecision",
        })
      : null;
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
    const executionTraceTopic = new ROSLIB.Topic({
      ros,
      name: "/surgery/execution_trace",
      messageType: "surgical_msgs/msg/ExecutionTrace",
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
    const vlmRequestContextTopic = activeSubscriptionPlan.vlmRequestContext
      ? new ROSLIB.Topic({
          ros,
          name: "/context/vlm_request_context",
          messageType: "surgical_msgs/msg/VLMRequestContext",
        })
      : null;
    const inputSourceStatusTopics = activeSubscriptionPlan.inputSourceStatuses
      ? ["flir", "cam4", "vlm", "speech"].map(
          (sourceId) =>
            new ROSLIB.Topic({
              ros,
              name: `/input/${sourceId}/status`,
              messageType: "surgical_msgs/msg/InputSourceStatus",
            }),
        )
      : [];
    const vlmReducerTopic = activeSubscriptionPlan.vlmReducerDecisions
      ? new ROSLIB.Topic({
          ros,
          name: "/vlm/reducer_decisions",
          messageType: "surgical_msgs/msg/VLMReducerDecision",
        })
      : null;
    const useExternalPreviews = runtimeMode === "live" || runtimeMode === "llm";
    const externalPreviewNames = new Set(
      Object.values(cameraPreviewContracts).map(({ topic }) => topic),
    );
    const cameraPreviewSet = new Set<MissionCameraId>(
      activeSubscriptionPlan.cameraPreviews,
    );
    // A Live operator no longer receives camera JPEGs through generic 9090.
    // The dedicated 9095 lane owns those five subscriptions independently;
    // Debug/replay/LLM keep their existing in-bridge preview behavior.
    const cameraPreviewSubscriptions = runtimeMode === "live" && !replayPresentationOnly
      ? []
      : [
          {
            cameraId: "cam1" as const,
            name: cameraPreviewContracts.cam1.topic,
            setter: setCam1Image,
          },
          {
            cameraId: "cam2" as const,
            name: cameraPreviewContracts.cam2.topic,
            setter: setCam2Image,
          },
          {
            cameraId: "cam3" as const,
            name: cameraPreviewContracts.cam3.topic,
            setter: setCam3Image,
          },
          {
            cameraId: "cam4" as const,
            name: cameraPreviewContracts.cam4.topic,
            setter: setCam4Image,
          },
          {
            cameraId: "flir" as const,
            name: cameraPreviewContracts.flir.topic,
            setter: setFlirImage,
          },
        ].filter(({ cameraId }) => cameraPreviewSet.has(cameraId));
    const modelVisualSubscriptions = activeSubscriptionPlan.vlmModelVisual
      ? [{
        // This is the model-ready visual stream only. RF-DETR tool detections
        // reach the VLM as typed CAM3/CAM4 observation facts, not painted
        // detector pixels. Operator overlays remain on their own topics.
        name: "/taskplanner/internal/vlm/model_visual/compressed",
      }]
      : [];
    const cameraSubscriptions = [
      ...cameraPreviewSubscriptions.map(({ name, setter }) => ({
        kind: "stage-camera" as const,
        name,
        setter,
      })),
      ...modelVisualSubscriptions.map(({ name }) => ({
        kind: "model-visual" as const,
        name,
      })),
    ].map((subscription) => {
      const { name } = subscription;
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
      const onMessage = (message: unknown) => {
        if (!isCurrentGeneration()) return;
        const frame = compressedImageToFrame(
          message as RosCompressedImage,
          name,
        );
        if (!frame) return;
        if (subscription.kind === "stage-camera") {
          pendingCameraFramesRef.current.set(subscription.setter, frame);
          if (cameraFlushFrameRef.current === null) {
            cameraFlushFrameRef.current = window.requestAnimationFrame(() => {
              cameraFlushFrameRef.current = null;
              const pending = Array.from(pendingCameraFramesRef.current.entries());
              pendingCameraFramesRef.current.clear();
              for (const [applyFrame, nextFrame] of pending) applyFrame(nextFrame);
            });
          }
          return;
        }
        setVlmCompositeImage(frame);
      };
      return { topic, onMessage };
    });
    const cameraTopics = cameraSubscriptions.map(({ topic }) => topic);
    let observationNormalizers: typeof import("../ros/toolObservationMessages") | null = null;
    const typedRfdetrToolObservationSubscriptions = rfdetrToolViewConfigs(
      activeSubscriptionPlan.rfdetrObservationSource,
    ).map(
      (config) => {
        const topic = new ROSLIB.Topic({
          ros,
          name: config.topic,
          messageType: "surgical_perception_msgs/msg/ToolObservation2DArray",
          // Detector outputs are for display only. Keep the newest completed
          // observation instead of allowing a slow browser to accumulate its
          // mask-bearing upstream messages.
          throttle_rate: TYPED_RFDETR_UI_THROTTLE_MS,
          queue_length: ROSBRIDGE_IMAGE_QUEUE_LENGTH,
        });
        const onMessage = (message: unknown) => {
          if (!isCurrentGeneration() || !observationNormalizers) return;
          const frame = observationNormalizers.normalizeTypedRfdetrToolDetections(message, config);
          if (!frame) return;
          typedRfdetrObservationStore.ingest(frame);
        };
        return { topic, onMessage };
      },
    );
    const typedRfdetrToolObservationTopics =
      typedRfdetrToolObservationSubscriptions.map(({ topic }) => topic);
    const shadowReplayStateTopic = activeSubscriptionPlan.shadowReplay
      ? new ROSLIB.Topic({
          ros,
          name: "/shadow/replay_state",
          messageType: "surgical_msgs/msg/ShadowReplayState",
        })
      : null;
    const cam4SemanticsTopic = activeSubscriptionPlan.cam4Semantics
      ? new ROSLIB.Topic({
          ros,
          name: "/surgery/perception/cam4/semantics/json",
          messageType: "std_msgs/msg/String",
        })
      : null;
    const shadowTranscriptTopic = activeSubscriptionPlan.shadowReplay
      ? new ROSLIB.Topic({
          ros,
          name: "/shadow/speech/utterance",
          messageType: "surgical_msgs/msg/SpeechUtterance",
        })
      : null;
    const shadowTranscriptHistoryTopic = activeSubscriptionPlan.shadowReplay
      ? new ROSLIB.Topic({
          ros,
          name: "/shadow/speech/history",
          messageType: "std_msgs/msg/String",
        })
      : null;
    const shadowGroundTruthTopic = activeSubscriptionPlan.shadowReplay
      ? new ROSLIB.Topic({
          ros,
          name: "/shadow/ground_truth/state",
          messageType: "std_msgs/msg/String",
        })
      : null;
    const liveAsrStatusTopic = new ROSLIB.Topic({
      ros,
      name: "/input/asr/runtime_status",
      messageType: "std_msgs/msg/String",
    });
    // CommandRouter republishes every admitted final on this observer-only
    // lane. Keep it in the primary bridge as well as the isolated ASR bridge
    // so operationPresentation can project the same sentence onto the stage.
    const liveAsrObservedFinalTopic = new ROSLIB.Topic({
      ros,
      name: "/surgery/audio/observed_utterance",
      messageType: "surgical_msgs/msg/SpeechUtterance",
      queue_length: 20,
    });
    const ttsPlaybackStatusTopic = new ROSLIB.Topic({
      ros,
      name: TTS_PLAYBACK_STATUS_TOPIC,
      messageType: "surgical_msgs/msg/TTSPlaybackStatus",
      queue_length: 1,
    });
    const surgeryRecordReceiptTopic = new ROSLIB.Topic({
      ros,
      name: SURGERY_RECORD_RECEIPT_TOPIC,
      messageType: "std_msgs/msg/String",
      queue_length: 1,
    });
    const rosbagRecordingStatusTopic = new ROSLIB.Topic({
      ros,
      name: ROSBAG_RECORDING_STATUS_TOPIC,
      messageType: "std_msgs/msg/String",
      queue_length: 1,
    });
    const rosbagRecordingAuditTopic = runtimeMode === "live" && !replayPresentationOnly
      ? new ROSLIB.Topic({
          ros,
          name: ROSBAG_UI_AUDIT_TOPIC,
          messageType: "std_msgs/msg/String",
          queue_length: 10,
        })
      : null;
    const integrationReadinessTopic = new ROSLIB.Topic({
      ros,
      name: "/integration/readiness",
      messageType: "std_msgs/msg/String",
    });
    const executionRouteStateTopic = new ROSLIB.Topic({
      ros,
      name: "/integration/execution_route/state",
      messageType: "std_msgs/msg/String",
    });
    let toolBeliefTopic: { unsubscribe: () => void } | null = null;

    let heavySubscriptionsStarted = false;
    const startHeavySubscriptions = () => {
      if (
        heavySubscriptionsStarted ||
        !isCurrentGeneration() ||
        !ros.isConnected
      ) {
        return;
      }
      heavySubscriptionsStarted = true;
      cameraSubscriptions.forEach(({ topic, onMessage }) => {
        topic.subscribe(onMessage);
      });
      // Display-only geometry/context parsing is loaded after connection,
      // outside the mission entry/control bundle. Subscribe only after it is
      // ready so retained context is not lost or raw masks queued in JS.
      void import("../ros/toolObservationMessages").then((normalizers) => {
        if (!isCurrentGeneration()) return;
        observationNormalizers = normalizers;
        typedRfdetrToolObservationSubscriptions.forEach(({ topic, onMessage }) => {
          topic.subscribe(onMessage);
        });
        vlmRequestContextTopic?.subscribe((message: unknown) => {
          if (!isCurrentGeneration()) return;
          if (!belongsToActiveProcedureRun(message as object, activeProcedureRunIdRef.current)) return;
          const parsed = normalizers.normalizeVlmRequestToolDetectionEvidence(message);
          startTransition(() => setVlmRequestToolDetectionEvidence(parsed));
        });
      }).catch((error: unknown) => {
        if (!isCurrentGeneration()) return;
        typedRfdetrObservationStore.clear();
        setVlmRequestToolDetectionEvidence(null);
        console.warn("Optional tool-observation display could not load.", error);
      });
    };

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
      if (runtimeMode !== "shadow") {
        const nextRunId = activeProcedureRunId(nextState);
        if (nextRunId !== activeProcedureRunIdRef.current) {
          activeProcedureRunIdRef.current = nextRunId;
          // Clear every presentation cache at *both* edges of a run.  A
          // delayed status/trace is then additionally rejected by its typed
          // procedure_run_id below; clearing alone is never treated as a
          // correctness fence.
          startTransition(() => {
            setSurgeonState(DEFAULT_SURGEON);
            setSurgeonLlmDecision(DEFAULT_SURGEON_LLM_DECISION);
            setBtDecision(DEFAULT_BT_DECISION);
            setSkillStatus(DEFAULT_SKILL_STATUS);
            setSkillStatusByCommand({});
            setExecutionTraceFeed(emptyOperationTraceFeed());
            setEvents([]);
            setVlmResult(DEFAULT_VLM_RESULT);
            setVlmRequestToolDetectionEvidence(null);
            setVlmReducerDecisions([]);
          });
        }
      }
      const optimisticStart = optimisticStartControlRef.current;
      const preserveOptimisticStart = Boolean(
        optimisticStart
          && optimisticStart.runId === controlRunIdRef.current
          && Date.now() < optimisticStart.expiresAt
          && !nextState.running
          && nextState.execution_state === "idle",
      );
      if (!preserveOptimisticStart) {
        if (optimisticStart?.runId === controlRunIdRef.current) {
          optimisticStartControlRef.current = null;
        }
        simulationStateRef.current = nextState;
        setSimulationState(nextState);
      }
      if (runtimeMode !== "shadow" && ros.isConnected) {
        commandReadyGenerationRef.current = generation;
        setConnected(true);
        setRuntimeAuthorityStatus("ready");
        setActionMessage("ROS bridge connected.");
        scheduleRuntimeStateFreshness();
        startHeavySubscriptions();
      }
    });
    worldTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      startTransition(() => {
        setWorldState(normalizeWorldState(message));
      });
    });
    toolPolicyTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      const status = normalizeToolPolicyStatus(message);
      startTransition(() => setToolPolicyStatus(status));
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
      if (!belongsToActiveProcedureRun(event, activeProcedureRunIdRef.current)) return;
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
      if (!belongsToActiveProcedureRun(message as object, activeProcedureRunIdRef.current)) return;
      startTransition(() => {
        setSurgeonState(message as SurgeonState);
      });
    });
    surgeonLlmDecisionTopic?.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (!isBoundedRosPayload(message)) return;
      if (!belongsToActiveProcedureRun(message as object, activeProcedureRunIdRef.current)) return;
      startTransition(() => {
        setSurgeonLlmDecision(message as SurgeonLLMDecision);
      });
    });
    btDecisionTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (!isBoundedRosPayload(message)) return;
      if (!belongsToActiveProcedureRun(message as object, activeProcedureRunIdRef.current)) return;
      startTransition(() => {
        setBtDecision(message as BTDecision);
      });
    });
    skillStatusTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (!isBoundedRosPayload(message)) return;
      const status = message as SkillStatus;
      if (!belongsToActiveProcedureRun(status, activeProcedureRunIdRef.current)) return;
      const commandId = typeof status.command_id === "string"
        ? status.command_id.trim()
        : "";
      startTransition(() => {
        setSkillStatus(status);
        if (!commandId) return;
        setSkillStatusByCommand((current) => {
          // Refreshing an existing command moves it to the end so pruning is
          // based on last status update rather than first observation.
          const next = { ...current };
          delete next[commandId];
          next[commandId] = status;
          const commandIds = Object.keys(next);
          for (const staleCommandId of commandIds.slice(0, -MAX_SKILL_STATUS_HISTORY)) {
            delete next[staleCommandId];
          }
          return next;
        });
      });
    });
    executionTraceTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      const trace = normalizeExecutionTrace(message);
      if (!trace) return;
      if (!belongsToActiveProcedureRun(trace, activeProcedureRunIdRef.current)) return;
      startTransition(() => {
        setExecutionTraceFeed((current) => appendOperationTrace(current, trace));
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
      if (!belongsToActiveProcedureRun(message as object, activeProcedureRunIdRef.current)) return;
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
    vlmReducerTopic?.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      if (!isBoundedRosPayload(message)) return;
      startTransition(() => {
        setVlmReducerDecisions((current) => [message as VLMReducerDecision, ...current].slice(0, 8));
      });
    });
    shadowReplayStateTopic?.subscribe((message: unknown) => {
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
        startHeavySubscriptions();
      }
      startTransition(() => {
        if (runChanged || replayRewound) {
          setShadowTranscript([]);
          setEvents([]);
          setExecutionTraceFeed((current) => resetOperationTraceFeed(current));
          setSkillStatusByCommand({});
          setVlmReducerDecisions([]);
          setVlmResult(DEFAULT_VLM_RESULT);
          setVlmRequestToolDetectionEvidence(null);
          typedRfdetrObservationStore.clear();
          setVlmResultReceivedAt(0);
          setVlmCompositeImage(null);
          setCam1Image(null);
          setCam2Image(null);
          setCam3Image(null);
          setCam4Image(null);
          setFlirImage(null);
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
    cam4SemanticsTopic?.subscribe((message: unknown) => {
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
    shadowGroundTruthTopic?.subscribe((message: unknown) => {
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
      ttsPlaybackStatusTopic.subscribe((message: unknown) => {
        if (!isCurrentGeneration()) return;
        const parsed = normalizeTtsPlaybackStatus(message);
        if (!parsed) return;
        setTtsPlaybackStatus((current) => (
          current && parsed.stampSec < current.stampSec ? current : parsed
        ));
      });
      import("../ros/toolBeliefMessages").then((beliefMessages) => {
        if (!isCurrentGeneration()) return;
        toolBeliefTopic = beliefMessages.default(
          ROSLIB.Topic,
          ros,
          isCurrentGeneration,
          setToolBeliefs,
        );
      });
      liveAsrStatusTopic.subscribe((message: unknown) => {
        if (!isCurrentGeneration()) return;
        const normalized = normalizeLiveAsrStatus(message);
        if (!normalized) return;
        const parsed = mergeExternalAsrTranscripts(
          normalized,
          null,
          observedAsrFinals,
        );
        const receivedAt = Date.now();
        const hadPrimaryStatus = liveAsrStatusReceivedAtRef.current > 0;
        const previousFinals = liveAsrStatusRef.current.finals;
        const finalsChanged = previousFinals.length !== parsed.finals.length
          || previousFinals.some((previous, index) => {
            const next = parsed.finals[index];
            return !next
              || previous.stamp !== next.stamp
              || previous.text !== next.text
              || previous.response_latency_ms !== next.response_latency_ms
              || previous.latency_basis !== next.latency_basis
              || previous.latency_correlated !== next.latency_correlated;
          });
        liveAsrStatusRef.current = parsed;
        liveAsrStatusReceivedAtRef.current = receivedAt;
        // The LiveAsrPanel reads its 10 Hz meter through an isolated socket.
        // Keep this broad mission state quiet unless a new final needs to
        // reach the operation presentation feed.
        if (!hadPrimaryStatus || finalsChanged) {
          setLiveAsrStatus(parsed);
          setLiveAsrStatusReceivedAt(receivedAt);
          setLiveAsrStatusBridgeUrl(url);
        }
      });
      liveAsrObservedFinalTopic.subscribe((message: unknown) => {
        if (!isCurrentGeneration()) return;
        const final = normalizeExternalAsrFinal(message);
        if (!final) return;
        observedAsrFinals = [...observedAsrFinals, final].slice(-48);
        const parsed = mergeExternalAsrTranscripts(
          liveAsrStatusRef.current,
          null,
          observedAsrFinals,
        );
        liveAsrStatusRef.current = parsed;
        const receivedAt = Date.now();
        liveAsrStatusReceivedAtRef.current = receivedAt;
        setLiveAsrStatus(parsed);
        setLiveAsrStatusReceivedAt(receivedAt);
        setLiveAsrStatusBridgeUrl(url);
      });
      surgeryRecordReceiptTopic.subscribe((message: unknown) => {
        if (!isCurrentGeneration()) return;
        const parsed = normalizeSurgeryRecordReceipt(message);
        if (!parsed || sameSurgeryRecordReceipt(surgeryRecordReceiptRef.current, parsed)) {
          return;
        }
        surgeryRecordReceiptRef.current = parsed;
        startTransition(() => {
          setSurgeryRecordReceipt(parsed);
        });
      });
      rosbagRecordingStatusTopic.subscribe((message: unknown) => {
        if (!isCurrentGeneration()) return;
        const parsed = normalizeRosbagRecordingStatus(message);
        if (!parsed || sameRosbagRecordingStatus(rosbagRecordingRef.current, parsed)) {
          return;
        }
        rosbagRecordingRef.current = parsed;
        startTransition(() => {
          setRosbagRecording(parsed);
        });
      });
      integrationReadinessTopic.subscribe((message: unknown) => {
        if (!isCurrentGeneration()) return;
        const parsed = normalizeIntegrationReadiness(message);
        if (!parsed) {
          // A malformed or contradictory status is an active safety failure,
          // not a reason to continue trusting the preceding heartbeat.
          integrationReadinessRef.current = DEFAULT_INTEGRATION_READINESS;
          integrationReadinessReceivedAtRef.current = 0;
          setIntegrationReadiness(DEFAULT_INTEGRATION_READINESS);
          setIntegrationReadinessReceivedAt(null);
          setIntegrationReadinessBridgeUrl("");
          return;
        }
        const receivedAt = Date.now();
        integrationReadinessRef.current = parsed;
        integrationReadinessReceivedAtRef.current = receivedAt;
        setIntegrationReadiness(parsed);
        setIntegrationReadinessReceivedAt(receivedAt);
        setIntegrationReadinessBridgeUrl(url);
      });
      executionRouteStateTopic.subscribe((message: unknown) => {
        if (!isCurrentGeneration()) return;
        const parsed = normalizeExecutionRouteState(message);
        if (!parsed) {
          executionRouteStateRef.current = null;
          executionRouteStateReceivedAtRef.current = 0;
          setExecutionRouteState(null);
          setExecutionRouteStateReceivedAt(null);
          return;
        }
        const previous = executionRouteStateRef.current;
        // A transient-local retained status can race a newer service response
        // during bridge reconnect. Never regress to an older route revision.
        if (
          previous &&
          (parsed.revision < previous.revision ||
            (parsed.revision === previous.revision &&
              parsed.initializationRevision < previous.initializationRevision))
        ) {
          return;
        }
        const receivedAt = Date.now();
        executionRouteStateRef.current = parsed;
        executionRouteStateReceivedAtRef.current = receivedAt;
        setExecutionRouteState(parsed);
        setExecutionRouteStateReceivedAt(receivedAt);
      });
    }
    shadowTranscriptTopic?.subscribe((message: unknown) => {
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
    shadowTranscriptHistoryTopic?.subscribe((message: unknown) => {
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
    if (runtimeMode === "live" && rosbagRecordingAuditTopic) {
      // Advertise before the manual recorder can start. That makes the small
      // audit lane discoverable during rosbag2's first graph scan, so the
      // snapshot emitted immediately after a successful manual start is not
      // lost to discovery timing.
      try {
        rosbagRecordingAuditTopic.advertise();
      } catch {
        // Publishing remains best-effort observer telemetry and must never
        // change the operator's normal control path.
      }
      rosbagRecordingAuditTopicRef.current = rosbagRecordingAuditTopic;
    }
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
        priorityStopInFlightRef.current = false;
        liveAsrControlRunIdRef.current += 1;
        liveAsrControlInFlightRef.current = false;
        rosbagRecordingControlRunIdRef.current += 1;
        rosbagRecordingControlInFlightRef.current = false;
        rosbagRecordingRef.current = null;
        rosbagRecordingAuditTopicRef.current = null;
        liveAsrStatusRef.current = DEFAULT_LIVE_ASR_STATUS;
        liveAsrStatusReceivedAtRef.current = 0;
        surgeryRecordReceiptRef.current = null;
        executionRouteRunIdRef.current += 1;
        activeProcedureRunIdRef.current = "";
        simulationStateReceivedAtRef.current = 0;
        shadowReplayStateReceivedAtRef.current = 0;
        simulationStateRef.current = DEFAULT_STATE;
        shadowReplayStateRef.current = DEFAULT_SHADOW_REPLAY_STATE;
        integrationReadinessRef.current = DEFAULT_INTEGRATION_READINESS;
        integrationReadinessReceivedAtRef.current = 0;
        executionRouteStateRef.current = null;
        executionRouteStateReceivedAtRef.current = 0;
        rosRef.current = null;
        setTransportConnected(false);
        setConnected(false);
        setRuntimeAuthorityStatus("offline");
        setSimulationState(DEFAULT_STATE);
        setExecutionTraceFeed((current) => resetOperationTraceFeed(current));
        setSkillStatusByCommand({});
        typedRfdetrObservationStore.clear();
        setToolBeliefs(null);
        setShadowReplayState(DEFAULT_SHADOW_REPLAY_STATE);
        setIntegrationReadiness(DEFAULT_INTEGRATION_READINESS);
        setIntegrationReadinessReceivedAt(null);
        setIntegrationReadinessBridgeUrl("");
        setExecutionRouteState(null);
        setExecutionRouteStateReceivedAt(null);
        setExecutionRouteTransition({ state: "idle", message: "" });
        setSurgeryRecordReceipt(null);
        setRosbagRecording(null);
        setRosbagRecordingControlPending("");
        setRosbagRecordingControlMessage("");
        setTtsPlaybackStatus(null);
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
        toolPolicyTopic,
        bedRobotArmStatusTopic,
        eventTopic,
        surgeonTopic,
        surgeonLlmDecisionTopic,
        btDecisionTopic,
        skillStatusTopic,
        executionTraceTopic,
        vlmHealthTopic,
        vlmResultTopic,
        vlmRequestContextTopic,
        ...inputSourceStatusTopics,
        vlmReducerTopic,
        ...cameraTopics,
        ...typedRfdetrToolObservationTopics,
        shadowReplayStateTopic,
        cam4SemanticsTopic,
        shadowTranscriptTopic,
        shadowTranscriptHistoryTopic,
        shadowGroundTruthTopic,
        ...(runtimeMode === "live"
          ? [
              toolBeliefTopic,
              liveAsrStatusTopic,
              liveAsrObservedFinalTopic,
              ttsPlaybackStatusTopic,
              surgeryRecordReceiptTopic,
              rosbagRecordingStatusTopic,
              integrationReadinessTopic,
              executionRouteStateTopic,
            ]
          : []),
      ].filter(Boolean));
      if (rosbagRecordingAuditTopic) {
        try {
          rosbagRecordingAuditTopic.unadvertise();
        } catch {
          // The bridge may already have closed its publisher socket.
        }
        if (rosbagRecordingAuditTopicRef.current === rosbagRecordingAuditTopic) {
          rosbagRecordingAuditTopicRef.current = null;
        }
      }
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
  }, [connectEnabled, observationProfile, replayPresentationOnly, runtimeMode, url]);

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
      // VLM model-visual frames are also operator-facing evidence. Do not
      // leave the last model input looking current after that publisher stops.
      setVlmCompositeImage(clearIfStale);
      typedRfdetrObservationStore.expireOlderThan(CAMERA_STALE_AFTER_MS);
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

  function publishRosbagUiAudit(
    event: RosbagUiAuditEventName,
    {
      value = "",
      bundle: selectedBundle = bundle,
      startPhase: selectedStartPhase = startPhase,
      presentation = rosbagUiPresentationRef.current,
      allowWhenInactive = false,
    }: RosbagUiAuditPublishOptions = {},
  ) {
    // Query-flag replay is a passive consumer of this same topic. It must
    // never write an audit echo into the bag/bridge it is displaying.
    if (replayPresentationOnly) return;
    if (runtimeMode !== "live") return;
    if (!allowWhenInactive && !rosbagRecordingRef.current?.recordingActive) return;
    const topic = rosbagRecordingAuditTopicRef.current;
    if (!topic) return;
    const audit = createRosbagUiAuditMessage({
      event,
      value,
      bundle: selectedBundle,
      startPhase: selectedStartPhase,
      transportConnected: Boolean(transportConnected),
      presentation,
    });
    const payload = audit ? serializeRosbagUiAuditMessage(audit) : null;
    if (!payload) return;
    try {
      topic.publish(new ROSLIB.Message({ data: payload }));
    } catch {
      // The audit lane is observational. A closing rosbridge must not affect
      // the operator's selected bundle, phase, or planner control request.
    }
  }

  function setBundleSelection(nextBundle: string) {
    if (replayPresentationOnly) return;
    bundleApplyRunIdRef.current += 1;
    bundlePreviewInFlightRef.current = false;
    bundleDirtyRef.current = true;
    setBundle(nextBundle);
    publishRosbagUiAudit("bundle_selected", {
      value: nextBundle,
      bundle: nextBundle,
    });
    updateScenarioRevision({
      ...EMPTY_SCENARIO_REVISION_STATE,
      bundleName: nextBundle,
    });
  }

  function setStartPhaseSelection(nextPhase: string) {
    if (replayPresentationOnly) return;
    setStartPhaseState(nextPhase);
    publishRosbagUiAudit("start_phase_selected", {
      value: nextPhase,
      startPhase: nextPhase,
    });
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
    options: { requireFreshRuntimeState?: boolean } = {},
  ) {
    if (replayPresentationOnly) {
      throw new Error("ROS bag presentation replay is read-only.");
    }
    const requireFreshRuntimeState = options.requireFreshRuntimeState !== false;
    const generation = bridgeGenerationRef.current;
    const ros = rosRef.current as RosServiceConnection;
    if (
      !ros ||
      !ros.isConnected ||
      (requireFreshRuntimeState && (
        commandReadyGenerationRef.current !== generation || !runtimeStateIsFresh()
      ))
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
        runtimeBoundServiceCancelsRef.current.delete(cancel);
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
          rosRef.current !== ros ||
          (requireFreshRuntimeState && (
            commandReadyGenerationRef.current !== generation || !runtimeStateIsFresh()
          ))
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
      if (requireFreshRuntimeState) runtimeBoundServiceCancelsRef.current.add(cancel);
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

  async function controlRosbagRecording(enabled: boolean) {
    if (replayPresentationOnly) {
      return { accepted: false, message: "ROS bag presentation replay is read-only." };
    }
    if (runtimeMode !== "live") {
      return { accepted: false, message: "ROS bag recording is available only in live integration mode." };
    }
    if (rosbagRecordingControlInFlightRef.current) {
      return { accepted: false, message: "Another ROS bag recording request is still pending." };
    }
    const generation = bridgeGenerationRef.current;
    if (url !== runtimeBridgeUrl(runtimeMode)) {
      return { accepted: false, message: "ROS bridge is not ready for live recording control." };
    }

    rosbagRecordingControlInFlightRef.current = true;
    const runId = rosbagRecordingControlRunIdRef.current + 1;
    rosbagRecordingControlRunIdRef.current = runId;
    const isCurrentRun = () =>
      bridgeGenerationRef.current === generation
      && rosbagRecordingControlRunIdRef.current === runId
      && runtimeMode === "live"
      && url === runtimeBridgeUrl(runtimeMode);
    setRosbagRecordingControlPending(enabled ? "start" : "stop");
    setRosbagRecordingControlMessage("");

    try {
      const response = await callService(
        ROSBAG_RECORDING_CONTROL_SERVICE,
        ROSBAG_RECORDING_CONTROL_SERVICE_TYPE,
        { data: enabled },
        30_000,
        { requireFreshRuntimeState: false },
      );
      const accepted = response.success === true;
      const fallback = enabled
        ? "ROS bag recording started."
        : "ROS bag recording stopped and saved.";
      const message = typeof response.message === "string" && response.message.trim()
        ? response.message.trim().slice(0, 1_024)
        : fallback;
      if (!accepted) throw new Error(message);
      if (isCurrentRun()) {
        if (enabled) {
          // The explicit snapshot is independent of scenario lifecycle. It
          // captures selections made before the manual recorder was enabled.
          publishRosbagUiAudit("recording_ui_snapshot", { allowWhenInactive: true });
          // rosbag2's graph discovery can still be settling when the control
          // service acknowledges the request. Emit one bounded follow-up only
          // after the retained status confirms that this same recorder is
          // active, so the initial browser-local presentation is not missed.
          window.setTimeout(() => {
            if (
              replayPresentationOnly ||
              !isCurrentRun() ||
              !rosbagRecordingRef.current?.recordingActive
            ) {
              return;
            }
            publishRosbagUiAudit("recording_ui_snapshot");
          }, 1_000);
        }
        setRosbagRecordingControlMessage(message);
      }
      return { accepted: true, message };
    } catch (error) {
      const message = (error instanceof Error ? error.message : String(error)).slice(0, 1_024);
      if (isCurrentRun()) setRosbagRecordingControlMessage(message);
      return { accepted: false, message };
    } finally {
      if (isCurrentRun()) {
        setRosbagRecordingControlPending("");
        rosbagRecordingControlInFlightRef.current = false;
      }
    }
  }

  useEffect(() => {
    let disposed = false;
    let refreshing = false;
    if (replayPresentationOnly) {
      setVlmModelOptions([]);
      setVlmProviderStatuses([]);
      setVlmModelSelection(null);
      setVlmModelCatalogStatus("ROS bag presentation replay");
      return () => {
        disposed = true;
      };
    }

    if (!subscriptionPlan.modelStatusObservation) {
      setVlmModelOptions([]);
      setVlmProviderStatuses([]);
      setVlmModelSelection(null);
      setVlmModelCatalogStatus("VLM unknown");
      return () => {
        disposed = true;
      };
    }

    async function refreshVlmModels() {
      if (!transportConnected || refreshing) return;
      refreshing = true;
      try {
        const projection: ModelCatalogProjection = parseModelCatalogResponse(
          await callService(
            "/real_vlm_node/list_model_catalog",
            "surgical_msgs/srv/ListModelCatalog",
            {},
            10000,
            { requireFreshRuntimeState: false },
          ),
        );
        if (disposed) return;
        setVlmModelOptions(projection.models);
        setVlmProviderStatuses(projection.providers);
        setVlmModelSelection(projection.selection);
        setVlmModelCatalogStatus(projection.status);
      } catch (error) {
        if (disposed) return;
        setVlmModelCatalogStatus(error instanceof Error ? error.message : "VLM unavailable");
      } finally {
        refreshing = false;
      }
    }

    if (!transportConnected) {
      setVlmModelOptions([]);
      setVlmProviderStatuses([]);
      setVlmModelSelection(null);
      setVlmModelCatalogStatus(
        connectionBlocked
          ? "ROS mismatch"
          : connectionPending
            ? "ROS transport pending"
            : "ROS offline",
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
    const timer = window.setInterval(refreshWhenVisible, VLM_MODEL_REFRESH_MS);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [
    connectionBlocked,
    connectionPending,
    subscriptionPlan.modelControls,
    subscriptionPlan.modelStatusObservation,
    transportConnected,
    replayPresentationOnly,
    url,
  ]);

  useEffect(() => {
    let disposed = false;
    let refreshing = false;
    actorPolicyRevisionRef.current += 1;
    setActorEnabledKnown(false);
    if (
      replayPresentationOnly ||
      !subscriptionPlan.modelControls ||
      !transportConnected ||
      runtimeMode === "shadow"
    ) {
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
          { requireFreshRuntimeState: false },
        );
        if (disposed || revision !== actorPolicyRevisionRef.current) return;
        const values = Array.isArray(response.values) ? response.values : [];
        const value = values[0];
        if (
          !value ||
          typeof value !== "object" ||
          Number((value as { type?: unknown }).type) !== ROS_PARAMETER_BOOL ||
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
  }, [replayPresentationOnly, runtimeMode, subscriptionPlan.modelControls, transportConnected, url]);

  useEffect(() => {
    let disposed = false;
    let refreshing = false;
    if (replayPresentationOnly) {
      setActorModelOptions([]);
      setActorProviderStatuses([]);
      setActorModelSelection(null);
      setActorModelCatalogStatus("ROS bag presentation replay");
      return () => {
        disposed = true;
      };
    }

    async function refreshActorModels() {
      if (!transportConnected || refreshing) return;
      refreshing = true;
      try {
        const projection: ModelCatalogProjection = parseModelCatalogResponse(
          await callService(
            "/surgeon_actor/list_model_catalog",
            "surgical_msgs/srv/ListModelCatalog",
            {},
            10000,
            { requireFreshRuntimeState: false },
          ),
        );
        if (disposed) return;
        setActorModelOptions(projection.models);
        setActorProviderStatuses(projection.providers);
        setActorModelSelection(projection.selection);
        setActorModelCatalogStatus(projection.status);
      } catch (error) {
        if (disposed) return;
        setActorModelCatalogStatus(error instanceof Error ? error.message : "Actor model catalog unavailable.");
      } finally {
        refreshing = false;
      }
    }

    if (
      !subscriptionPlan.modelControls ||
      !transportConnected ||
      runtimeMode !== "llm"
    ) {
      setActorModelOptions([]);
      setActorProviderStatuses([]);
      setActorModelSelection(null);
      setActorModelCatalogStatus(
        !subscriptionPlan.modelControls
          ? "unavailable in Live"
          : runtimeMode !== "llm"
          ? "disabled in this runtime mode"
          : connectionBlocked
            ? "ROS access blocked by runtime-contract mismatch"
            : connectionPending
              ? "ROS transport pending"
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
  }, [
    connectionBlocked,
    connectionPending,
    runtimeMode,
    subscriptionPlan.modelControls,
    transportConnected,
    replayPresentationOnly,
    url,
  ]);

  async function setNodeParameters(
    nodeName: string,
    parameters: RosParameter[],
  ) {
    const response = await callService(
      `/${nodeName}/set_parameters`,
      "rcl_interfaces/srv/SetParameters",
      setParametersRequest(parameters),
      10000,
      { requireFreshRuntimeState: false },
    );
    assertSetParametersAccepted(response, parameters.length);
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

  function showOptimisticStartState(runId: number) {
    const previous = simulationStateRef.current;
    const next: SimulationState = {
      ...previous,
      running: false,
      execution_state: "starting",
      // Preserve a more specific non-idle robot status when it exists.  The
      // lifecycle label itself is enough to give the click immediate feedback.
      robot_state: previous.robot_state === "idle" ? "starting" : previous.robot_state,
      recent_events: [],
    };
    optimisticStartControlRef.current = {
      runId,
      previous,
      expiresAt: Date.now() + OPTIMISTIC_START_STATE_MAX_MS,
    };
    simulationStateRef.current = next;
    setSimulationState(next);
  }

  function clearOptimisticStartState(
    runId: number,
    { restore = false }: { restore?: boolean } = {},
  ) {
    const optimistic = optimisticStartControlRef.current;
    if (!optimistic || optimistic.runId !== runId) return;
    optimisticStartControlRef.current = null;
    if (!restore || controlRunIdRef.current !== runId) return;
    // Never overwrite an authoritative non-idle state that arrived while the
    // Service call was in flight.
    if (simulationStateRef.current.execution_state !== "starting") return;
    simulationStateRef.current = optimistic.previous;
    setSimulationState(optimistic.previous);
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
      setVlmRequestToolDetectionEvidence(null);
      typedRfdetrObservationStore.clear();
      setSkillStatusByCommand({});
      setVlmResultReceivedAt(0);
      setVlmCompositeImage(null);
      setCam1Image(null);
      setCam2Image(null);
      setCam3Image(null);
      setCam4Image(null);
      setFlirImage(null);
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

  async function previewBundle(targetBundle = bundle) {
    const selectedBundle = (targetBundle || bundle).trim();
    if (!selectedBundle || bundlePreviewInFlightRef.current) return;
    const runId = bundleApplyRunIdRef.current + 1;
    bundleApplyRunIdRef.current = runId;
    bundlePreviewInFlightRef.current = true;
    updateScenarioRevision({
      phase: "previewing",
      bundleName: selectedBundle,
      message: "Checking the selected bundle revision...",
      result: null,
    });
    try {
      // Preview is a read-only server operation. It is deliberately not routed
      // through runAction and does not inherit running/paused write gates.
      const response = await callService(
        SELECT_BUNDLE_SERVICE,
        SELECT_BUNDLE_SERVICE_TYPE,
        scenarioRevisionPreviewRequest(selectedBundle),
        12000,
        { requireFreshRuntimeState: false },
      );
      const result = parseScenarioRevisionResult(response);
      if (bundleApplyRunIdRef.current !== runId || bundle !== selectedBundle) return;
      updateScenarioRevision({
        phase: result.success ? "previewed" : "failed",
        bundleName: selectedBundle,
        message: result.message,
        result,
      });
    } catch (error) {
      if (bundleApplyRunIdRef.current !== runId) return;
      updateScenarioRevision({
        phase: "failed",
        bundleName: selectedBundle,
        message: error instanceof Error ? error.message : String(error),
        result: null,
      });
    } finally {
      if (bundleApplyRunIdRef.current === runId) {
        bundlePreviewInFlightRef.current = false;
      }
    }
  }

  async function applyBundle(targetBundle = bundle) {
    const selectedBundle = (targetBundle || bundle).trim();
    if (!selectedBundle) return;
    publishRosbagUiAudit("bundle_apply_requested", {
      value: selectedBundle,
      bundle: selectedBundle,
    });
    const currentRevision = scenarioRevisionRef.current;
    const initialAdmission = computeScenarioRevisionApplyAdmission({
      state: simulationStateRef.current,
      selectedBundle,
      revision: currentRevision,
      stateFresh: runtimeStateIsFresh(),
      commandPending: actionInFlightRef.current,
    });
    if (!initialAdmission.allowed) {
      updateScenarioRevision({
        ...currentRevision,
        phase: "failed",
        bundleName: selectedBundle,
        message: initialAdmission.reason,
      });
      setActionMessage(initialAdmission.reason);
      return;
    }

    const applyRunId = bundleApplyRunIdRef.current + 1;
    bundleApplyRunIdRef.current = applyRunId;
    updateScenarioRevision({
      ...currentRevision,
      phase: "applying",
      bundleName: selectedBundle,
      message: "Applying the server-verified bundle revision...",
    });
    await runAction("Applying bundle revision", async () => {
      // State may change between preview and click. Re-evaluate immediately
      // before transport; the manager performs the final authoritative check.
      const admission = computeScenarioRevisionApplyAdmission({
        state: simulationStateRef.current,
        selectedBundle,
        revision: currentRevision,
        stateFresh: runtimeStateIsFresh(),
        commandPending: false,
      });
      if (!admission.allowed) throw new Error(admission.reason);
      const expectedCandidateRevision =
        currentRevision.result?.candidateRevision.trim();
      if (!expectedCandidateRevision) {
        throw new Error("Preview this bundle revision before applying it.");
      }
      const response = await callService(
        SELECT_BUNDLE_SERVICE,
        SELECT_BUNDLE_SERVICE_TYPE,
        scenarioRevisionApplyRequest(
          selectedBundle,
          admission,
          expectedCandidateRevision,
        ),
        12000,
      );
      const result = parseScenarioRevisionResult(response);
      if (bundleApplyRunIdRef.current !== applyRunId) return;
      const appliedRevisionMatches =
        result.activeBundle === selectedBundle &&
        result.activeRevision === result.candidateRevision &&
        result.activeRevision === expectedCandidateRevision;
      const applyAccepted =
        result.success &&
        appliedRevisionMatches &&
        (
          (
            result.applied &&
            result.changed &&
            result.disposition === "applied"
          ) ||
          (!result.applied && !result.changed && result.disposition === "unchanged")
        );
      if (!applyAccepted) {
        updateScenarioRevision({
          phase: "failed",
          bundleName: selectedBundle,
          message: result.message || "The server did not apply the selected revision.",
          result,
        });
        throw new Error(result.message || "The server did not apply the selected revision.");
      }

      const appliedBundle = result.activeBundle;
      bundleDirtyRef.current = false;
      setBundle(appliedBundle);
      setStartPhaseState("");
      clearEventLog({ suppressMs: 500 });
      // Do not synthesize a hybrid state from the Service result. The next
      // complete /simulation/state frame owns bundle, layout and instruments.
      updateScenarioRevision({
        phase: "applied",
        bundleName: appliedBundle,
        message: result.message,
        result,
      });
      setActionMessage(result.message || `Bundle revision applied to ${appliedBundle}.`);
    });
    if (
      bundleApplyRunIdRef.current === applyRunId &&
      scenarioRevisionRef.current.phase === "applying"
    ) {
      updateScenarioRevision({
        ...scenarioRevisionRef.current,
        phase: "failed",
        message: "The bundle revision request did not complete.",
      });
    }
  }

  async function control(command: ControlCommand) {
    if (replayPresentationOnly) {
      setActionMessage("ROS bag presentation replay is read-only.");
      return;
    }
    publishRosbagUiAudit("planner_control_requested", { value: command });
    // Stop is intentionally a separate, high-priority planner lifecycle
    // request.  A Start Service can remain outstanding for 45 seconds, and a
    // blanket single-flight guard would otherwise swallow the operator's Stop
    // click precisely when the manager can still interrupt startup.  This is
    // not a physical E-stop: the UI explicitly directs externally accepted
    // robot motion to the device's independent safety procedure.
    if (command === "stop") {
      if (priorityStopInFlightRef.current) return;
      priorityStopInFlightRef.current = true;
      const stopRunId = controlRunIdRef.current + 1;
      controlRunIdRef.current = stopRunId;
      // A priority stop supersedes any locally projected Start immediately.
      optimisticStartControlRef.current = null;
      // Let an earlier request settle on the transport, but make its late
      // result unable to mutate UI state or restart the displayed lifecycle.
      actionRunIdRef.current += 1;
      actionInFlightRef.current = false;
      controlInFlightRef.current = false;
      const label = "Stopping planner execution";
      setActionPending(label);
      setActionMessage(`${label}...`);
      try {
        await prepareShadowControl("stop");
        if (controlRunIdRef.current !== stopRunId) return;
        const response = await callService(
          "/simulation/control",
          "surgical_msgs/srv/ControlSimulation",
          { command: "stop", start_phase_id: "" },
          20000,
        );
        if (controlRunIdRef.current !== stopRunId) return;
        const success = response.success === undefined ? true : Boolean(response.success);
        if (!success) {
          throw new Error(String(response.message || "Stopping planner execution failed."));
        }
        const rawMessage = String(response.message || "planner execution stopped");
        const stopMessage = runtimeMode === "live"
          ? `${rawMessage === "ok" ? "planner stop accepted" : rawMessage}. Verify external robot safety state separately.`
          : rawMessage === "ok" ? "simulation stopped" : rawMessage;
        setActionMessage(stopMessage);
        if (rawMessage.endsWith("requested")) {
          const stableMessage = await waitForControlTarget("stop", 20000, stopRunId);
          if (controlRunIdRef.current !== stopRunId) return;
          await finalizeShadowControl("stop", stopRunId);
          if (stableMessage) {
            setActionMessage(
              runtimeMode === "live"
                ? `${stableMessage}. Verify external robot safety state separately.`
                : stableMessage,
            );
          }
        }
      } catch (error) {
        if (controlRunIdRef.current === stopRunId) {
          setActionMessage(error instanceof Error ? error.message : String(error));
        }
      } finally {
        priorityStopInFlightRef.current = false;
        if (controlRunIdRef.current === stopRunId) {
          setActionPending("");
          controlInFlightRef.current = false;
        }
      }
      return;
    }

    // The buttons are disabled while a command is pending, but keep a ref-level
    // single-flight guard as well so rapid clicks or DOM-driven retries cannot
    // submit two conflicting runtime commands before React re-renders.
    if (priorityStopInFlightRef.current || controlInFlightRef.current) return;
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
            : "Resetting simulation";
    try {
      await runAction(label, async () => {
        if (command === "start") {
          suppressEventsUntilRef.current = 0;
          clearEventLog();
          // The state core acknowledges Start asynchronously.  Project the
          // accepted lifecycle locally before waiting for rosbridge's next
          // state heartbeat so the button has instant, stable feedback.
          showOptimisticStartState(controlRunId);
        }
        if (command === "reset") {
          clearEventLog({ suppressMs: 1200 });
          optimisticStartControlRef.current = null;
        }
        await prepareShadowControl(command);
        if (controlRunIdRef.current !== controlRunId) return;
        try {
          const response = await callService(
            "/simulation/control",
            "surgical_msgs/srv/ControlSimulation",
            { command, start_phase_id: command === "start" ? startPhase : "" },
            command === "start" ? 45000 : command === "reset" ? 30000 : 20000,
          );
        if (controlRunIdRef.current !== controlRunId) return;
        const success = response.success === undefined ? true : Boolean(response.success);
        if (!success) {
          throw new Error(String(response.message || `${label} failed.`));
        }
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
          if (command === "start") {
            clearOptimisticStartState(controlRunId, { restore: true });
          }
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

  function executionRouteSwitchBlocker(): string | null {
    return executionRouteSwitchBlockReason({
      runtimeMode,
      connected,
      runtimeStateFresh: runtimeStateIsFresh(),
      routeState: executionRouteStateRef.current,
      actionInFlight: actionInFlightRef.current,
      controlInFlight: controlInFlightRef.current,
      simulationState: simulationStateRef.current,
    });
  }

  function commitExecutionRouteState(
    next: ExecutionRouteState,
    receivedAt = Date.now(),
  ) {
    const previous = executionRouteStateRef.current;
    if (
      previous &&
      (next.revision < previous.revision ||
        (next.revision === previous.revision &&
          next.initializationRevision < previous.initializationRevision))
    ) {
      return false;
    }
    executionRouteStateRef.current = next;
    executionRouteStateReceivedAtRef.current = receivedAt;
    setExecutionRouteState(next);
    setExecutionRouteStateReceivedAt(receivedAt);
    return true;
  }

  /**
   * Change the independently reviewed Action and Service endpoint sources. The
   * manager/bridge rechecks stopped state and performs the authoritative
   * initialization; the browser only clears observer-derived presentation
   * after that server acknowledgement.
   */
  async function configureExecutionRoute(sources: {
    toolHandoverSource: ExecutionEndpointSource;
    retractionSource: ExecutionEndpointSource;
  }) {
    const requestedSource = normalizeExecutionEndpointSource(sources.toolHandoverSource);
    const requestedRetractionSource = normalizeExecutionEndpointSource(sources.retractionSource);
    if (!requestedSource || !requestedRetractionSource) return;
    const blocker = executionRouteSwitchBlocker();
    if (blocker) {
      setExecutionRouteTransition({ state: "failed", message: blocker });
      setActionMessage(blocker);
      return;
    }
    if (actionInFlightRef.current) return;
    const routeRunId = executionRouteRunIdRef.current + 1;
    executionRouteRunIdRef.current = routeRunId;
    const currentRevision = executionRouteStateRef.current?.revision ?? -1;
    const isCurrentRouteRun = () =>
      executionRouteRunIdRef.current === routeRunId &&
      runtimeMode === "live" &&
      bridgeGenerationRef.current === commandReadyGenerationRef.current;
    setExecutionRouteTransition({
      state: "switching",
      message: "Applying the selected Action and Service routes.",
    });
    await runAction("Switching Action/Service server", async () => {
      try {
        const response = await callService(
          "/integration/execution_route/command",
          "surgical_msgs/srv/IntegrationDebugCommand",
          {
            operation: "configure_execution_endpoints",
            payload_json: JSON.stringify({
              tool_handover_source: requestedSource,
              retraction_source: requestedRetractionSource,
            }),
          },
          30000,
        );
        if (!isCurrentRouteRun()) return;
        const resultState = normalizeExecutionRouteCommandResult(
          response.result_json,
        );
        const accepted = Boolean(response.accepted);
        const serverMessage = String(
          response.message ||
            (accepted
              ? "Action/Service route initialized."
              : "Action/Service route change was rejected."),
        ).trim().slice(0, 360);
        if (resultState) commitExecutionRouteState(resultState);
        if (
          !accepted ||
          !resultState ||
          resultState.selectedSource !== requestedSource ||
          resultState.retractionSource !== requestedRetractionSource ||
          resultState.revision <= currentRevision ||
          !resultState.routeControlEnabled
        ) {
          const message = !accepted
            ? serverMessage
            : !resultState
              ? "The Action/Service route response did not contain a valid server state."
              : "The Action/Service route did not confirm the requested initialized state.";
          setExecutionRouteTransition({ state: "failed", message });
          throw new Error(message);
        }

        setExecutionRouteTransition({
          state: "ready",
          message: "Action/Service route changed. Endpoint availability remains diagnostic until a request is sent.",
        });
        setActionMessage(serverMessage || "Action/Service route initialized.");
      } catch (error) {
        if (!isCurrentRouteRun()) return;
        const message = error instanceof Error ? error.message : String(error);
        setExecutionRouteTransition({ state: "failed", message });
        throw error;
      }
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
            { requireFreshRuntimeState: false },
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
            { requireFreshRuntimeState: false },
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
        { requireFreshRuntimeState: false },
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
    if (!transportConnected || !actorEnabledKnown || actionInFlightRef.current) return;
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

  function commitLiveAsrStatus(
    status: LiveAsrStatus,
    isCurrentRun: () => boolean,
  ) {
    if (!isCurrentRun()) return;
    // A control response contains the local sidecar snapshot only. Preserve
    // observed finals already received from CommandRouter so a start/stop or
    // route-policy operation cannot blank the stage's latest speech.
    const mergedStatus = mergeExternalAsrTranscripts(
      status,
      null,
      liveAsrStatusRef.current.finals,
    );
    const receivedAt = Date.now();
    liveAsrStatusRef.current = mergedStatus;
    liveAsrStatusReceivedAtRef.current = receivedAt;
    setLiveAsrStatus(mergedStatus);
    setLiveAsrStatusReceivedAt(receivedAt);
    setLiveAsrStatusBridgeUrl(url);
  }

  async function requestLiveAsrControl(
    operation: "refresh_devices" | "set_route_policy" | "start" | "stop" | "start_recording",
    deviceId: number,
    routePolicy: string,
    isCurrentRun: () => boolean,
  ): Promise<LiveAsrControlResult> {
    const response = await callService(
      "/input/asr/control",
      "surgical_msgs/srv/AsrControl",
      {
        operation,
        device_id: deviceId,
        // The browser never selects an endpoint by URL. The reviewed policy
        // identifier is validated by the Live ASR node, which then chooses a
        // concrete cloud/LAN URL from deployment configuration.
        server_url: "",
        route_policy: routePolicy,
      },
      20000,
      { requireFreshRuntimeState: false },
    );
    const accepted = Boolean(response.accepted);
    const message = String(
      response.message || (accepted ? "ASR request accepted." : "ASR request rejected."),
    );
    const rawResult = String(response.result_json ?? "").trim();
    if (rawResult && isCurrentRun()) {
      const parsed = normalizeLiveAsrStatus({ data: rawResult });
      if (parsed) commitLiveAsrStatus(parsed, isCurrentRun);
    }
    return { accepted, message };
  }

  async function controlLiveAsr(
    operation: "refresh_devices" | "set_route_policy" | "set_input_mode" | "start" | "stop" | "restart_node",
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
      if (operation === "set_input_mode") {
        const inputMode = routePolicy.trim().toLowerCase();
        if (inputMode !== "utterance" && inputMode !== "tagged_sentence") {
          throw new Error("ASR 입력 모드는 utterance 또는 tagged_sentence여야 합니다.");
        }
        const currentState = liveAsrStatusRef.current.state;
        if (["STARTING", "LISTENING", "STOPPING"].includes(currentState)) {
          throw new Error("ASR 마이크 세션을 중지한 뒤 입력 모드를 변경하세요.");
        }
        await setNodeParameters("speech_input_adapter", [
          stringParameter("input_mode", inputMode),
        ]);
        const message = inputMode === "tagged_sentence"
          ? "외부 토픽 입력 모드로 전환했습니다."
          : "로컬 마이크 입력 모드로 전환했습니다.";
        if (isCurrentRun()) setLiveAsrControlMessage(message);
        return { accepted: true, message };
      }
      if (operation === "restart_node") {
        if (!transportConnected) {
          throw new Error("Live ROS bridge가 준비된 뒤 다시 시도하세요.");
        }
        const requestedAt = Date.now();
        const previousStatus = liveAsrStatusRef.current;
        const previousStatusReceivedAt = liveAsrStatusReceivedAtRef.current;
        const { hotRestartAsrNode } = await import("../ros/asrRestartControl");
        const { message } = await hotRestartAsrNode({
          requestedAt,
          previousStatus,
          previousStatusReceivedAt,
          isCurrent: isCurrentRun,
          getCurrentStatus: () => ({
            status: liveAsrStatusRef.current,
            receivedAt: liveAsrStatusReceivedAtRef.current,
          }),
          requestControl: (requestedOperation, requestedDeviceId, requestedRoutePolicy) =>
            requestLiveAsrControl(
              requestedOperation,
              requestedDeviceId,
              requestedRoutePolicy,
              isCurrentRun,
            ),
        });
        if (isCurrentRun()) setLiveAsrControlMessage(message);
        return { accepted: true, message };
      }

      const result = await requestLiveAsrControl(
        operation,
        deviceId,
        routePolicy,
        isCurrentRun,
      );
      const { message } = result;
      if (isCurrentRun()) setLiveAsrControlMessage(message);
      return result;
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      const message = operation === "restart_node"
        ? `ASR 노드 새로 시작 실패: ${reason} 코드를 수정한 뒤 이 버튼으로 다시 시도하세요.`
        : reason;
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
  const scenarioRevisionAdmission = computeScenarioRevisionApplyAdmission({
    state: simulationState,
    selectedBundle: bundle,
    revision: scenarioRevision,
    stateFresh: runtimeStateIsFresh(),
    commandPending: Boolean(actionPending),
  });

  return {
    url,
    setUrl,
    transportConnected,
    connected,
    runtimeAuthorityStatus: connectionPending
      ? "checking" as const
      : connectionBlocked
        ? "blocked" as const
      : connectEnabled
        ? runtimeAuthorityStatus
        : "offline" as const,
    bundle,
    setBundleSelection,
    startPhase,
    setStartPhase: setStartPhaseSelection,
    activeBundle,
    simulationState,
    worldState,
    toolPolicyStatus: activeToolPolicyStatus(toolPolicyStatus, simulationState),
    bedRobotArms: externalBedRobotArmStatus?.arms ?? [],
    surgeonState,
    surgeonLlmDecision,
    btDecision,
    skillStatus,
    skillStatusByCommand,
    executionTraces,
    vlmHealth,
    inputSourceStatuses,
    vlmResult,
    vlmRequestToolDetectionEvidence,
    typedRfdetrObservationStore,
    toolBeliefs,
    cam4ToolRequest,
    vlmReducerDecisions,
    vlmImage: vlmCompositeImage,
    vlmCompositeImage,
    cameraMediaStore: liveCameraMedia.store,
    cameraMediaConnected: liveCameraMedia.transportConnected,
    cam1Image,
    cam2Image,
    cam3Image,
    cam4Image,
    flirImage,
    cameraPreviewContracts,
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
    actorEnabled,
    actorEnabledKnown,
    shadowReplayState,
    shadowTranscript,
    shadowGroundTruth,
    liveAsrStatus,
    liveAsrStatusReceivedAt: liveAsrStatusBridgeUrl === url ? liveAsrStatusReceivedAt : null,
    liveAsrControlPending,
    liveAsrControlMessage,
    ttsPlaybackStatus,
    surgeryRecordReceipt,
    rosbagRecording,
    rosbagRecordingControlPending,
    rosbagRecordingControlMessage,
    integrationReadiness,
    integrationReadinessReceivedAt:
      integrationReadinessBridgeUrl === url
        ? integrationReadinessReceivedAt
        : null,
    executionRouteState,
    executionRouteStateReceivedAt,
    executionRouteTransition,
    scenarioRevision,
    scenarioRevisionAdmission,
    previewBundle,
    applyBundle,
    control,
    setVlmModel,
    setActorModel,
    controlVlmModelRuntime,
    controlActorModelRuntime,
    setActorEnabled,
    controlLiveAsr,
    controlRosbagRecording,
    publishRosbagUiAudit,
    configureExecutionRoute,
    selectShadowCase,
    configureShadowReplay,
  };
}
