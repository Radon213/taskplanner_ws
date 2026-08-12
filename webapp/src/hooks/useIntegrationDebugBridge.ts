import { useCallback, useEffect, useRef, useState } from "react";
import ROSLIB from "roslib";

interface RosConnection {
  close: () => void;
}

interface RosTopicHandle {
  advertise: () => void;
  unadvertise: () => void;
  publish: (message: unknown) => void;
}

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
  message_count: number;
  window_message_count: number;
  last_age_sec: number | null;
  source_delay_sec: number | null;
  bandwidth_bytes_sec: number;
  last_sample: string;
  state: string;
}

export interface DebugEndpointStatus {
  name: "tool_handover" | "retraction_adjustment" | "tool_change" | "bed_robot_arm_status";
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
  server_url: string;
  topic: string;
  device_id: number | null;
  device_name: string;
  devices: DebugAsrDevice[];
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
  };
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

function parseStatus(raw: unknown): IntegrationDebugStatus | null {
  if (typeof raw !== "string") return null;
  try {
    const value = JSON.parse(raw) as Partial<IntegrationDebugStatus>;
    if (value.schema !== "taskplanner.integration_debug.status.v1") return null;

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
        }
      : value.action;

    return {
      ...value,
      endpoints,
      action,
    } as IntegrationDebugStatus;
  } catch {
    return null;
  }
}

export function useIntegrationDebugBridge(url: string) {
  const rosRef = useRef<RosConnection | null>(null);
  const sentenceTopicRef = useRef<RosTopicHandle | null>(null);
  const heartbeatTopicRef = useRef<RosTopicHandle | null>(null);
  const sentenceUnadvertiseTimerRef = useRef<number | null>(null);
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState<IntegrationDebugStatus | null>(null);
  const [readiness, setReadiness] = useState<Record<string, unknown> | null>(null);
  const [connectionError, setConnectionError] = useState("");
  const [statusReceivedAt, setStatusReceivedAt] = useState(0);
  const [connectionNonce, setConnectionNonce] = useState(0);
  const [reconnecting, setReconnecting] = useState(false);

  useEffect(() => {
    let disposed = false;
    let reconnectTimer: number | null = null;
    setConnected(false);
    setStatus(null);
    setReadiness(null);
    setConnectionError("");

    const ros = new ROSLIB.Ros();
    rosRef.current = ros;
    const statusTopic = new ROSLIB.Topic({
      ros,
      name: "/integration/debug/status",
      messageType: "std_msgs/msg/String",
    });
    const readinessTopic = new ROSLIB.Topic({
      ros,
      name: "/integration/readiness",
      messageType: "std_msgs/msg/String",
    });
    const sentenceTopic = new ROSLIB.Topic({
      ros,
      name: "/sensors/surgeon/sentence",
      messageType: "std_msgs/msg/String",
      queue_size: 1,
    });
    const heartbeatTopic = new ROSLIB.Topic({
      ros,
      name: "/integration/debug/heartbeat",
      messageType: "std_msgs/msg/String",
      queue_size: 1,
    });
    sentenceTopicRef.current = sentenceTopic;
    heartbeatTopicRef.current = heartbeatTopic;

    function scheduleReconnect() {
      if (disposed || reconnectTimer !== null) return;
      setReconnecting(true);
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        setConnectionNonce((value) => value + 1);
      }, 1200);
    }

    ros.on("connection", () => {
      heartbeatTopic.advertise();
      setConnected(true);
      setReconnecting(false);
      setConnectionError("");
    });
    ros.on("error", (error: unknown) => {
      setConnectionError(error instanceof Error ? error.message : "ROSBridge 연결에 실패했습니다.");
      scheduleReconnect();
    });
    ros.on("close", () => {
      setConnected(false);
      setConnectionError("ROSBridge 연결이 종료되었습니다.");
      scheduleReconnect();
    });
    statusTopic.subscribe((message: { data?: unknown }) => {
      const parsed = parseStatus(message.data);
      if (!parsed) return;
      setStatus(parsed);
      setStatusReceivedAt(Date.now());
    });
    readinessTopic.subscribe((message: { data?: unknown }) => {
      if (typeof message.data !== "string") return;
      try {
        const value = JSON.parse(message.data) as Record<string, unknown>;
        setReadiness(value);
      } catch {
        setReadiness(null);
      }
    });
    const connectionTimer = window.setTimeout(() => {
      if (!disposed) ros.connect(url);
    }, 0);

    return () => {
      disposed = true;
      window.clearTimeout(connectionTimer);
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      statusTopic.unsubscribe();
      readinessTopic.unsubscribe();
      if (sentenceUnadvertiseTimerRef.current !== null) {
        window.clearTimeout(sentenceUnadvertiseTimerRef.current);
        sentenceUnadvertiseTimerRef.current = null;
      }
      sentenceTopic.unadvertise();
      sentenceTopicRef.current = null;
      heartbeatTopic.unadvertise();
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
      if (!ros || !connected) {
        throw new Error("디버그 ROSBridge가 연결되지 않았습니다.");
      }
      const service = new ROSLIB.Service({
        ros,
        name: "/integration/debug/command",
        serviceType: "surgical_msgs/srv/IntegrationDebugCommand",
      });
      return new Promise<DebugCommandResponse>((resolve, reject) => {
        const request = new ROSLIB.ServiceRequest({
          operation,
          payload_json: JSON.stringify(payload),
        });
        service.callService(
          request,
          (raw: Record<string, unknown>) => {
            let result: Record<string, unknown> = {};
            if (typeof raw.result_json === "string" && raw.result_json) {
              try {
                const parsed = JSON.parse(raw.result_json) as unknown;
                if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                  result = parsed as Record<string, unknown>;
                }
              } catch {
                result = {};
              }
            }
            resolve({
              accepted: Boolean(raw.accepted),
              command_id: String(raw.command_id ?? ""),
              message: String(raw.message ?? ""),
              result,
            });
          },
          (error: unknown) => {
            reject(error instanceof Error ? error : new Error(String(error)));
          },
        );
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

  const publishSentence = useCallback((sentence: string) => {
    const topic = sentenceTopicRef.current;
    if (!topic || !connected) {
      throw new Error("디버그 ROSBridge가 연결되지 않았습니다.");
    }
    const normalized = sentence.trim();
    if (!normalized) throw new Error("완성된 문장을 입력해 주세요.");
    topic.advertise();
    topic.publish(new ROSLIB.Message({ data: normalized }));
    if (sentenceUnadvertiseTimerRef.current !== null) {
      window.clearTimeout(sentenceUnadvertiseTimerRef.current);
    }
    sentenceUnadvertiseTimerRef.current = window.setTimeout(() => {
      if (sentenceTopicRef.current === topic) topic.unadvertise();
      sentenceUnadvertiseTimerRef.current = null;
    }, 500);
  }, [connected]);

  const retry = useCallback(() => {
    setReconnecting(true);
    setConnectionNonce((value) => value + 1);
  }, []);

  return {
    url,
    connected,
    reconnecting,
    connectionError,
    status,
    readiness,
    statusReceivedAt,
    command,
    publishSentence,
    retry,
  };
}
