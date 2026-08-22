import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import ROSLIB from "roslib";

// Keep the bridge expiry and Debug UI's age badge/write lock on one contract.
// Exported so a future heartbeat change cannot silently split the two gates.
export const DEBUG_STATUS_MAX_AGE_MS = 3000;
const DEBUG_COMMAND_TIMEOUT_MS = 10000;
const MAX_DEBUG_STATUS_JSON_CHARS = 512 * 1024;
const MAX_DEBUG_STATUS_COLLECTION_ITEMS = 512;
const MAX_DEBUG_STATUS_OBJECT_KEYS = 512;
const MAX_DEBUG_STATUS_STRING_CHARS = 16 * 1024;
const MAX_DEBUG_COMMAND_RESULT_JSON_CHARS = 128 * 1024;
interface RosConnection {
  close: () => void;
  idCounter?: number;
  isConnected?: boolean;
  on: (event: string, callback: (message: unknown) => void) => void;
  off?: (event: string, callback: (message: unknown) => void) => void;
  removeListener?: (event: string, callback: (message: unknown) => void) => void;
  callOnConnection: (message: Record<string, unknown>) => void;
}

interface RosTopicHandle {
  advertise: () => void;
  unadvertise: () => void;
  publish: (message: unknown) => void;
}

export interface DebugReadOnlyTopicSpec {
  name: string;
  messageType: string;
  compression?: "cbor";
  throttleRate?: number;
  queueLength?: number;
  reliability?: "reliable" | "best_effort";
  /**
   * `/tf_static` is the one Debug observer stream that must receive retained
   * samples from every publisher.  Keep the default volatile so ordinary
   * image/diagnostic streams cannot accidentally request retained history.
   */
  durability?: "volatile" | "transient_local";
}

export type DebugReadOnlyTopicSubscriber = (
  spec: DebugReadOnlyTopicSpec,
  onMessage: (message: unknown) => void,
) => () => void;

export type DebugSessionState = "MONITOR_ONLY" | "ARMED" | "BUSY" | "FAULT_LOCKED";

export interface DebugInputStatus {
  name: string;
  topic: string;
  expected_type: string;
  actual_types: string[];
  publisher_count: number;
  publishers: string[];
  qos_profiles: string[];
  expected_qos: string;
  expected_hz: number;
  measured_hz: number;
  monitor_hz?: number;
  message_count: number;
  source_message_count?: number | null;
  source_dropped_count?: number | null;
  window_message_count: number;
  last_age_sec: number | null;
  source_delay_sec: number | null;
  bandwidth_bytes_sec: number;
  source_topic?: string;
  source_type?: string;
  source_qos?: string;
  last_sample: string;
  state: string;
}

export interface DebugEndpointStatus {
  name: "tool_handover" | "retraction_service" | "bed_robot_arm_status";
  endpoint: string;
  kind: "action" | "service" | "topic";
  ready: boolean;
}

export type DebugActionState =
  | "idle"
  | "submitting"
  | "accepted"
  | "executing"
  | "cancel_requested"
  | "cancel_accepted"
  | "cancel_rejected"
  | "completed"
  | "failed"
  | "rejected"
  | "remote_state_unknown"
  | "REMOTE_STATE_UNKNOWN"
  | (string & {});

export interface DebugActionStatus {
  route: string;
  command_id: string;
  command?: string;
  response_semantics?: "action" | "admission";
  request_accepted?: boolean | null;
  result_code?: number | null;
  response_message?: string;
  state: DebugActionState;
  progress: number;
  success: boolean;
  terminal: boolean;
  reason_code: string;
  recovery_required: boolean;
  elapsed_sec?: number;
  last_update_age_sec?: number | null;
  recovery_age_sec?: number | null;
  server_ready?: boolean;
  cancel_available?: boolean;
  source?: string;
}

export interface DebugRetractionVoiceInterpretation {
  transcript: string;
  command: string | null;
  target_side: string;
  distance_m: number;
  confidence: number;
  reason: string;
  /** Provenance such as text_vlm, deterministic_fallback, or shared_deterministic. */
  interpreter_source: string;
  /** True only after a model transport attempt actually occurred. */
  vlm_invoked: boolean;
  /** Bounded machine-readable outcome used to explain VLM success or fallback. */
  detail?: string;
}

export interface DebugRetractionVoiceStatus {
  /**
   * This gate only decides whether a final sentence admitted by the speech
   * adapter on /surgery/audio/request_text may be normalized and submitted.
   * It never owns microphone capture or the ASR process.
   */
  mode: "buttons_only" | "voice_and_buttons" | (string & {});
  /** Local Debug bookkeeping derived from Service admission, never robot pose. */
  internal_state: string;
  /** Selected runtime policy; voice mode alone never changes this setting. */
  interpreter_mode?: "deterministic" | "vlm_with_fallback" | (string & {});
  /** A final transcript is being interpreted asynchronously; no Service call yet. */
  interpreter_pending?: boolean;
  interpreter_pending_age_sec?: number | null;
  /** Commands admitted by the shared local policy for the current internal state. */
  allowed_commands: string[];
  service_ready: boolean;
  in_flight: boolean;
  last_interpretation: DebugRetractionVoiceInterpretation;
  last_rejection_reason: string;
}

export interface DebugVlmStatus {
  base_url?: string;
  model_id?: string;
  manager_reachable?: boolean;
  catalog_reachable?: boolean;
  load_state?: string;
  loaded?: boolean;
  available?: boolean;
  runtime_managed?: boolean;
  probe_pending?: boolean;
  detail?: string;
  last_probe_age_sec?: number | null;
  /** This result is interpretation-only and must never imply Service dispatch. */
  micro_test?: {
    state?: string;
    transcript?: string;
    interpretation?: Record<string, unknown> | string | null;
    latency_ms?: number | null;
    error?: string;
  };
}

export interface DebugVirtualRobotStatus {
  enabled?: boolean;
  /** Explicit routing source; virtual must never be presented as external. */
  selected_source?: "external" | "virtual" | (string & {});
  tool_handover_ready?: boolean;
  retraction_service_ready?: boolean;
  external_retraction_service_ready?: boolean;
  virtual_retraction_service_ready?: boolean;
  bed_status_ready?: boolean;
  external_tool_handover_ready?: boolean;
  virtual_tool_handover_ready?: boolean;
  external_bed_status_ready?: boolean;
  virtual_bed_status_ready?: boolean;
  profile_id?: string;
  external?: {
    tool_handover?: string;
    retraction_service?: string;
    bed_status?: string;
  };
  virtual?: {
    tool_handover?: string;
    retraction_service?: string;
    bed_status?: string;
  };
}

export interface DebugOutputStatus {
  topic: string;
  type: string;
  enabled: boolean;
  configured_hz: number;
  measured_hz: number;
  publish_count: number;
  sequence: number;
  last_age_sec: number | null;
  subscriber_count: number;
  subscribers: string[];
  conflicting_publishers: string[];
}

export interface DebugRecentEvent {
  stamp: string;
  event_type: string;
  payload: Record<string, unknown>;
}

export interface DebugNetworkAddress {
  interface: string;
  address: string;
  prefix_length: number;
  mac_address: string;
  up: boolean;
  loopback: boolean;
  multicast: boolean;
  primary: boolean;
  carrier?: boolean;
  operstate?: string;
  kind?: "ethernet" | "wifi" | "virtual";
}

export interface DebugNetworkStatus {
  preferred_interface?: string;
  primary_interface: string;
  primary_ipv4: string;
  prefix_length: number;
  gateway_ipv4: string;
  multicast_capable: boolean;
  interface_present?: boolean;
  interface_kind?: "ethernet" | "wifi" | "virtual" | "unknown";
  link_up?: boolean;
  selection_source?: string;
  addresses: DebugNetworkAddress[];
  settings_path: string;
  restart_supported: boolean;
  restart_scheduled: boolean;
  locked_to_runtime?: boolean;
  error?: string;
}

export interface DebugAsrDevice {
  id: number;
  name: string;
  input_channels: number;
  default_samplerate: number;
  default: boolean;
}

export interface DebugAsrFinal {
  stamp: string;
  text: string;
  response_latency_ms: number | null;
  latency_basis: "latest_pcm_send_complete_to_final_receive" | "unavailable";
  latency_correlated: false;
}

export interface DebugAsrStatus {
  available: boolean;
  dependency_error: string;
  state: "UNAVAILABLE" | "STOPPED" | "STARTING" | "LISTENING" | "STOPPING" | "ERROR";
  /** Reviewed ASR route selected for this Debug microphone session. */
  endpoint_id: "cloud" | "lan";
  server_url: string;
  topic: string;
  device_id: number | null;
  device_name: string;
  devices: DebugAsrDevice[];
  device_status: "READY" | "NO_INPUT" | "HOST_AUDIO_UNAVAILABLE" | "BRIDGE_ERROR";
  device_message: string;
  connected: boolean;
  audio_level_dbfs: number;
  peak_level_dbfs: number;
  elapsed_sec: number;
  blocks_captured: number;
  input_dropped: number;
  partial_text: string;
  finals: DebugAsrFinal[];
  last_error: string;
  recording_path: string;
  transcript_path: string;
  sample_rate: number;
  channels: number;
  sample_width_bits: number;
  block_frames: number;
  wire_chunk_bytes: number;
  input_sample_rate: number;
  input_channels: number;
  input_block_frames: number;
  resampling: boolean;
  sent_chunks: number;
  responses: number;
  dropped_chunks: number;
  sessions: number;
  padded_final_bytes: number;
  pending_chunks: number;
}

export interface DebugSurgeryRecordExample {
  case_id: string;
  filename: string;
  characters: number;
  bytes: number;
  lines: number;
  sha256: string;
  valid_for_api: boolean;
}

export interface DebugSurgeryRecordResult {
  state?: "SUCCEEDED" | "FAILED" | "REMOTE_STATE_UNKNOWN";
  request_id?: string;
  case_id?: string;
  filename?: string;
  endpoint?: string;
  room_name?: string;
  surgery_code?: string;
  date?: string;
  text_characters?: number;
  body_bytes?: number;
  text_sha256?: string;
  submitted_at?: string;
  completed_at?: string;
  duration_sec?: number;
  http_status?: number;
  success?: boolean;
  transport_error?: string;
  response_headers?: Record<string, string>;
  response_json?: Record<string, unknown> | null;
  response_text?: string;
  receipt_id?: string;
  received_at?: string;
  error_code?: string;
  error_message?: string;
  generated_record_body_returned?: boolean;
}

export interface DebugSurgeryRecordStatus {
  state: "IDLE" | "SUBMITTING" | "SUCCEEDED" | "FAILED" | "REMOTE_STATE_UNKNOWN";
  active_request_id: string;
  default_endpoint: string;
  input_dir: string;
  examples: DebugSurgeryRecordExample[];
  last_error: string;
  last_result: DebugSurgeryRecordResult;
  history: DebugSurgeryRecordResult[];
  api_key_configured: boolean;
  contract: {
    method: "POST";
    content_type: string;
    auth_header: "X-API-Key";
    max_text_characters: number;
    max_body_bytes: number;
    max_response_bytes?: number;
    max_response_text_bytes?: number;
    server_timeout_sec: number;
    generated_record_body_returned: boolean;
    result_lookup_defined: boolean;
    auto_retry?: boolean;
    reconciliation_defined?: boolean;
    allowed_endpoints?: string[];
  };
}

export interface IntegrationDebugStatus {
  schema: "taskplanner.integration_debug.status.v1";
  stamp_sec: number;
  session: {
    session_id: string;
    state: DebugSessionState;
    armed: boolean;
    acknowledged_blocked_nodes?: string[];
    planner_coexistence_active?: boolean;
    fault_locked: boolean;
    last_error: string;
    event_log_path: string;
  };
  runtime: {
    ros_domain_id: string;
    rmw_implementation: string;
    discovery_range: string;
    blocked_nodes: string[];
    detected_planner_nodes?: string[];
    operational_state?: string | null;
    operational_state_age_sec?: number | null;
    operational_runtime_stopped?: boolean;
    operational_running?: boolean;
    operational_active_robot_task_id?: string;
    operational_robot_state?: string;
    operational_cleaner_busy?: boolean;
    operational_state_publishers?: string[];
    operational_state_expected_publisher?: string;
    operational_state_publisher_trusted?: boolean;
    operational_state_fresh?: boolean;
    manual_control_available?: boolean;
    planner_coexistence_allowed?: boolean;
    action_watchdog?: {
      goal_response_timeout_sec: number;
      feedback_timeout_sec: number;
      max_duration_sec: number;
      server_loss_grace_sec: number;
    };
    network: DebugNetworkStatus;
  };
  inputs: DebugInputStatus[];
  endpoints: DebugEndpointStatus[];
  action: DebugActionStatus;
  outputs: DebugOutputStatus[];
  voice: {
    auto_execute: boolean;
    last_sentence: string;
    last_parse: {
      matched?: boolean;
      ambiguous?: boolean;
      operation?: string;
      payload?: Record<string, unknown>;
      reason?: string;
    };
    retraction?: DebugRetractionVoiceStatus;
  };
  /** Optional while the backend rolls out isolated VLM diagnostics. */
  vlm?: DebugVlmStatus;
  /** Optional while the explicit external/virtual endpoint selector rolls out. */
  virtual_robot?: DebugVirtualRobotStatus;
  asr: DebugAsrStatus;
  surgery_record: DebugSurgeryRecordStatus;
  recent_events: DebugRecentEvent[];
}

export interface DebugCommandResponse {
  accepted: boolean;
  command_id: string;
  message: string;
  result: Record<string, unknown>;
}

const DEBUG_SESSION_STATES = new Set<DebugSessionState>(["MONITOR_ONLY", "ARMED", "BUSY", "FAULT_LOCKED"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isBoundedDebugPayload(value: unknown, depth = 0): boolean {
  if (depth > 8) return false;
  if (typeof value === "string") return value.length <= MAX_DEBUG_STATUS_STRING_CHARS;
  if (Array.isArray(value)) {
    return value.length <= MAX_DEBUG_STATUS_COLLECTION_ITEMS
      && value.every((item) => isBoundedDebugPayload(item, depth + 1));
  }
  if (isRecord(value)) {
    const entries = Object.entries(value);
    return entries.length <= MAX_DEBUG_STATUS_OBJECT_KEYS
      && entries.every(([key, item]) => key.length <= MAX_DEBUG_STATUS_STRING_CHARS
        && isBoundedDebugPayload(item, depth + 1));
  }
  return true;
}

function hasRequiredDebugStatusShape(value: unknown): value is IntegrationDebugStatus {
  if (!isRecord(value) || value.schema !== "taskplanner.integration_debug.status.v1") return false;
  const session = value.session;
  const runtime = value.runtime;
  const network = isRecord(runtime) ? runtime.network : null;
  const action = value.action;
  const voice = value.voice;
  const asr = value.asr;
  const surgeryRecord = value.surgery_record;
  return isRecord(session)
    && typeof value.stamp_sec === "number" && Number.isFinite(value.stamp_sec) && value.stamp_sec >= 0
    && typeof session.session_id === "string" && session.session_id.trim().length > 0
    && typeof session.state === "string" && DEBUG_SESSION_STATES.has(session.state as DebugSessionState)
    && typeof session.armed === "boolean" && typeof session.fault_locked === "boolean"
    && typeof session.last_error === "string" && typeof session.event_log_path === "string"
    && isRecord(runtime)
    && Array.isArray(runtime.blocked_nodes) && runtime.blocked_nodes.every((node) => typeof node === "string")
    && isRecord(network) && Array.isArray(network.addresses)
    && isRecord(action) && typeof action.state === "string"
    && typeof action.progress === "number" && Number.isFinite(action.progress) && action.progress >= 0 && action.progress <= 1
    && typeof action.success === "boolean" && typeof action.terminal === "boolean" && typeof action.recovery_required === "boolean"
    && isRecord(voice) && typeof voice.auto_execute === "boolean"
    && typeof voice.last_sentence === "string" && isRecord(voice.last_parse)
    && isRecord(asr) && typeof asr.available === "boolean" && typeof asr.state === "string"
    && Array.isArray(asr.devices) && Array.isArray(asr.finals)
    && isRecord(surgeryRecord) && typeof surgeryRecord.state === "string"
    && Array.isArray(surgeryRecord.history) && Array.isArray(value.inputs)
    && Array.isArray(value.endpoints) && Array.isArray(value.outputs) && Array.isArray(value.recent_events);
}

function parseStatus(raw: unknown): IntegrationDebugStatus | null {
  if (typeof raw !== "string" || raw.length > MAX_DEBUG_STATUS_JSON_CHARS) return null;
  try {
    const value = JSON.parse(raw) as unknown;
    if (!isBoundedDebugPayload(value) || !hasRequiredDebugStatusShape(value)) return null;

    const endpoints = Array.isArray(value.endpoints)
      ? value.endpoints.filter((endpoint) => {
          const name = String(endpoint?.name || "").toLowerCase();
          const path = String(endpoint?.endpoint || "").toLowerCase();
          return name !== "suction" && !path.includes("/suction");
        })
      : [];
    const action = value.action?.route === "suction"
      ? {
          route: "",
          command_id: "",
          state: "idle",
          progress: 0,
          success: false,
          terminal: true,
          reason_code: "",
          recovery_required: false,
        }
      : value.action;

    return {
      ...value,
      endpoints,
      action,
    };
  } catch {
    return null;
  }
}

function cleanupDebugTopics(ros: any, topics: any[], advertisedTopic: any): void {
  if (!ros?.isConnected || ros.socket?.readyState !== WebSocket.OPEN) return;
  topics.forEach((topic) => {
    try {
      topic.unsubscribe();
    } catch {
      // The closed bridge has already released the subscription.
    }
  });
  try {
    advertisedTopic.unadvertise();
  } catch {
    // The closed bridge has already released the advertisement.
  }
}

function reconcileAcknowledgedSessionTransition(
  current: IntegrationDebugStatus | null,
  operation: string,
  payload: Record<string, unknown>,
): IntegrationDebugStatus | null {
  if (!current) return current;
  if (operation === "arm") {
    const acknowledgedBlockedNodes = Array.isArray(payload.acknowledged_blocked_nodes)
      ? payload.acknowledged_blocked_nodes
        .filter((node): node is string => typeof node === "string")
        .map((node) => node.trim())
        .filter(Boolean)
      : [];
    return {
      ...current,
      session: {
        ...current.session,
        state: "ARMED",
        armed: true,
        fault_locked: false,
        last_error: "",
        acknowledged_blocked_nodes: acknowledgedBlockedNodes,
        planner_coexistence_active: acknowledgedBlockedNodes.length > 0,
      },
    };
  }
  if (operation === "disarm") {
    return {
      ...current,
      session: {
        ...current.session,
        state: "MONITOR_ONLY",
        armed: false,
        acknowledged_blocked_nodes: [],
        planner_coexistence_active: false,
      },
    };
  }
  if (operation === "reset_fault") {
    return {
      ...current,
      session: {
        ...current.session,
        state: current.session.armed ? current.session.state : "MONITOR_ONLY",
        fault_locked: false,
        last_error: "",
      },
    };
  }
  return current;
}

export function useIntegrationDebugBridge(url: string) {
  const rosRef = useRef<RosConnection | null>(null);
  const heartbeatTopicRef = useRef<RosTopicHandle | null>(null);
  const bridgeGenerationRef = useRef(0);
  const commandReadyGenerationRef = useRef(0);
  const statusReceivedAtRef = useRef(0);
  const statusStampSecRef = useRef<number | null>(null);
  const pendingCommandCancelsRef = useRef(new Set<(reason: string) => void>());
  const [transportConnected, setTransportConnected] = useState(false);
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState<IntegrationDebugStatus | null>(null);
  const [readiness, setReadiness] = useState<Record<string, unknown> | null>(null);
  const [connectionError, setConnectionError] = useState("");
  const [statusReceivedAt, setStatusReceivedAt] = useState(0);
  const [connectionNonce, setConnectionNonce] = useState(0);
  const [reconnecting, setReconnecting] = useState(false);

  useLayoutEffect(() => {
    let disposed = false;
    let reconnectTimer: number | null = null;
    const generation = bridgeGenerationRef.current + 1;
    bridgeGenerationRef.current = generation;
    commandReadyGenerationRef.current = 0;
    statusReceivedAtRef.current = 0;
    statusStampSecRef.current = null;
    for (const cancel of Array.from(pendingCommandCancelsRef.current)) {
      cancel("디버그 ROSBridge 연결이 변경되어 대기 중인 명령을 취소했습니다.");
    }
    setTransportConnected(false);
    setConnected(false);
    setStatus(null);
    setReadiness(null);
    setConnectionError("");
    const ros = new ROSLIB.Ros();
    rosRef.current = ros;
    const isCurrentGeneration = () =>
      !disposed && bridgeGenerationRef.current === generation && rosRef.current === ros;

    const statusTopic = new ROSLIB.Topic({
      ros,
      name: "/integration/debug/status",
      messageType: "std_msgs/msg/String",
    });
    const readinessTopic = new ROSLIB.Topic({
      ros,
      name: "/integration/debug/readiness",
      messageType: "std_msgs/msg/String",
    });
    const heartbeatTopic = new ROSLIB.Topic({
      ros,
      name: "/integration/debug/heartbeat",
      messageType: "std_msgs/msg/String",
      queue_size: 1,
    });
    heartbeatTopicRef.current = heartbeatTopic;

    function scheduleReconnect() {
      if (!isCurrentGeneration() || reconnectTimer !== null) return;
      setReconnecting(true);
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        if (isCurrentGeneration()) setConnectionNonce((value) => value + 1);
      }, 1200);
    }

    function invalidateCommandReadiness(reason: string) {
      if (!isCurrentGeneration() || commandReadyGenerationRef.current !== generation) return;
      commandReadyGenerationRef.current = 0;
      setConnected(false);
      for (const cancel of Array.from(pendingCommandCancelsRef.current)) {
        cancel(reason);
      }
    }

    ros.on("connection", () => {
      if (!isCurrentGeneration()) return;
      heartbeatTopic.advertise();
      setTransportConnected(true);
      setConnected(false);
      setReconnecting(false);
      setConnectionError("");
    });
    ros.on("error", (error: unknown) => {
      if (!isCurrentGeneration()) return;
      setTransportConnected(false);
      invalidateCommandReadiness("디버그 ROSBridge 오류로 대기 중인 명령을 취소했습니다.");
      setConnectionError(error instanceof Error ? error.message : "ROSBridge 연결에 실패했습니다.");
      scheduleReconnect();
    });
    ros.on("close", () => {
      if (!isCurrentGeneration()) return;
      setTransportConnected(false);
      invalidateCommandReadiness("디버그 ROSBridge 연결이 종료되어 대기 중인 명령을 취소했습니다.");
      setConnectionError("ROSBridge 연결이 종료되었습니다.");
      scheduleReconnect();
    });
    statusTopic.subscribe((message: { data?: unknown }) => {
      if (!isCurrentGeneration()) return;
      const parsed = parseStatus(message.data);
      if (!parsed) {
        setConnectionError("디버그 상태 스냅샷이 유효하지 않습니다.");
        invalidateCommandReadiness(
          "디버그 상태 스냅샷이 유효하지 않아 쓰기 제어를 잠갔습니다.",
        );
        return;
      }
      const receivedAt = Date.now();
      if (
        statusStampSecRef.current !== null
        && parsed.stamp_sec + 0.001 < statusStampSecRef.current
      ) {
        setConnectionError("디버그 상태 스냅샷의 시각이 이전 상태보다 뒤로 갔습니다.");
        invalidateCommandReadiness(
          "디버그 상태 스냅샷 순서가 역전되어 쓰기 제어를 잠갔습니다.",
        );
        return;
      }
      const payloadAgeMs = receivedAt - parsed.stamp_sec * 1_000;
      if (payloadAgeMs > DEBUG_STATUS_MAX_AGE_MS * 2 || payloadAgeMs < -60_000) {
        setConnectionError("디버그 상태 스냅샷의 시각이 현재 연결과 맞지 않습니다.");
        invalidateCommandReadiness(
          "디버그 상태 스냅샷이 오래되었거나 시각이 유효하지 않아 쓰기 제어를 잠갔습니다.",
        );
        return;
      }
      statusStampSecRef.current = parsed.stamp_sec;
      statusReceivedAtRef.current = receivedAt;
      commandReadyGenerationRef.current = generation;
      setStatus(parsed);
      setStatusReceivedAt(receivedAt);
      if (ros.isConnected) setConnected(true);
    });
    readinessTopic.subscribe((message: { data?: unknown }) => {
      if (!isCurrentGeneration()) return;
      if (typeof message.data !== "string" || message.data.length > MAX_DEBUG_STATUS_JSON_CHARS) return;
      try {
        const value = JSON.parse(message.data) as Record<string, unknown>;
        setReadiness(isBoundedDebugPayload(value) ? value : null);
      } catch {
        setReadiness(null);
      }
    });
    const connectionTimer = window.setTimeout(() => {
      if (isCurrentGeneration()) ros.connect(url);
    }, 0);
    // The command path checks this same age synchronously before every write;
    // this slower repaint only updates the visible lock when a heartbeat dies.
    const freshnessTimer = window.setInterval(() => {
      if (!isCurrentGeneration()) return;
      const now = Date.now();
      if (
        commandReadyGenerationRef.current === generation &&
        now - statusReceivedAtRef.current > DEBUG_STATUS_MAX_AGE_MS
      ) {
        invalidateCommandReadiness(
          "디버그 상태 heartbeat가 만료되어 대기 중인 명령을 취소했습니다.",
        );
      }
    }, 500);

    return () => {
      disposed = true;
      window.clearTimeout(connectionTimer);
      window.clearInterval(freshnessTimer);
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      if (bridgeGenerationRef.current === generation) {
        commandReadyGenerationRef.current = 0;
        statusReceivedAtRef.current = 0;
        statusStampSecRef.current = null;
        setTransportConnected(false);
        setConnected(false);
        for (const cancel of Array.from(pendingCommandCancelsRef.current)) {
          cancel("디버그 ROSBridge 연결이 변경되어 대기 중인 명령을 취소했습니다.");
        }
      }
      cleanupDebugTopics(
        ros,
        [statusTopic, readinessTopic],
        heartbeatTopic,
      );
      heartbeatTopicRef.current = null;
      if (rosRef.current === ros) rosRef.current = null;
      try {
        ros.close();
      } catch {
        // The bridge can already be closed during mode exit.
      }
    };
  }, [connectionNonce, url]);

  const command = useCallback(
    async (operation: string, payload: Record<string, unknown> = {}): Promise<DebugCommandResponse> => {
      const ros = rosRef.current;
      const generation = bridgeGenerationRef.current;
      const statusFreshNow = () =>
        Date.now() - statusReceivedAtRef.current <= DEBUG_STATUS_MAX_AGE_MS;
      if (
        !ros ||
        !ros.isConnected ||
        !connected ||
        commandReadyGenerationRef.current !== generation ||
        !statusFreshNow() ||
        pendingCommandCancelsRef.current.size > 0
      ) {
        throw new Error("디버그 제어가 잠겼습니다.");
      }
      return new Promise<DebugCommandResponse>((resolve, reject) => {
        let settled = false;
        let timeout = 0;
        const serviceCallId = `call_service:/integration/debug/command:${Number(ros.idCounter ?? 0) + 1}`;
        ros.idCounter = Number(ros.idCounter ?? 0) + 1;
        const cleanup = (handler: (message: unknown) => void) => {
          window.clearTimeout(timeout);
          pendingCommandCancelsRef.current.delete(cancel);
          if (typeof ros.off === "function") ros.off(serviceCallId, handler);
          else if (typeof ros.removeListener === "function") ros.removeListener(serviceCallId, handler);
        };
        let cancel = (_reason: string) => {};
        const isCurrentCommand = () =>
          generation === bridgeGenerationRef.current &&
          commandReadyGenerationRef.current === generation &&
          rosRef.current === ros &&
          ros.isConnected &&
          statusFreshNow();
        const handler = (message: unknown) => {
          if (!isCurrentCommand()) {
            cancel("디버그 ROSBridge 연결이 변경되어 이전 명령 응답을 무시했습니다.");
            return;
          }
          if (settled) return;
          settled = true;
          cleanup(handler);
          const response = message && typeof message === "object"
            ? message as { result?: unknown; values?: Record<string, unknown> | string }
            : {};
          if (response.result !== true) {
            reject(new Error(String(response.values || "디버그 명령 응답을 확인할 수 없습니다.")));
            return;
          }
          if (!response.values || typeof response.values !== "object" || Array.isArray(response.values)) {
            reject(new Error("디버그 명령 응답 형식이 유효하지 않습니다."));
            return;
          }
          const raw = response.values;
          if (typeof raw.accepted !== "boolean"
            || typeof raw.command_id !== "string"
            || typeof raw.message !== "string") {
            reject(new Error("디버그 명령 응답의 수락 형식이 유효하지 않습니다."));
            return;
          }
          let result: Record<string, unknown> = {};
          if (typeof raw.result_json === "string"
            && raw.result_json
            && raw.result_json.length <= MAX_DEBUG_COMMAND_RESULT_JSON_CHARS) {
            try {
              const parsed = JSON.parse(raw.result_json) as unknown;
              if (isBoundedDebugPayload(parsed) && parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                result = parsed as Record<string, unknown>;
              }
            } catch {
              result = {};
            }
          }
          const commandResponse = {
            accepted: raw.accepted,
            command_id: raw.command_id,
            message: raw.message,
            result,
          };
          if (commandResponse.accepted) {
            // The command service is the authority that admitted arm/disarm.
            // Reflect that receipt immediately, while the next status topic
            // remains the source of truth and freshness still expires normally.
            setStatus((current) => reconcileAcknowledgedSessionTransition(
              current,
              operation,
              payload,
            ));
          }
          resolve(commandResponse);
        };
        cancel = (reason: string) => {
          if (settled) return;
          settled = true;
          cleanup(handler);
          reject(new Error(reason));
        };
        pendingCommandCancelsRef.current.add(cancel);
        timeout = window.setTimeout(
          () => cancel("디버그 명령 응답 시간이 초과되었습니다."),
          DEBUG_COMMAND_TIMEOUT_MS,
        );
        ros.on(serviceCallId, handler);
        try {
          ros.callOnConnection({
            op: "call_service",
            id: serviceCallId,
            service: "/integration/debug/command",
            type: "surgical_msgs/srv/IntegrationDebugCommand",
            args: new ROSLIB.ServiceRequest({
              operation,
              payload_json: JSON.stringify(payload),
            }),
            timeout: DEBUG_COMMAND_TIMEOUT_MS / 1_000,
          });
        } catch (error) {
          cancel(error instanceof Error ? error.message : String(error));
        }
      });
    },
    [connected],
  );

  useEffect(() => {
    if (!connected || !status?.session.armed) return;
    const publishHeartbeat = () => {
      const topic = heartbeatTopicRef.current;
      if (!topic) return;
      topic.publish(new ROSLIB.Message({ data: status.session.session_id }));
    };
    const heartbeat = window.setInterval(publishHeartbeat, 2000);
    publishHeartbeat();
    return () => window.clearInterval(heartbeat);
  }, [connected, status?.session.armed, status?.session.session_id]);

  const subscribeReadOnlyTopic = useCallback<DebugReadOnlyTopicSubscriber>((spec, onMessage) => {
    const ros = rosRef.current;
    const generation = bridgeGenerationRef.current;
    if (!ros || !ros.isConnected) return () => {};
    let disposed = false;
    let unsubscribe = () => {};
    const guardedMessage = (message: unknown) => {
      if (
        rosRef.current === ros
        && bridgeGenerationRef.current === generation
        && ros.isConnected
      ) onMessage(message);
    };
    void import("../utils/debugReadOnlyTopicSubscription").then(({ subscribeDebugReadOnlyTopic }) => {
      if (disposed || rosRef.current !== ros || bridgeGenerationRef.current !== generation) return;
      unsubscribe = subscribeDebugReadOnlyTopic(ros, spec, guardedMessage);
    }).catch(() => {
      // The panel remains fail-closed if its deferred read-only transport cannot load.
    });
    return () => {
      disposed = true;
      unsubscribe();
    };
  }, [connectionNonce, transportConnected]);

  const retry = useCallback(() => {
    setReconnecting(true);
    setConnectionNonce((value) => value + 1);
  }, []);

  return {
    url,
    transportConnected,
    connected,
    reconnecting,
    connectionError,
    status,
    readiness,
    statusReceivedAt,
    subscribeReadOnlyTopic,
    command,
    retry,
  };
}
