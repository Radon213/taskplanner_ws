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
  name: "tool_handover" | "retraction" | "suction";
  endpoint: string;
  kind: "action" | "service";
  ready: boolean;
}

export interface DebugActionStatus {
  route: string;
  command_id: string;
  state: string;
  progress: number;
  success: boolean;
  terminal: boolean;
  reason_code: string;
  elapsed_sec?: number;
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
}

export interface DebugNetworkStatus {
  primary_interface: string;
  primary_ipv4: string;
  prefix_length: number;
  gateway_ipv4: string;
  multicast_capable: boolean;
  addresses: DebugNetworkAddress[];
  settings_path: string;
  restart_supported: boolean;
  restart_scheduled: boolean;
  error?: string;
}

export interface IntegrationDebugStatus {
  schema: "taskplanner.integration_debug.status.v1";
  stamp_sec: number;
  session: {
    session_id: string;
    state: DebugSessionState;
    armed: boolean;
    fault_locked: boolean;
    last_error: string;
    event_log_path: string;
  };
  runtime: {
    ros_domain_id: string;
    rmw_implementation: string;
    discovery_range: string;
    blocked_nodes: string[];
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
    return value.schema === "taskplanner.integration_debug.status.v1"
      ? (value as IntegrationDebugStatus)
      : null;
  } catch {
    return null;
  }
}

export function useIntegrationDebugBridge(url: string) {
  const rosRef = useRef<RosConnection | null>(null);
  const sentenceTopicRef = useRef<RosTopicHandle | null>(null);
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
    sentenceTopicRef.current = sentenceTopic;

    function scheduleReconnect() {
      if (disposed || reconnectTimer !== null) return;
      setReconnecting(true);
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        setConnectionNonce((value) => value + 1);
      }, 1200);
    }

    ros.on("connection", () => {
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
    const heartbeat = window.setInterval(() => {
      void command("heartbeat").catch(() => undefined);
    }, 2000);
    void command("heartbeat").catch(() => undefined);
    return () => window.clearInterval(heartbeat);
  }, [command, connected, status?.session.armed]);

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
