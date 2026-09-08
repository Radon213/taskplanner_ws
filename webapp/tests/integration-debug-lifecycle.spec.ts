import { expect, test, type Page, type WebSocketRoute } from "playwright/test";

type DebugSocketOptions = {
  runtimeMode?: "debug" | "live";
  respondToCommands?: boolean;
  commandResponseDelayMs?: number;
  refreshStatusOnCommand?: boolean;
  statusForConnection?: (connection: number) => Record<string, unknown>;
  resultForCommand?: (operation: string, payload: Record<string, unknown>) => Record<string, unknown>;
  commandResponseValues?: (operation: string, payload: Record<string, unknown>) => Record<string, unknown>;
  onCommand?: (operation: string, payload: Record<string, unknown>) => void;
};

type RetractionVoiceStatusOptions = {
  internalState?: string;
  allowedCommands?: string[];
  serviceReady?: boolean;
  inFlight?: boolean;
  lastRejectionReason?: string;
};

function retractionVoiceStatus({
  internalState = "idle",
  allowedCommands = ["change_tool", "start_direct_teach"],
  serviceReady = false,
  inFlight = false,
  lastRejectionReason = "",
}: RetractionVoiceStatusOptions = {}): Record<string, unknown> {
  return {
    mode: "direct_service_only",
    internal_state: internalState,
    allowed_commands: allowedCommands,
    service_ready: serviceReady,
    in_flight: inFlight,
    last_rejection_reason: lastRejectionReason,
  };
}

function debugAsrStatus(): Record<string, unknown> {
  return {
    available: true,
    dependency_error: "",
    state: "STOPPED",
    endpoint_id: "cloud",
    server_url: "wss://arpa.worker-02.puzzle-ai.com",
    topic: "/sensors/surgeon/sentence",
    device_id: 7,
    device_name: "USB Audio Microphone",
    devices: [{ id: 7, name: "USB Audio Microphone", input_channels: 1, default_samplerate: 48_000, default: true }],
    device_status: "READY",
    device_message: "USB input ready",
    connected: false,
    audio_level_dbfs: -60,
    peak_level_dbfs: -60,
    elapsed_sec: 0,
    blocks_captured: 0,
    input_dropped: 0,
    partial_text: "",
    finals: [],
    last_error: "",
    recording_path: "/tmp/debug-asr.wav",
    transcript_path: "/tmp/debug-asr.txt",
    sample_rate: 16_000,
    channels: 1,
    sample_width_bits: 16,
    block_frames: 4_096,
    wire_chunk_bytes: 8_192,
    input_sample_rate: 48_000,
    input_channels: 1,
    input_block_frames: 4_800,
    resampling: true,
    sent_chunks: 0,
    responses: 0,
    dropped_chunks: 0,
    sessions: 0,
    padded_final_bytes: 0,
    pending_chunks: 0,
  };
}

function debugInput(name: string, topic: string): Record<string, unknown> {
  const observedUtterance = topic === "/surgery/audio/observed_utterance";
  return {
    name,
    topic,
    expected_type: observedUtterance ? "surgical_msgs/msg/SpeechUtterance" : "std_msgs/msg/String",
    actual_types: [observedUtterance ? "surgical_msgs/msg/SpeechUtterance" : "std_msgs/msg/String"],
    publisher_count: 1,
    publishers: ["/integration_debug_test"],
    qos_profiles: ["RELIABLE"],
    expected_qos: "RELIABLE",
    expected_hz: 0,
    measured_hz: 1,
    message_count: 1,
    window_message_count: 1,
    last_age_sec: 0.1,
    source_delay_sec: 0.02,
    bandwidth_bytes_sec: 64,
    last_sample: "테스트 final 문장",
    state: "READY",
  };
}

function debugStatus(sessionId: string, armed = false): Record<string, unknown> {
  return {
    schema: "taskplanner.integration_debug.status.v1",
    stamp_sec: Date.now() / 1000,
    session: {
      session_id: sessionId,
      state: armed ? "ARMED" : "MONITOR_ONLY",
      armed,
      fault_locked: false,
      last_error: "",
      event_log_path: "/tmp/debug-events.jsonl",
    },
    runtime: {
      ros_domain_id: "0",
      rmw_implementation: "rmw_fastrtps_cpp",
      discovery_range: "LOCALHOST",
      blocked_nodes: [],
      operational_state: "idle",
      operational_running: false,
      operational_runtime_stopped: true,
      operational_intervention_allowed: true,
      operational_intervention_block_reason: "",
      operational_control_window_open: true,
      manual_control_available: true,
      manual_control_gate: "planner_nodes",
      planner_coexistence_allowed: false,
      network: {
        primary_interface: "eth0",
        primary_ipv4: "127.0.0.1",
        prefix_length: 8,
        gateway_ipv4: "",
        multicast_capable: true,
        interface_present: true,
        link_up: true,
        addresses: [],
        settings_path: "/tmp/debug-network.json",
        restart_supported: true,
        restart_scheduled: false,
      },
    },
    inputs: [
      debugInput("surgeon_sentence", "/sensors/surgeon/sentence"),
      debugInput("observed_surgeon_utterance", "/surgery/audio/observed_utterance"),
    ],
    endpoints: [],
    action: {
      route: "",
      command_id: "",
      state: "idle",
      progress: 0,
      success: false,
      terminal: true,
      reason_code: "",
      recovery_required: false,
    },
    outputs: [],
    voice: {
      auto_execute: false,
      last_sentence: "",
      last_parse: {},
      retraction: retractionVoiceStatus(),
    },
    virtual_robot: {
      enabled: true,
      selected_source: "external",
      tool_handover_ready: false,
      retraction_service_ready: false,
      external_retraction_service_ready: false,
      virtual_retraction_service_ready: true,
      bed_status_ready: false,
      external_tool_handover_ready: false,
      virtual_tool_handover_ready: true,
      external_bed_status_ready: false,
      virtual_bed_status_ready: true,
      profile_id: "mock-admission-only",
    },
    asr: debugAsrStatus(),
    surgery_record: { state: "IDLE", history: [] },
    recent_events: [],
  };
}

function retractionServiceStatus(sessionId: string): Record<string, unknown> {
  const status = debugStatus(sessionId, true);
  status.endpoints = [
    {
      name: "retraction_service",
      endpoint: "/surgery/retraction/command",
      kind: "service",
      ready: true,
    },
  ];
  status.voice = {
    auto_execute: false,
    last_sentence: "",
    last_parse: {},
    retraction: retractionVoiceStatus({
      internalState: "taught_ready",
      allowedCommands: ["start_direct_teach", "start_retraction"],
      serviceReady: true,
    }),
  };
  status.virtual_robot = {
    ...(status.virtual_robot as Record<string, unknown>),
    selected_source: "external",
    retraction_service_ready: true,
    external_retraction_service_ready: true,
  };
  status.action = {
    route: "retraction_service",
    command_id: "retraction-command-1",
    command: "start_retraction",
    response_semantics: "admission",
    request_accepted: true,
    result_code: 0,
    response_message: "accepted for controller admission",
    state: "accepted",
    progress: 0,
    success: false,
    terminal: true,
    reason_code: "RESULT_ACCEPTED",
    recovery_required: false,
  };
  return status;
}

function manualControlsReadyStatus(sessionId: string): Record<string, unknown> {
  const status = debugStatus(sessionId);
  status.endpoints = [
    {
      name: "tool_handover",
      endpoint: "/surgery/tool_handover",
      kind: "action",
      ready: true,
    },
    {
      name: "retraction_service",
      endpoint: "/surgery/retraction/command",
      kind: "service",
      ready: true,
    },
  ];
  status.voice = {
    auto_execute: false,
    last_sentence: "",
    last_parse: {},
    retraction: retractionVoiceStatus({
      allowedCommands: ["change_tool", "start_direct_teach"],
      serviceReady: true,
    }),
  };
  status.virtual_robot = {
    ...(status.virtual_robot as Record<string, unknown>),
    selected_source: "external",
    tool_handover_ready: true,
    retraction_service_ready: true,
    external_retraction_service_ready: true,
  };
  return status;
}

async function openDebugWorkspace(page: Page, options: DebugSocketOptions = {}) {
  const runtimeMode = options.runtimeMode ?? "debug";
  let connectionCount = 0;
  const sockets: WebSocketRoute[] = [];
  const readOnlyServiceCalls: string[] = [];
  const subscriptionsBySocket = new Map<WebSocketRoute, Set<string>>();
  const subscriptionRequestsBySocket = new Map<WebSocketRoute, Map<string, Record<string, unknown>>>();
  await page.addInitScript((mode: "debug" | "live") => {
    window.localStorage.setItem("taskplanner.runtimeMode.live", mode);
  }, runtimeMode);
  await page.route("**/api/runtime/status", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      phase: "idle",
      active_mode: runtimeMode,
      requested_mode: runtimeMode,
      message: "Selected runtime is ready.",
      retryable: false,
    }),
  }));
  await page.route("**/api/runtime/lifecycle", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      phase: "idle",
      generation: 0,
      job_id: null,
      request_id: null,
      operation: null,
      active_mode: runtimeMode,
      message: "Runtime lifecycle control is ready.",
      retryable: false,
      ninfer: {
        available: true,
        model_id: "qwen3.6-35b-a3b",
        model_state: "loaded",
        detail: "ready",
      },
    }),
  }));
  const debugPort = runtimeMode === "live" ? "9093" : "9091";
  await page.routeWebSocket(new RegExp(`ws://127\\.0\\.0\\.1:${debugPort}/?$`), (socket) => {
    connectionCount += 1;
    const connection = connectionCount;
    let status: Record<string, unknown> | null = null;
    sockets.push(socket);
    subscriptionsBySocket.set(socket, new Set());
    subscriptionRequestsBySocket.set(socket, new Map());
    socket.onMessage((raw) => {
      const message = JSON.parse(typeof raw === "string" ? raw : raw.toString()) as {
        op?: string;
        id?: string;
        service?: string;
        topic?: string;
        args?: { operation?: string; payload_json?: string };
      };
      if (message.op === "subscribe" && message.topic) {
        subscriptionsBySocket.get(socket)?.add(message.topic);
        subscriptionRequestsBySocket.get(socket)?.set(
          message.topic,
          message as unknown as Record<string, unknown>,
        );
      }
      if (message.op === "unsubscribe" && message.topic) {
        subscriptionsBySocket.get(socket)?.delete(message.topic);
        subscriptionRequestsBySocket.get(socket)?.delete(message.topic);
        return;
      }
      if (message.op === "subscribe" && message.topic === "/integration/debug/status") {
        status = options.statusForConnection?.(connection) ?? debugStatus(`session-${connection}`);
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: { data: JSON.stringify(status) },
        }));
        return;
      }
      if (message.op === "subscribe" && message.topic === "/tf_static") {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            transforms: [{
              header: { frame_id: "cam_4_color_optical_frame", stamp: { sec: 1_900_000_100, nanosec: 100 } },
              child_frame_id: "tag1",
              transform: {
                translation: { x: 0.12, y: -0.04, z: 0.82 },
                rotation: { x: 0, y: 0, z: 0, w: 1 },
              },
            }],
          },
        }));
        return;
      }
      if (message.op === "subscribe" && message.topic === "/tf") {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            transforms: [{
              header: { frame_id: "cam_4_color_optical_frame", stamp: { sec: 1_900_000_101, nanosec: 200 } },
              child_frame_id: "cam_4_bovie_0",
              transform: {
                translation: { x: 0.042, y: -0.027, z: 0.806 },
                rotation: { x: 0, y: 0, z: 0.3826834, w: 0.9238795 },
              },
            }],
          },
        }));
        return;
      }
      if (message.op === "subscribe" && message.topic === "/multicam_node/capture_status") {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            online_cameras: ["cam_1", "cam_2", "cam_3", "cam_4", "flir"],
            offline_cameras: [],
            all_cameras_online: true,
            uptime_sec: 12,
            cameras: [],
          },
        }));
        return;
      }
      if (message.op !== "call_service" || !message.id || !message.service) return;
      if (message.service === "/rosapi/topics") {
        readOnlyServiceCalls.push(message.service);
        socket.send(JSON.stringify({
          op: "service_response",
          id: message.id,
          service: message.service,
          result: true,
          values: {
            topics: [
              "/multicam_node/capture_status",
              "/world_anchor_node/status",
              "/synced/cam_1/color/image_raw/compressed",
              "/synced/cam_4/depth/image_rect_raw/compressedDepth",
            ],
            types: [
              "arpa_multicam_msgs/msg/CaptureStatus",
              "std_msgs/msg/String",
              "sensor_msgs/msg/CompressedImage",
              "sensor_msgs/msg/CompressedImage",
            ],
          },
        }));
        return;
      }
      let payload: Record<string, unknown> = {};
      try {
        const decoded = JSON.parse(String(message.args?.payload_json ?? "{}"));
        if (decoded && typeof decoded === "object" && !Array.isArray(decoded)) {
          payload = decoded as Record<string, unknown>;
        }
      } catch {
        // Test socket still records the operation when a malformed payload is sent.
      }
      options.onCommand?.(String(message.args?.operation ?? ""), payload);
      if (options.respondToCommands === false) return;
      const sendResponse = () => {
        socket.send(JSON.stringify({
          op: "service_response",
          id: message.id,
          service: message.service,
          result: true,
        values: {
            accepted: true,
            command_id: `command-${connection}`,
            message: "accepted",
            result_json: JSON.stringify(options.resultForCommand?.(String(message.args?.operation ?? ""), payload) ?? {}),
            ...options.commandResponseValues?.(String(message.args?.operation ?? ""), payload),
          },
        }));
        if (options.refreshStatusOnCommand && status) {
          socket.send(JSON.stringify({
            op: "publish",
            topic: "/integration/debug/status",
            msg: { data: JSON.stringify(status) },
          }));
        }
      };
      if (options.commandResponseDelayMs) {
        setTimeout(sendResponse, options.commandResponseDelayMs);
      } else {
        sendResponse();
      }
    });
  });
  await page.goto(runtimeMode === "live" ? "/?workspace=debug" : "/");
  return {
    connectionCount: () => connectionCount,
    sockets,
    readOnlyServiceCalls,
    subscriptionsBySocket,
    subscriptionRequestsBySocket,
  };
}

const DEBUG_RAW_JPEG = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAAJABADASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAABgf/xAAbEAACAgMBAAAAAAAAAAAAAAAABQcjMkGhQv/EABUBAQEAAAAAAAAAAAAAAAAAAAYH/8QAHBEAAQMFAAAAAAAAAAAAAAAAAAEDBgQFISJR/9oADAMBAAIRAxEAPwCbJomxp4O00TY08HSbQ6T+RIlzf6TCOyOs1yf/2Q==";
const DEBUG_TRANSPARENT_WEBP = "UklGRhoAAABXRUJQVlA4TA0AAAAvAUAAEAcQERGIiP4HAA==";

function supportPlaneDiagnosticsFixture(valid: boolean): Record<string, unknown> {
  return {
    schema: "pnu.tool.support_plane_diagnostics.v1",
    validation_requested: true,
    artifact_loaded: true,
    static_reasons: [],
    calibration_fit: {
      available: true,
      inlier_ratio: 0.964,
      residual_p95_m: 0.004,
    },
    runtime_validation: {
      evaluated: true,
      metrics_available: true,
      valid,
      reasons: valid ? [] : ["support_plane_runtime_inlier_ratio_low"],
      sample_count: 12_480,
      inlier_ratio: valid ? 0.948 : 0.71,
      residual_median_m: valid ? 0.0014 : 0.0032,
      residual_p95_m: valid ? 0.0038 : 0.021,
      camera_info_sha256: "d".repeat(64),
    },
  };
}

function validBloodSemanticInstance(instanceId = 0): Record<string, unknown> {
  return {
    instance_id: instanceId,
    confidence: 0.87 - instanceId * 0.01,
    bbox_xyxy_px: [420 + instanceId * 10, 305, 468 + instanceId * 10, 356],
    centroid_xy_px: [444 + instanceId * 10, 330.5],
    centroid_depth_valid: true,
    centroid_depth_m: 0.641 + instanceId * 0.001,
  };
}

function publishDebugTopic(socket: WebSocketRoute, topic: string, msg: Record<string, unknown>) {
  socket.send(JSON.stringify({ op: "publish", topic, msg }));
}

function debugCompressedBytes(base64: string): number {
  return Math.floor(base64.length * 3 / 4)
    - (base64.endsWith("==") ? 2 : base64.endsWith("=") ? 1 : 0);
}

function finalOverlayLayer(
  state: "live" | "stale" | "missing" | "disabled",
  stamp: Record<string, number>,
  count: number,
): Record<string, unknown> {
  return {
    state,
    source_stamp: state === "live" || state === "stale" ? stamp : null,
    age_sec: state === "live" ? 0.02 : state === "stale" ? 1.4 : 0,
    count: state === "disabled" ? 0 : count,
    dropped: 0,
  };
}

function finalOverlayStatusFixture({
  sec = 1_900_000_000,
  nanosec = 123_456_789,
  cam4HandState = "live",
}: {
  sec?: number;
  nanosec?: number;
  cam4HandState?: "live" | "stale" | "missing" | "disabled";
} = {}): Record<string, unknown> {
  const stamp = { sec, nanosec };
  const base = {
    source_stamp: stamp,
    age_sec: 0.02,
    received: 42,
    dropped: 0,
  };
  return {
    schema: "pnu.perception.final_overlay.v1",
    published_at: stamp,
    output: {
      source_stamp: stamp,
      hz: 15,
      bytes: debugCompressedBytes(DEBUG_RAW_JPEG),
      width: 1920,
      height: 540,
    },
    cameras: {
      cam3: {
        state: "live",
        base: { ...base },
        layers: {
          tool: finalOverlayLayer("live", stamp, 1),
          pose: finalOverlayLayer("stale", stamp, 1),
          hand: finalOverlayLayer("disabled", stamp, 0),
          blood: finalOverlayLayer("disabled", stamp, 0),
        },
      },
      cam4: {
        state: "live",
        base: { ...base },
        layers: {
          tool: finalOverlayLayer("live", stamp, 1),
          pose: finalOverlayLayer("live", stamp, 1),
          hand: finalOverlayLayer(cam4HandState, stamp, 1),
          blood: finalOverlayLayer("live", stamp, 1),
        },
      },
    },
  };
}

function publishFinalOverlay(
  socket: WebSocketRoute,
  status = finalOverlayStatusFixture(),
) {
  const output = status.output as { source_stamp: { sec: number; nanosec: number } };
  publishDebugTopic(socket, "/perception/debug/final_overlay/compressed", {
    header: { stamp: output.source_stamp, frame_id: "perception_final_overlay" },
    format: "jpeg",
    data: DEBUG_RAW_JPEG,
  });
  publishDebugTopic(socket, "/perception/debug/final_overlay/status", {
    data: JSON.stringify(status),
  });
}

async function waitForPnuSubscriptions(bridge: Awaited<ReturnType<typeof openDebugWorkspace>>) {
  await expect.poll(() => {
    const socket = bridge.sockets[bridge.sockets.length - 1];
    const topics = socket ? bridge.subscriptionsBySocket.get(socket) : undefined;
    return [
      "/perception/debug/final_overlay/compressed",
      "/perception/debug/final_overlay/status",
      "/surgery/perception/cam4/tool_poses",
      "/surgery/perception/cam4/blood_semantics/json",
      "/surgery/perception/rfdetr/health",
      "/surgery/perception/rfdetr/diagnostics/json",
    ].every((topic) => topics?.has(topic));
  }).toBe(true);
}

function activeDebugSocket(bridge: Awaited<ReturnType<typeof openDebugWorkspace>>): WebSocketRoute {
  const socket = bridge.sockets[bridge.sockets.length - 1];
  if (!socket) throw new Error("active Debug WebSocket is unavailable");
  return socket;
}

function publishPnuEvidence(
  socket: WebSocketRoute,
  {
    sec = 1_900_000_000,
    nanosec = 123_456_789,
    tool = 1,
    blood = 1,
    overlaySec = sec,
    provider = "pnu_hand_blood",
    rawFrameId = "cam_4_color_optical_frame",
    diagnosticsFrameId = rawFrameId,
    overlayFrameId = rawFrameId,
    publishOverlay = true,
    publishSemanticEvidence = true,
    supportPlaneValidated = false,
    healthOverrides = {},
    diagnosticsOverrides = {},
    bloodMessageOverrides = {},
  }: {
    sec?: number;
    nanosec?: number;
    tool?: number;
    blood?: number;
    overlaySec?: number;
    provider?: string;
    rawFrameId?: string;
    diagnosticsFrameId?: string;
    overlayFrameId?: string;
    publishOverlay?: boolean;
    publishSemanticEvidence?: boolean;
    supportPlaneValidated?: boolean;
    healthOverrides?: Record<string, unknown>;
    diagnosticsOverrides?: Record<string, unknown>;
    bloodMessageOverrides?: Record<string, unknown>;
  } = {},
) {
  const total = tool + blood;
  publishDebugTopic(socket, "/surgery/perception/rfdetr/health", {
    data: JSON.stringify({
      schema: "taskplanner.rfdetr_health.v1",
      provider,
      enabled: true,
      connected: true,
      status: "ready",
      model_ready: true,
      semantic_ready: true,
      depth_aligned: true,
      metric_3d_ready: true,
      metric_3d_reasons: [],
      support_plane_validated: supportPlaneValidated,
      transport_mode: "http_local",
      auth_mode: "none_local",
      requested_algorithms: ["tool", "blood"],
      executed_algorithms: ["tool", "blood"],
      detection_count: total,
      empty_detection_result: total === 0,
      source_stamp_sec: sec,
      source_stamp_nanosec: nanosec,
      last_error_code: "",
      last_error_message: "",
      ...healthOverrides,
    }),
  });
  publishDebugTopic(socket, "/surgery/perception/rfdetr/diagnostics/json", {
    data: JSON.stringify({
      schema: "pnu.rfdetr_diagnostics.v2",
      provider,
      sequence: 42,
      frame_id: diagnosticsFrameId,
      source_stamp_sec: sec,
      source_stamp_nanosec: nanosec,
      requested_algorithms: ["tool", "blood"],
      executed_algorithms: ["tool", "blood"],
      model_version: "tool:v1,blood:v1",
      model_digests: {
        tool: "a".repeat(64),
        blood: "b".repeat(64),
      },
      tool_detection_count: tool,
      blood_detection_count: blood,
      instance_count: total,
      empty_detection_result: total === 0,
      metric_3d_ready: true,
      metric_3d_reasons: [],
      depth_aligned: true,
      depth_scale_validated: true,
      support_plane_validated: supportPlaneValidated,
      support_plane_diagnostics: supportPlaneDiagnosticsFixture(supportPlaneValidated),
      transport_mode: "http_local",
      auth_mode: "none_local",
      inference_latency_ms: 84.5,
      source_to_output_latency_ms: 121.8,
      queue_age_ms: 4.2,
      render_encode_latency_ms: 7.6,
      overlay_published: publishOverlay,
      overlay_status: publishOverlay ? "published" : "rate_limited",
      overlay_truncated: false,
      overlay_drawn_tool_count: publishOverlay ? tool : 0,
      overlay_drawn_blood_count: publishOverlay ? blood : 0,
      error_code: "",
      error_message: "",
      ...diagnosticsOverrides,
    }),
  });
  if (publishSemanticEvidence) {
    const sourceStampNs = BigInt(sec) * 1_000_000_000n + BigInt(nanosec);
    publishDebugTopic(socket, "/surgery/perception/cam4/blood_semantics/json", {
      data: JSON.stringify({
        schema: "taskplanner.cam4_blood_semantics.v1",
        source: "cam4_pnu_blood",
        provider: "pnu_hand_blood",
        source_stamp_sec: Number((sec + nanosec / 1_000_000_000).toFixed(6)),
        source_stamp_ns: sourceStampNs.toString(),
        frame_id: rawFrameId,
        ground_truth: false,
        metric_3d_ready: true,
        detections: Array.from(
          { length: blood },
          (_, index) => validBloodSemanticInstance(index),
        ),
        combined_centroid_xy_px: blood > 0 ? [444, 330.5] : null,
        combined_centroid_depth_valid: blood > 0,
        combined_centroid_depth_m: blood > 0 ? 0.641 : null,
        ...bloodMessageOverrides,
      }),
    });
  }
  // `publishOverlay`, `overlaySec`, and `overlayFrameId` remain fixture
  // inputs for diagnostics compatibility. The browser intentionally receives
  // no per-layer PNU image stream; final raster is exercised separately.
}

function validPlanarToolPose(instanceId = 4): Record<string, unknown> {
  return {
    frame_local_instance_id: instanceId,
    canonical_class_id: 1,
    model_class_index: 1,
    class_name: "Allis Forceps",
    class_confidence: 0.91,
    pose: {
      position: { x: 0.041, y: -0.027, z: 0.806 },
      orientation: { x: 0, y: 0, z: 0.3826834, w: 0.9238795 },
    },
    pose_mode: 2,
    position_valid: true,
    orientation_valid: true,
    dof_observed: [true, true, true, false, false, true],
    observation_point_definition: "mask_internal_depth_valid_observed_surface_point_v1",
    observation_point_uv_px: [632.5, 351.25],
    observation_point_inside_mask: true,
    observation_point_depth_valid: true,
    observation_point_selection_mode: "longitudinal_axis_midpoint",
    observation_point_boundary_clearance_px: 12.5,
    axis_definition: "+Y handle to tip; +Z support plane to free space; +X=+Yx+Z",
    symmetry_type: "axial_180",
    endpoint_sign_confidence: 0.83,
    valid_depth_ratio: 0.94,
    pose_point_count: 420,
    axis_anisotropy: 4.25,
    support_plane_inlier_ratio: 0.97,
    support_plane_residual_p95_m: 0.0018,
    pose_confidence: 0.86,
    pose_confidence_calibrated: true,
    validity: 1,
    status_flags: ["SUPPORT_PLANE_VALIDATED", "PLANAR_POSE"],
    invalid_reason: "",
  };
}

function positionOnlyToolPose(instanceId = 4): Record<string, unknown> {
  return {
    ...validPlanarToolPose(instanceId),
    pose: {
      position: { x: 0.041, y: -0.027, z: 0.806 },
      orientation: { x: 0, y: 0, z: 0, w: 0 },
    },
    orientation_valid: false,
    dof_observed: [true, true, true, false, false, false],
    pose_confidence: 0,
    pose_confidence_calibrated: false,
    validity: 2,
    status_flags: ["SUPPORT_PLANE_UNVALIDATED"],
    invalid_reason: "SUPPORT_PLANE_UNVALIDATED",
  };
}

function poseDiagnosticsOverrides(
  tools: Record<string, unknown>[],
  published = true,
): Record<string, unknown> {
  const axisCount = tools.filter((tool) => tool.orientation_valid === true).length;
  const positionOnlyCount = tools.filter(
    (tool) => tool.position_valid === true && tool.orientation_valid === false,
  ).length;
  return {
    pose_overlay_published: published,
    pose_overlay_status: published ? "published" : "rate_limited",
    pose_overlay_drawn_axis_count: published ? axisCount : 0,
    pose_overlay_drawn_position_only_count: published ? positionOnlyCount : 0,
    pose_overlay_render_encode_latency_ms: published ? 3.4 : 0,
    pose_overlay_truncated: false,
  };
}

function publishToolPoseEvidence(
  socket: WebSocketRoute,
  {
    sec = 1_900_000_000,
    nanosec = 123_456_789,
    frameId = "cam_4_color_optical_frame",
    overlaySec = sec,
    overlayFrameId = frameId,
    publishOverlay = true,
    tools = [validPlanarToolPose()],
  }: {
    sec?: number;
    nanosec?: number;
    frameId?: string;
    overlaySec?: number;
    overlayFrameId?: string;
    publishOverlay?: boolean;
    tools?: Record<string, unknown>[];
  } = {},
) {
  publishDebugTopic(socket, "/surgery/perception/cam4/tool_poses", {
    header: { stamp: { sec, nanosec }, frame_id: frameId },
    sequence: 42,
    schema_version: "pnu.surgical_tool_pose_array.v1.3",
    observation_id: `cam4:${BigInt(sec) * 1_000_000_000n + BigInt(nanosec)}`,
    source_view: "cam4",
    model_version: "cam4-rfdetr-seg-tool-v1",
    ontology_version: "pnu-tool-ontology-v1",
    calibration_version: "viplab-cam4-align-depth-to-color-v1",
    pose_convention_version: "pnu-planar-pose-v1",
    tools,
  });
  // Pose pixels are included only in the server-composited final raster.
}

test("embeds the multicam observer in Debug on the existing read-only ROS session", async ({ page }) => {
  let standaloneMulticamConnections = 0;
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9091\/multicam\/?$/, (socket) => {
    standaloneMulticamConnections += 1;
    void socket.close();
  });
  const bridge = await openDebugWorkspace(page);
  await expect(page.getByRole("heading", { name: "디버그 모드" })).toBeVisible();
  const debugConnectionsBeforeMulticam = bridge.connectionCount();

  await page.getByRole("tab", { name: /^멀티캠 관제/ }).click();
  const panel = page.locator('[data-slot="debug-multicam-ops"]');
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("DEBUG · MULTICAM OBSERVER");
  await expect(panel).toContainText("멀티캠 observer ready · CaptureStatus fresh");
  await expect(panel.getByText("Graph topic 발견")).toBeVisible();
  await expect(page.locator("main main")).toHaveCount(0);
  const socket = activeDebugSocket(bridge);
  await expect.poll(() => [...(bridge.subscriptionsBySocket.get(socket) ?? [])].sort()).toEqual(expect.arrayContaining([
    "/multicam_node/capture_status",
    "/world_anchor_node/status",
    "/synced/cam_1/color/image_raw/compressed",
  ]));
  expect(bridge.subscriptionRequestsBySocket.get(socket)?.get("/synced/cam_1/color/image_raw/compressed")).toMatchObject({
    throttle_rate: 0,
    qos: { reliability: "best_effort", durability: "volatile", depth: 1 },
  });
  expect(bridge.connectionCount()).toBe(debugConnectionsBeforeMulticam);
  expect(standaloneMulticamConnections).toBe(0);
  await expect(panel.getByText(/^ws:\/\/127\.0\.0\.1:9091$/)).toBeVisible();
  await expect(panel.locator(".ops-tf-card")).toHaveCount(0);
  await panel.getByRole("tab", { name: "Depth" }).click();
  await expect.poll(() => [...(bridge.subscriptionsBySocket.get(socket) ?? [])]).toContain(
    "/synced/cam_4/depth/image_rect_raw/compressedDepth",
  );
  for (const buttonName of ["샘플 수집 시작", "수집 중지", "Solve · 저장 · TF 발행", "저장된 Anchor 다시 발행"]) {
    await expect(panel.getByRole("button", { name: buttonName })).toBeDisabled();
  }
  await expect.poll(() => bridge.readOnlyServiceCalls).toEqual(["/rosapi/topics"]);

  await page.getByRole("tab", { name: /^관측 로그/ }).click();
  await expect(panel).toHaveCount(0);
  await expect.poll(() => [...(bridge.subscriptionsBySocket.get(socket) ?? [])]).not.toContain(
    "/multicam_node/capture_status",
  );
  expect(standaloneMulticamConnections).toBe(0);
  expect(bridge.readOnlyServiceCalls).toEqual(["/rosapi/topics"]);
});

test("opens a read-only TF tab with separately bounded static and dynamic transforms", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: "TF 좌표계·3D 모델" }).click();
  const panel = page.locator('[data-slot="debug-tf-panel"]');

  await expect(panel).toBeVisible();
  const socket = activeDebugSocket(bridge);
  await expect.poll(() => [...(bridge.subscriptionsBySocket.get(activeDebugSocket(bridge)) ?? [])].sort()).toEqual(expect.arrayContaining([
    "/integration/debug/status",
    "/tf",
    "/tf_static",
  ]));
  const requests = bridge.subscriptionRequestsBySocket.get(socket);
  await expect.poll(() => requests?.get("/tf_static")?.qos).toMatchObject({
    reliability: "reliable",
    durability: "transient_local",
    depth: 32,
  });
  await expect.poll(() => requests?.get("/tf")?.qos).toMatchObject({
    reliability: "best_effort",
    durability: "volatile",
    depth: 10,
  });
  await expect(panel.locator('[data-slot="debug-tf-static-stream"]')).toHaveAttribute("data-state", "LIVE");
  await expect(panel.locator('[data-slot="debug-tf-dynamic-stream"]')).toHaveAttribute("data-state", "LIVE");
  await expect(panel.locator('[data-slot="debug-tf-static-list"]')).toContainText("cam_4_color_optical_frame");
  await expect(panel.locator('[data-slot="debug-tf-static-list"]')).toContainText("tag1");
  await expect(panel.locator('[data-slot="debug-tf-dynamic-list"]')).toContainText("cam_4_bovie_0");
  await expect(panel.getByText(/robot\/world 정합을 주장하지 않습니다/)).toBeVisible();

  // Dynamic transforms are an observation cache, unlike retained static
  // calibration. They must disappear from the scene/list after three seconds
  // of silence rather than implying the tool is still present.
  await page.waitForTimeout(3_200);
  await expect(panel.locator('[data-slot="debug-tf-dynamic-stream"]')).toHaveAttribute("data-state", "STALE");
  await expect(panel.locator('[data-slot="debug-tf-dynamic-stream"]')).toContainText("FRAMES0");
  await expect(panel.locator('[data-slot="debug-tf-dynamic-list"]')).not.toContainText("cam_4_bovie_0");

  await page.getByRole("tab", { name: /^관측 로그/ }).click();
  await expect(panel).toHaveCount(0);
  await expect.poll(() => [...(bridge.subscriptionsBySocket.get(socket) ?? [])]).not.toContain("/tf");
  await expect.poll(() => [...(bridge.subscriptionsBySocket.get(socket) ?? [])]).not.toContain("/tf_static");
});

test("uses one final 2-up raster while retaining scalar PNU evidence", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await expect(panel).toBeVisible();
  await waitForPnuSubscriptions(bridge);
  const subscriptionRequests = bridge.subscriptionRequestsBySocket.get(activeDebugSocket(bridge));
  expect(subscriptionRequests?.get("/surgery/perception/cam4/tool_poses")).not.toHaveProperty(
    "compression",
  );
  expect(
    subscriptionRequests?.get("/surgery/perception/cam4/blood_semantics/json"),
  ).not.toHaveProperty("compression");
  expect(subscriptionRequests?.get("/perception/debug/final_overlay/compressed")).toMatchObject({
    compression: "cbor",
    throttle_rate: 180,
    qos: { reliability: "best_effort", depth: 1 },
  });
  expect(subscriptionRequests?.get("/perception/debug/final_overlay/status")).toMatchObject({
    qos: { reliability: "reliable", depth: 1 },
  });
  expect(subscriptionRequests?.get("/surgery/perception/rfdetr/diagnostics/json")).toMatchObject({
    qos: { reliability: "best_effort", durability: "volatile", depth: 1 },
  });
  for (const retiredImageTopic of [
    "/synced/cam_4/color/image_raw/compressed",
    "/surgery/images/cam4/detection_overlay/compressed",
    "/surgery/images/cam4/pose_overlay/compressed",
  ]) expect(subscriptionRequests?.has(retiredImageTopic)).toBe(false);
  publishFinalOverlay(activeDebugSocket(bridge));
  publishPnuEvidence(activeDebugSocket(bridge));

  await expect(panel.locator('[data-slot="debug-direct-perception-final-overlay"]')).toBeVisible();
  await expect.poll(() => panel.locator('[data-slot="debug-direct-perception-final-overlay"]').evaluate(
    (image: HTMLImageElement) => image.naturalWidth,
  )).toBeGreaterThan(0);
  const finalViewportRatio = await panel.locator('[data-slot="debug-direct-perception-final-viewport"]').evaluate((viewport) => {
    const bounds = viewport.getBoundingClientRect();
    return bounds.width / bounds.height;
  });
  expect(finalViewportRatio).toBeGreaterThan(3.4);
  expect(finalViewportRatio).toBeLessThan(3.7);
  await expect(panel.locator('[data-slot="debug-direct-perception-final-viewport"]')).toHaveAttribute(
    "data-source-stamp",
    "1900000000:123456789",
  );
  const lowRateStatus = finalOverlayStatusFixture({ sec: 1_900_000_001 });
  publishDebugTopic(activeDebugSocket(bridge), "/perception/debug/final_overlay/status", {
    data: JSON.stringify(lowRateStatus),
  });
  await expect(panel.locator('[data-slot="debug-direct-perception-final-overlay"]')).toBeVisible();
  await expect(panel.locator('[data-slot="debug-direct-perception-final-status"]')).not.toContainText("상태 계약 오류");
  await expect(panel.locator('[data-slot="debug-direct-perception-snapshot-relation"]')).toContainText("STATUS SNAPSHOT · LOW-RATE");
  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText("Scalar 실행 증거 검증 가능");
  await expect(panel.locator('[data-slot="debug-perception-image-consumer-disabled"]')).toContainText("상단 final raster 한 장");
  await expect(panel).toContainText("PNU tool/blood worker");
  await expect(panel).toContainText("TOOL · BLOOD");
  await expect(panel).toContainText("Metric 3D");
  await expect(panel).toContainText("VALIDATED");
  await expect(panel).toContainText("Support plane");
  await expect(panel).toContainText("Tool orientation / 6D는 DEGRADED");
  await expect(panel).toContainText("84.5 ms");
  await expect(panel).toContainText("121.8 ms");
  await expect(panel.locator(".debug-perception-kpis")).toContainText("2");
  await expect(panel.locator('[data-slot="debug-perception-overlay-status"]')).toContainText(
    "Server final overlay PUBLISHED · Drawn T 1 / B 1",
  );
});

test("keeps a final raster visible when one server layer is missing and reports malformed status", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  const direct = panel.locator('[data-slot="debug-direct-perception-overlay-panel"]');
  await expect(direct).toBeVisible();

  const socket = activeDebugSocket(bridge);
  const missingLayer = finalOverlayStatusFixture({ cam4HandState: "missing" });
  publishFinalOverlay(socket, missingLayer);
  await expect(direct.locator('[data-slot="debug-direct-perception-final-overlay"]')).toBeVisible();
  await expect(direct.locator('[data-slot="debug-direct-perception-final-viewport"] img')).toHaveCount(1);
  await expect(direct.locator('[data-slot="debug-direct-perception-status-cam4"]')).toContainText(/Hand\s*ACTIVE · MISSING/);
  await expect(direct.locator('[data-slot="debug-direct-perception-status-cam3"]')).toContainText(/Hand\s*DISABLED/);
  await expect(direct.getByRole("button")).toHaveCount(0);

  const startupStatus = finalOverlayStatusFixture();
  const output = startupStatus.output as Record<string, unknown>;
  output.source_stamp = null;
  output.bytes = 0;
  output.width = 0;
  output.height = 0;
  const cam3 = (startupStatus.cameras as Record<string, Record<string, unknown>>).cam3;
  cam3.state = "missing";
  const cam3Base = cam3.base as Record<string, unknown>;
  cam3Base.source_stamp = null;
  cam3Base.age_sec = null;
  const cam3Layers = cam3.layers as Record<string, Record<string, unknown>>;
  for (const layer of Object.values(cam3Layers)) {
    layer.source_stamp = null;
    layer.age_sec = null;
    layer.count = 0;
  }
  cam3Layers.tool.state = "missing";
  cam3Layers.pose.state = "missing";
  publishDebugTopic(socket, "/perception/debug/final_overlay/status", {
    data: JSON.stringify(startupStatus),
  });
  await expect(direct.locator('[data-slot="debug-direct-perception-final-overlay"]')).toBeVisible();
  await expect(direct.locator('[data-slot="debug-direct-perception-final-status"]')).not.toContainText("상태 계약 오류");
  await expect(direct.locator('[data-slot="debug-direct-perception-status-cam3"]')).toContainText("ACTIVE · MISSING");

  // A malformed non-null layer stamp is not an alias for the permitted null
  // never-seen value. Keep the raster but fail the compact status contract.
  const invalidNonNullLayerStamp = finalOverlayStatusFixture({ cam4HandState: "missing" });
  const invalidCam4 = (invalidNonNullLayerStamp.cameras as Record<string, Record<string, unknown>>).cam4;
  const invalidCam4Layers = invalidCam4.layers as Record<string, Record<string, unknown>>;
  invalidCam4Layers.hand.source_stamp = { sec: "not-an-integer", nanosec: 0 };
  publishDebugTopic(socket, "/perception/debug/final_overlay/status", {
    data: JSON.stringify(invalidNonNullLayerStamp),
  });
  await expect(direct.locator('[data-slot="debug-direct-perception-final-overlay"]')).toBeVisible();
  await expect(direct.locator('[data-slot="debug-direct-perception-final-status"]')).toContainText("상태 계약 오류");

  publishDebugTopic(socket, "/perception/debug/final_overlay/status", {
    data: JSON.stringify({ schema: "unknown" }),
  });
  await expect(direct.locator('[data-slot="debug-direct-perception-final-overlay"]')).toBeVisible();
  await expect(direct.locator('[data-slot="debug-direct-perception-final-status"]')).toContainText("상태 계약 오류");
});

test("shows exact-stamp Blood centroid evidence as monitor-only", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge));

  const bloodCard = panel.locator('[data-slot="debug-blood-evidence"]');
  await expect(bloodCard.locator('[data-slot="debug-blood-state"]')).toContainText("Blood 1건 검토 가능");
  await expect(bloodCard.locator('[data-slot="debug-blood-combined"]')).toContainText("444.0, 330.5");
  await expect(bloodCard.locator('[data-slot="debug-blood-combined"]')).toContainText("0.6410");
  await expect(bloodCard.locator('[data-slot="debug-blood-combined"]')).toContainText("1900000000123456789");
  await expect(bloodCard.locator('[data-slot="debug-blood-list"] > li')).toHaveCount(1);
  await expect(bloodCard.locator('[data-slot="debug-blood-list"] > li')).toContainText("87.0%");
  await expect(bloodCard.locator('[data-slot="debug-blood-list"] > li')).toContainText("444.0, 330.5");
  await expect(bloodCard).toContainText("CAM4 optical frame의 monitor-only 관측값");
  await expect(bloodCard).toContainText("Robot/world/TCP pose나 흡인 목표·실행 권한이 아닙니다");

  await page.setViewportSize({ width: 320, height: 800 });
  const compactGeometry = await panel.evaluate((element) => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    overflowing: [...element.querySelectorAll<HTMLElement>("*")].filter((child) => {
      if (child.closest('[data-slot="debug-direct-perception-overlay-panel"]')) return false;
      const style = getComputedStyle(child);
      const bounds = child.getBoundingClientRect();
      return style.display !== "none"
        && style.visibility !== "hidden"
        && style.display !== "inline"
        && bounds.width > 0
        && (bounds.left < -1 || bounds.right > document.documentElement.clientWidth + 1);
    }).map((child) => `${child.tagName}.${child.className}`),
  }));
  expect(compactGeometry.overflowing).toEqual([]);
});

test("buffers next-frame Blood results until their exact diagnostics arrive", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  const socket = activeDebugSocket(bridge);
  publishPnuEvidence(socket);
  await expect(panel.locator('[data-slot="debug-blood-state"]')).toContainText("Blood 1건 검토 가능");

  const nextSec = 1_900_000_001;
  const nextNanosec = 223_456_789;
  publishDebugTopic(socket, "/surgery/perception/cam4/blood_semantics/json", {
    data: JSON.stringify({
      schema: "taskplanner.cam4_blood_semantics.v1",
      source: "cam4_pnu_blood",
      provider: "pnu_hand_blood",
      source_stamp_sec: Number((nextSec + nextNanosec / 1_000_000_000).toFixed(6)),
      source_stamp_ns: (
        BigInt(nextSec) * 1_000_000_000n + BigInt(nextNanosec)
      ).toString(),
      frame_id: "cam_4_color_optical_frame",
      ground_truth: false,
      metric_3d_ready: true,
      detections: [validBloodSemanticInstance()],
      combined_centroid_xy_px: [444, 330.5],
      combined_centroid_depth_valid: true,
      combined_centroid_depth_m: 0.641,
    }),
  });

  // ROSBridge may deliver the next semantic results while the prior overlay is
  // still current. They must remain buffered instead of being compared to and
  // discarded against that older frame.
  await expect(panel.locator('[data-slot="debug-blood-state"]')).toContainText("Blood 1건 검토 가능");

  publishPnuEvidence(socket, {
    sec: nextSec,
    nanosec: nextNanosec,
    publishSemanticEvidence: false,
  });
  await expect(panel.locator('[data-slot="debug-blood-state"]')).toContainText("Blood 1건 검토 가능");
  await expect(panel.locator('[data-slot="debug-blood-evidence"]')).toContainText(
    "동일 stamp의 Blood 1건",
  );
});

test("shows an exact-stamp executed zero Blood result as a normal empty state", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), { tool: 0, blood: 0 });

  await expect(panel.locator('[data-slot="debug-blood-state"]')).toContainText("실행 완료 · Blood 0건");
  await expect(panel.locator('[data-slot="debug-blood-empty"]')).toContainText("정상 empty result");
  await expect(panel.locator('[data-slot="debug-blood-list"]')).toHaveCount(0);
});

test("fails closed on a malformed lossless Blood semantic payload", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), {
    bloodMessageOverrides: { source_stamp_ns: "not-a-decimal-stamp" },
  });

  await expect(panel.locator('[data-slot="debug-blood-state"]')).toContainText("Blood 증거 계약 불일치");
  await expect(panel.locator('[data-slot="debug-blood-evidence"]')).toContainText("lossless source_stamp_ns");
  await expect(panel.locator('[data-slot="debug-blood-list"]')).toHaveCount(0);
});

test("does not display Blood payloads without a matching diagnostics source stamp", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), {
    bloodMessageOverrides: { source_stamp_ns: "1900000000123456788" },
  });

  await expect(panel.locator('[data-slot="debug-blood-state"]')).toContainText("Blood semantics 대기");
  await expect(panel.locator('[data-slot="debug-blood-evidence"]')).toContainText("stamp별로 버퍼링");
  await expect(panel.locator('[data-slot="debug-blood-list"]')).toHaveCount(0);
});

test("shows a live-valid support-plane audit and local transport claims with zero Tool detections", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), {
    tool: 0,
    blood: 0,
    supportPlaneValidated: true,
  });

  const audit = panel.locator('[data-slot="debug-support-plane-audit"]');
  await expect(audit).toBeVisible();
  await expect(audit).toHaveAttribute("data-live-valid", "true");
  await expect(audit.locator('[data-slot="debug-support-plane-state"]')).toContainText(
    "Live drift gate 유효",
  );
  await expect(audit.locator(".debug-support-plane-gates")).toContainText(
    /VALIDATION REQUESTED\s*YES/,
  );
  await expect(audit.locator(".debug-support-plane-gates")).toContainText(/ARTIFACT LOADED\s*YES/);
  await expect(audit.locator(".debug-support-plane-gates")).toContainText(/LIVE EVALUATED\s*YES/);
  await expect(audit.locator(".debug-support-plane-gates")).toContainText(/LIVE DRIFT GATE\s*VALID/);
  await expect(audit.locator('[data-slot="debug-support-plane-calibration"]')).toContainText("96.4%");
  await expect(audit.locator('[data-slot="debug-support-plane-calibration"]')).toContainText("4.00 mm");
  await expect(audit.locator('[data-slot="debug-support-plane-runtime"]')).toContainText("12,480");
  await expect(audit.locator('[data-slot="debug-support-plane-runtime"]')).toContainText("94.8%");
  await expect(audit.locator('[data-slot="debug-support-plane-runtime"]')).toContainText("1.40 mm");
  await expect(audit.locator('[data-slot="debug-support-plane-runtime"]')).toContainText("3.80 mm");
  await expect(audit.locator('[data-slot="debug-support-plane-static-reasons"]')).toHaveText("없음");
  await expect(audit.locator('[data-slot="debug-support-plane-runtime-reasons"]')).toHaveText("없음");
  await expect(panel.locator('[data-slot="debug-perception-transport"]')).toContainText(
    "LOCAL HTTP",
  );
  await expect(panel.locator('[data-slot="debug-perception-transport"]')).toContainText(
    "health/diagnostics 일치",
  );
  await expect(panel.locator('[data-slot="debug-perception-auth"]')).toContainText(
    "LOCAL · NO TOKEN",
  );
  await expect(panel.locator('[data-slot="debug-perception-executed-zero"]')).toContainText(
    "모델은 실행됐습니다",
  );
});

test("separates calibration fit from a failed live drift gate and keeps reasons visible with Tool 0", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), { tool: 0, supportPlaneValidated: false });

  const audit = panel.locator('[data-slot="debug-support-plane-audit"]');
  await expect(audit).toHaveAttribute("data-live-valid", "false");
  await expect(audit.locator('[data-slot="debug-support-plane-state"]')).toContainText(
    "Live drift gate 무효",
  );
  await expect(audit.locator(".debug-support-plane-gates")).toContainText(/ARTIFACT LOADED\s*YES/);
  await expect(audit.locator(".debug-support-plane-gates")).toContainText(/LIVE EVALUATED\s*YES/);
  await expect(audit.locator(".debug-support-plane-gates")).toContainText(/LIVE DRIFT GATE\s*INVALID/);
  const calibration = audit.locator('[data-slot="debug-support-plane-calibration"]');
  const runtime = audit.locator('[data-slot="debug-support-plane-runtime"]');
  await expect(calibration).toContainText("96.4%");
  await expect(calibration).toContainText("4.00 mm");
  await expect(runtime).toContainText("71.0%");
  await expect(runtime).toContainText("3.20 mm");
  await expect(runtime).toContainText("21.00 mm");
  await expect(audit.locator('[data-slot="debug-support-plane-runtime-reasons"]')).toContainText(
    "support_plane_runtime_inlier_ratio_low",
  );
  await expect(panel.locator(".debug-perception-kpis")).toContainText(/Tool\s*0/);
  await expect(panel.locator(".debug-perception-metric-card").filter({ hasText: "METRIC 3D" })).toContainText(
    "Tool orientation / 6D는 DEGRADED",
  );
});

test("shows a missing support-plane artifact separately from a not-evaluated runtime gate", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), {
    tool: 0,
    supportPlaneValidated: false,
    diagnosticsOverrides: {
      support_plane_diagnostics: {
        schema: "pnu.tool.support_plane_diagnostics.v1",
        validation_requested: true,
        artifact_loaded: false,
        static_reasons: ["support_plane_artifact_unavailable"],
        calibration_fit: {
          available: false,
          inlier_ratio: null,
          residual_p95_m: null,
        },
        runtime_validation: {
          evaluated: false,
          metrics_available: false,
          valid: false,
          reasons: ["support_plane_runtime_not_evaluated"],
          sample_count: 0,
          inlier_ratio: null,
          residual_median_m: null,
          residual_p95_m: null,
          camera_info_sha256: "",
        },
      },
    },
  });

  const audit = panel.locator('[data-slot="debug-support-plane-audit"]');
  await expect(audit.locator('[data-slot="debug-support-plane-state"]')).toContainText("Artifact 없음");
  await expect(audit.locator(".debug-support-plane-gates")).toContainText(/ARTIFACT LOADED\s*NO/);
  await expect(audit.locator(".debug-support-plane-gates")).toContainText(/LIVE EVALUATED\s*NO/);
  await expect(audit.locator('[data-slot="debug-support-plane-calibration"]')).toContainText(
    /FIT AVAILABLE\s*NO/,
  );
  await expect(audit.locator('[data-slot="debug-support-plane-static-reasons"]')).toContainText(
    "support_plane_artifact_unavailable",
  );
  await expect(audit.locator('[data-slot="debug-support-plane-runtime-reasons"]')).toContainText(
    "support_plane_runtime_not_evaluated",
  );
});

test("fails closed and clears support-plane metrics on malformed or oversized runtime evidence", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), { supportPlaneValidated: true });
  await expect(panel.locator('[data-slot="debug-support-plane-runtime"]')).toContainText("12,480");

  const malformed = supportPlaneDiagnosticsFixture(false);
  malformed.runtime_validation = {
    ...(malformed.runtime_validation as Record<string, unknown>),
    sample_count: "12480",
    reasons: Array.from({ length: 17 }, (_, index) => `support_plane_reason_${index}`),
  };
  publishPnuEvidence(activeDebugSocket(bridge), {
    sec: 1_900_000_001,
    publishOverlay: false,
    supportPlaneValidated: false,
    diagnosticsOverrides: { support_plane_diagnostics: malformed },
  });

  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "인식 계약 불일치",
  );
  await expect(panel.locator('[data-slot="debug-support-plane-state"]')).toContainText("진단 대기");
  await expect(panel.locator('[data-slot="debug-support-plane-runtime"]')).not.toContainText("12,480");
  await expect(panel.locator('[data-slot="debug-support-plane-runtime-reasons"]')).toHaveText("수신 전");
});

test("fails closed when health and diagnostics transport claims disagree", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), {
    publishOverlay: false,
    healthOverrides: {
      transport_mode: "https",
      auth_mode: "bearer",
    },
  });

  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "인식 계약 불일치",
  );
  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "전송 보안 계약이 일치하지 않습니다",
  );
  await expect(panel.locator('[data-slot="debug-perception-transport"]')).toContainText("대기");
  await expect(panel.locator('[data-slot="debug-support-plane-runtime"]')).not.toContainText("12,480");
});

test("labels remote HTTPS and trusted-LAN development transport and auth modes separately", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), {
    healthOverrides: { transport_mode: "https", auth_mode: "bearer" },
    diagnosticsOverrides: { transport_mode: "https", auth_mode: "bearer" },
  });

  await expect(panel.locator('[data-slot="debug-perception-transport"]')).toContainText("HTTPS · TLS");
  await expect(panel.locator('[data-slot="debug-perception-auth"]')).toContainText("BEARER TOKEN");

  publishPnuEvidence(activeDebugSocket(bridge), {
    sec: 1_900_000_001,
    healthOverrides: {
      transport_mode: "http_trusted_lan_dev",
      auth_mode: "none_trusted_lan_dev",
    },
    diagnosticsOverrides: {
      transport_mode: "http_trusted_lan_dev",
      auth_mode: "none_trusted_lan_dev",
    },
  });

  const transport = panel.locator('[data-slot="debug-perception-transport"]');
  const auth = panel.locator('[data-slot="debug-perception-auth"]');
  await expect(transport).toContainText("TRUSTED LAN HTTP");
  await expect(transport).toContainText("신뢰 LAN 개발 모드 · 평문");
  await expect(transport.locator("dd").first()).toHaveClass(/waiting/);
  await expect(auth).toContainText("LAN · NO TOKEN");
  await expect(auth).toContainText("신뢰 LAN 개발 모드 · 무인증");
  await expect(auth.locator("dd").first()).toHaveClass(/waiting/);
});

test("correlates typed ToolPoseArray with scalar diagnostics while exposing bounded pose evidence", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  const tools = [validPlanarToolPose()];
  publishPnuEvidence(activeDebugSocket(bridge), {
    supportPlaneValidated: true,
    diagnosticsOverrides: poseDiagnosticsOverrides(tools),
  });
  publishToolPoseEvidence(activeDebugSocket(bridge), { tools });

  await expect(panel.locator('[data-slot="debug-tool-pose-readiness"]')).toContainText("자세 수치 1건 검토 가능");
  const card = panel.locator('[data-slot="debug-tool-pose-card"]');
  await expect(card).toHaveCount(1);
  await expect(card).toHaveAttribute("data-pose-readiness", "ready");
  await expect(card).toContainText("Allis Forceps");
  await expect(card).toContainText("평면 제약 4DoF");
  await expect(card).toContainText("X +0.041 · Y -0.027 · Z +0.806");
  await expect(card).toContainText("+0.0000 · +0.0000 · +0.3827 · +0.9239");
  await expect(card).toContainText("PLANAR_4DOF_WITH_NORMAL_PRIOR");
  await expect(card).toContainText("VALID");
  await expect(card).toContainText("X 관측");
  await expect(card).toContainText("ROLL 미관측");
  await expect(card).toContainText("PLANE RESIDUAL P95");
  await expect(card).toContainText("1.80 mm");
  await expect(card).toContainText("SUPPORT_PLANE_VALIDATED");
  await expect(panel.locator('[data-slot="debug-pose-overlay-status"]')).toContainText(
    "Server final overlay pose layer PUBLISHED · Axes 1 · Position-only 0",
  );
  await expect(panel.locator('[data-slot="debug-detection-layer-toggle"]')).toHaveCount(0);
  await expect(panel.locator('[data-slot="debug-pose-layer-toggle"]')).toHaveCount(0);

  await page.setViewportSize({ width: 320, height: 844 });
  const compactGeometry = await panel.evaluate((root) => {
    const clientWidth = document.documentElement.clientWidth;
    const overflowing = [...root.querySelectorAll<HTMLElement>("*")].filter((element) => {
      if (element.closest('[data-slot="debug-direct-perception-overlay-panel"]')) return false;
      const bounds = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return style.display !== "none"
        && style.visibility !== "hidden"
        && style.display !== "inline"
        && bounds.width > 0
        && (bounds.left < -1 || bounds.right > clientWidth + 1);
    }).map((element) => ({ tag: element.tagName, className: element.className }));
    return { clientWidth, scrollWidth: document.documentElement.scrollWidth, overflowing };
  });
  expect(compactGeometry.overflowing).toEqual([]);
});

test("labels support-plane-unvalidated Tool poses as position-only and never orientation-ready", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  const tools = [positionOnlyToolPose()];
  publishPnuEvidence(activeDebugSocket(bridge), {
    supportPlaneValidated: false,
    diagnosticsOverrides: poseDiagnosticsOverrides(tools),
  });
  publishToolPoseEvidence(activeDebugSocket(bridge), { tools });

  await expect(panel.locator('[data-slot="debug-tool-pose-readiness"]')).toContainText("위치만 1건");
  const card = panel.locator('[data-slot="debug-tool-pose-card"]');
  await expect(card).toHaveAttribute("data-pose-readiness", "position-only");
  await expect(card).toContainText("XYZ만 유효 · quaternion과 자세 축 사용 금지");
  await expect(card).toContainText("미검증 · 표시/사용 금지");
  await expect(card.locator(".debug-tool-pose-facts")).toContainText(/ORIENTATION\s*INVALID/);
  await expect(card).toContainText("DEGRADED");
  await expect(card).toContainText("SUPPORT_PLANE_UNVALIDATED");
  await expect(panel.locator('[data-slot="debug-pose-overlay-status"]')).toContainText(
    "Axes 0 · Position-only 1",
  );
  await expect(panel.locator('[data-slot="debug-tool-pose-readiness"]')).not.toContainText("자세 축");
});

test("keeps exact typed pose values reviewable while the server reports a rate-limited pose layer", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  const tools = [validPlanarToolPose()];
  publishPnuEvidence(activeDebugSocket(bridge), {
    supportPlaneValidated: true,
    diagnosticsOverrides: poseDiagnosticsOverrides(tools, false),
  });
  publishToolPoseEvidence(activeDebugSocket(bridge), { publishOverlay: false, tools });

  await expect(panel.locator('[data-slot="debug-tool-pose-card"]')).toHaveCount(1);
  await expect(panel.locator('[data-slot="debug-tool-pose-readiness"]')).toContainText(
    "자세 수치 1건 검토 가능",
  );
  await expect(panel.locator('[data-slot="debug-pose-overlay-status"]')).toContainText(
    "Server final overlay pose layer RATE_LIMITED · 새 자세 오버레이 미발행",
  );
});

test("shows a typed zero-Tool result as a normal empty pose frame", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  const tools: Record<string, unknown>[] = [];
  publishPnuEvidence(activeDebugSocket(bridge), {
    tool: 0,
    blood: 0,
    supportPlaneValidated: true,
    diagnosticsOverrides: poseDiagnosticsOverrides(tools),
  });
  publishToolPoseEvidence(activeDebugSocket(bridge), { tools });

  await expect(panel.locator('[data-slot="debug-tool-pose-empty"]')).toContainText("정상 empty result");
  await expect(panel.locator('[data-slot="debug-tool-pose-empty"]')).toContainText("typed pose 경로는 실행됐고");
  await expect(panel.locator('[data-slot="debug-tool-pose-card"]')).toHaveCount(0);
  await expect(panel.locator('[data-slot="debug-tool-pose-readiness"]')).toContainText("실행 완료 · Tool 0건");
});

test("rejects orientation-ready ToolPoseArray evidence while support plane is unvalidated", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  const tools = [validPlanarToolPose()];
  publishPnuEvidence(activeDebugSocket(bridge), {
    supportPlaneValidated: false,
    diagnosticsOverrides: poseDiagnosticsOverrides(tools),
  });
  publishToolPoseEvidence(activeDebugSocket(bridge), { tools });

  await expect(panel.locator('[data-slot="debug-pose-contract-state"]')).toContainText("Tool 자세 계약 불일치");
  await expect(panel.locator('[data-slot="debug-pose-contract-state"]')).toContainText("support plane 미검증");
  await expect(panel.locator('[data-slot="debug-tool-pose-card"]')).toHaveCount(0);
});

test("rejects numeric strings and non-unit quaternions in typed ToolPoseArray", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  const invalidTool = {
    ...validPlanarToolPose(),
    class_confidence: "0.91",
    pose: {
      position: { x: 0.041, y: -0.027, z: 0.806 },
      orientation: { x: 0, y: 0, z: 0, w: 2 },
    },
  };
  const tools = [invalidTool];
  publishPnuEvidence(activeDebugSocket(bridge), {
    supportPlaneValidated: true,
    diagnosticsOverrides: poseDiagnosticsOverrides(tools),
  });
  publishToolPoseEvidence(activeDebugSocket(bridge), { publishOverlay: false, tools });

  await expect(panel.locator('[data-slot="debug-pose-contract-state"]')).toContainText("Tool 자세 계약 불일치");
  await expect(panel.locator('[data-slot="debug-pose-contract-state"]')).toContainText("bounded pose 또는 validity 계약");
  await expect(panel.locator('[data-slot="debug-tool-pose-card"]')).toHaveCount(0);
});

test("distinguishes an executed zero-result overlay from failure", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await expect(panel).toBeVisible();
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), { tool: 0, blood: 0 });

  await expect(panel.locator('[data-slot="debug-perception-executed-zero"]')).toContainText(
    "모델은 실행됐습니다",
  );
  await expect(panel).toContainText("health/diagnostics scalar 계약이 일치합니다");
  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "실행 완료 · 검출 0건",
  );
});

test("shows rate-limited diagnostics without fabricating a browser-local overlay", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await expect(panel).toBeVisible();
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge));
  await expect(panel.locator('[data-slot="debug-blood-list"] > li')).toHaveCount(1);
  await expect(panel.locator('[data-slot="debug-support-plane-runtime"]')).toContainText("12,480");
  publishPnuEvidence(activeDebugSocket(bridge), {
    sec: 1_900_000_001,
    publishOverlay: false,
  });

  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "Scalar 실행 증거 검증 가능",
  );
  await expect(panel.locator('[data-slot="debug-perception-overlay-status"]')).toContainText(
    "Server final overlay RATE_LIMITED · 새 오버레이 미발행",
  );
  await expect(panel.locator('[data-slot="debug-perception-image-consumer-disabled"]')).toBeVisible();
});

test("does not create a browser-local overlay from rate-limited diagnostics", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), {
    diagnosticsOverrides: {
      overlay_published: false,
      overlay_status: "rate_limited",
      overlay_drawn_tool_count: 0,
      overlay_drawn_blood_count: 0,
    },
  });

  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "Scalar 실행 증거 검증 가능",
  );
  await expect(panel.locator('[data-slot="debug-perception-overlay"]')).toHaveCount(0);
  await expect(panel.locator('[data-slot="debug-perception-image-consumer-disabled"]')).toBeVisible();
});

test("surfaces a PNU worker failure without retaining detection evidence", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await expect(panel).toBeVisible();
  await waitForPnuSubscriptions(bridge);
  publishDebugTopic(activeDebugSocket(bridge), "/surgery/perception/rfdetr/health", {
    data: JSON.stringify({
      schema: "taskplanner.rfdetr_health.v1",
      provider: "pnu_hand_blood",
      enabled: true,
      connected: false,
      status: "error",
      model_ready: false,
      semantic_ready: false,
      depth_aligned: false,
      metric_3d_ready: false,
      metric_3d_reasons: ["PNU_REQUEST_REJECTED"],
      support_plane_validated: false,
      transport_mode: "http_local",
      auth_mode: "none_local",
      requested_algorithms: ["tool", "blood"],
      executed_algorithms: [],
      detection_count: 0,
      empty_detection_result: false,
      source_stamp_sec: 1_900_000_000,
      source_stamp_nanosec: 123_456_789,
      last_error_code: "PNU_REQUEST_REJECTED",
      last_error_message: "worker response failed validation",
    }),
  });

  await expect(panel.locator('[data-slot="debug-perception-overlay"]')).toHaveCount(0);
  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "PNU_REQUEST_REJECTED",
  );
  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "worker response failed validation",
  );
  await expect(panel.locator(".debug-perception-kpis dd")).toHaveText(["—", "—", "—"]);
  await expect(panel.locator('[data-slot="debug-perception-model-state"]')).toHaveText("검증 보류");
  await expect(panel.locator('[data-slot="debug-perception-executed-state"]')).toHaveText("검증 보류");
});

test("rejects a string depth-scale flag instead of displaying it as validated", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), {
    publishOverlay: false,
    diagnosticsOverrides: { depth_scale_validated: "false" },
  });

  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "인식 계약 불일치",
  );
  await expect(panel.locator('[data-slot="debug-perception-overlay"]')).toHaveCount(0);
  await expect(
    panel.locator(".debug-perception-readiness > div").filter({ hasText: "Depth scale" }),
  ).toContainText("NOT VALIDATED");
});

test("rejects numeric strings in versioned PNU diagnostics", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), {
    publishOverlay: false,
    diagnosticsOverrides: { inference_latency_ms: "84.5" },
  });

  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "인식 계약 불일치",
  );
  await expect(panel.locator(".debug-perception-kpis dd")).toHaveText(["—", "—", "—"]);
});

test("requires a pinned SHA-256 digest for every successful requested model", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), {
    publishOverlay: false,
    diagnosticsOverrides: {
      model_digests: { tool: "a".repeat(64) },
    },
  });

  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "인식 계약 불일치",
  );
  await expect(panel.locator('[data-slot="debug-perception-overlay"]')).toHaveCount(0);
});

test("rejects an empty successful requested and executed algorithm contract", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), {
    publishOverlay: false,
    diagnosticsOverrides: {
      requested_algorithms: [],
      executed_algorithms: [],
      model_digests: {},
    },
  });

  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "인식 계약 불일치",
  );
});

test("rejects same-stamp health and diagnostics with different executed model sets", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), {
    blood: 0,
    diagnosticsOverrides: {
      requested_algorithms: ["tool"],
      executed_algorithms: ["tool"],
      model_version: "tool:v1",
      model_digests: { tool: "a".repeat(64) },
    },
  });

  await expect(panel.locator('[data-slot="debug-perception-overlay"]')).toHaveCount(0);
  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "인식 계약 불일치",
  );
  await expect(panel).toContainText("health와 diagnostics 실행·검출·3D 계약이 일치하지 않습니다");
});

test("does not subscribe to legacy PNU images when their historic stamp options differ", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await expect(panel).toBeVisible();
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), { overlaySec: 1_900_000_001 });

  await expect(panel.locator('[data-slot="debug-perception-overlay"]')).toHaveCount(0);
  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "Scalar 실행 증거 검증 가능",
  );
  await expect(panel.locator('[data-slot="debug-perception-image-consumer-disabled"]')).toBeVisible();
});

test("does not let legacy overlay frame_id options alter scalar evidence", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await expect(panel).toBeVisible();
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), { overlayFrameId: "unexpected_optical_frame" });

  await expect(panel.locator('[data-slot="debug-perception-overlay"]')).toHaveCount(0);
  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "Scalar 실행 증거 검증 가능",
  );
});

test("rejects empty CAM4, diagnostics, and overlay frame_id values", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), {
    rawFrameId: "",
    diagnosticsFrameId: "",
    overlayFrameId: "",
  });

  await expect(panel.locator('[data-slot="debug-perception-overlay"]')).toHaveCount(0);
  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "인식 계약 불일치",
  );
});

test("clears stale scalar evidence while ignoring a legacy raw CAM4 topic", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await expect(panel).toBeVisible();
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge));
  await expect(panel.locator('[data-slot="debug-blood-list"] > li')).toHaveCount(1);
  await expect(panel.locator('[data-slot="debug-support-plane-runtime"]')).toContainText("12,480");

  await page.waitForTimeout(3_200);
  publishDebugTopic(activeDebugSocket(bridge), "/synced/cam_4/color/image_raw/compressed", {
    header: {
      stamp: { sec: 1_900_000_002, nanosec: 123_456_789 },
      frame_id: "cam_4_color_optical_frame",
    },
    format: "jpeg",
    data: DEBUG_RAW_JPEG,
  });
  await expect(panel.locator('[data-slot="debug-perception-overlay"]')).toHaveCount(0);
  await expect(panel.locator('[data-slot="debug-blood-list"]')).toHaveCount(0);
  await expect(panel.locator('[data-slot="debug-blood-state"]')).toContainText("Blood semantics 대기");
  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText("인식 결과 만료");
  await expect(panel.locator(".debug-perception-kpis dd")).toHaveText(["—", "—", "—"]);
  await expect(panel.locator('[data-slot="debug-perception-model-state"]')).toHaveText("검증 보류");
  await expect(panel.locator('[data-slot="debug-perception-executed-state"]')).toHaveText("검증 보류");
  await expect(panel.locator('[data-slot="debug-support-plane-state"]')).toContainText("진단 대기");
  await expect(panel.locator('[data-slot="debug-support-plane-runtime"]')).not.toContainText("12,480");
  await expect(panel).not.toContainText("응답 검증 완료");
});

test("clears all PNU evidence immediately when the ROSBridge generation changes", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await waitForPnuSubscriptions(bridge);
  const tools = [validPlanarToolPose()];
  publishPnuEvidence(activeDebugSocket(bridge), {
    supportPlaneValidated: true,
    diagnosticsOverrides: poseDiagnosticsOverrides(tools),
  });
  publishToolPoseEvidence(activeDebugSocket(bridge), { tools });
  await expect(panel.locator('[data-slot="debug-tool-pose-card"]')).toHaveCount(1);
  await expect(panel.locator('[data-slot="debug-blood-list"] > li')).toHaveCount(1);
  await expect(panel.locator('[data-slot="debug-support-plane-runtime"]')).toContainText("12,480");
  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "Scalar 실행 증거 검증 가능",
  );

  const previousGeneration = bridge.connectionCount();
  await activeDebugSocket(bridge).close({ code: 1012, reason: "test generation change" });
  await expect(panel.locator('[data-slot="debug-perception-overlay"]')).toHaveCount(0);
  await expect(panel.locator('[data-slot="debug-tool-pose-card"]')).toHaveCount(0);
  await expect(panel.locator('[data-slot="debug-blood-list"]')).toHaveCount(0);
  await expect(panel.locator('[data-slot="debug-blood-state"]')).toContainText("Blood semantics 대기");
  await expect(panel.locator(".debug-perception-kpis dd")).toHaveText(["—", "—", "—"]);
  await expect(panel.locator('[data-slot="debug-support-plane-state"]')).toContainText("진단 대기");
  await expect(panel.locator('[data-slot="debug-support-plane-runtime"]')).not.toContainText("12,480");
  await expect(panel.locator('[data-slot="debug-perception-evidence-state"]')).toContainText(
    "PNU health 대기",
  );

  await expect.poll(bridge.connectionCount).toBeGreaterThan(previousGeneration);
  await waitForPnuSubscriptions(bridge);
  publishPnuEvidence(activeDebugSocket(bridge), {
    sec: 1_900_000_010,
    supportPlaneValidated: true,
    diagnosticsOverrides: poseDiagnosticsOverrides(tools),
  });
  publishToolPoseEvidence(activeDebugSocket(bridge), { sec: 1_900_000_010, tools });
  await expect(panel.locator('[data-slot="debug-blood-list"] > li')).toHaveCount(1);
});

test("fails closed when the Debug status snapshot is malformed", async ({ page }) => {
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = debugStatus("malformed-status");
      status.action = {
        ...(status.action as Record<string, unknown>),
        progress: 2,
      };
      return status;
    },
  });

  await expect(page.locator("[data-slot='debug-error-state']")).toBeVisible();
  await expect(page.locator("[data-slot='debug-error-state']")).toContainText("상태 스냅샷");
  await expect(page.getByRole("button", { name: "수동 제어 활성화" })).toHaveCount(0);
});

test("fails closed when the Debug status payload exceeds the UI bound", async ({ page }) => {
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = debugStatus("oversized-status");
      status.voice = {
        ...(status.voice as Record<string, unknown>),
        last_sentence: "x".repeat(512 * 1024 + 1),
      };
      return status;
    },
  });

  await expect(page.locator("[data-slot='debug-error-state']")).toBeVisible();
  await expect(page.locator("[data-slot='debug-error-state']")).toContainText("상태 스냅샷");
  await expect(page.getByRole("button", { name: "수동 제어 활성화" })).toHaveCount(0);
});

test("fails closed when the Debug status collection exceeds the UI bound", async ({ page }) => {
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = debugStatus("unbounded-status");
      status.recent_events = Array.from({ length: 513 }, (_, index) => ({
        event_id: `event-${index}`,
        message: "bounded test event",
      }));
      return status;
    },
  });

  await expect(page.locator("[data-slot='debug-error-state']")).toBeVisible();
  await expect(page.locator("[data-slot='debug-error-state']")).toContainText("상태 스냅샷");
  await expect(page.getByRole("button", { name: "수동 제어 활성화" })).toHaveCount(0);
});

test("fails closed when the Debug status snapshot timestamp is stale", async ({ page }) => {
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = debugStatus("stale-status");
      status.stamp_sec = Date.now() / 1000 - 20;
      return status;
    },
  });

  await expect(page.locator("[data-slot='debug-error-state']")).toBeVisible();
  await expect(page.locator("[data-slot='debug-error-state']")).toContainText("현재 연결과 맞지 않습니다");
  await expect(page.getByRole("button", { name: "수동 제어 활성화" })).toHaveCount(0);
});

test("fails closed when a Debug status timestamp moves backwards", async ({ page }) => {
  const bridge = await openDebugWorkspace(page);
  await expect(page.getByRole("button", { name: "수동 제어 활성화" })).toBeEnabled();

  const olderStatus = debugStatus("backwards-status");
  olderStatus.stamp_sec = Date.now() / 1000 - 1;
  bridge.sockets[0]?.send(JSON.stringify({
    op: "publish",
    topic: "/integration/debug/status",
    msg: { data: JSON.stringify(olderStatus) },
  }));

  await expect(page.getByRole("status")).toContainText("이전 상태보다 뒤로 갔습니다");
  await expect(page.getByRole("button", { name: "수동 제어 활성화" })).toBeDisabled();
});

test("rejects a non-boolean Debug command acceptance response", async ({ page }) => {
  const commands: string[] = [];
  await openDebugWorkspace(page, {
    commandResponseValues: () => ({ accepted: "false" }),
    onCommand: (operation) => commands.push(operation),
  });

  const manualButton = page.getByRole("button", { name: "수동 제어 활성화" });
  await expect(manualButton).toBeEnabled();
  await manualButton.click();
  await expect.poll(() => commands).toEqual(["arm"]);
  await expect(page.getByRole("alert")).toContainText("수락 형식이 유효하지 않습니다");
  await expect(manualButton).toBeEnabled();
});

test("locks Debug writes and cancels a pending command when status becomes stale", async ({ page }) => {
  const commands: string[] = [];
  await openDebugWorkspace(page, {
    respondToCommands: false,
    onCommand: (operation) => commands.push(operation),
  });
  await page.getByRole("tab", { name: "리트랙터 6개 명령" }).click();
  await expect(page.locator("#debug-operational-interlock")).toBeVisible();

  const manualButton = page.getByRole("button", { name: "수동 제어 활성화" });
  await expect(manualButton).toBeEnabled();
  await manualButton.click();
  await expect.poll(() => commands).toEqual(["arm"]);
  await manualButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    button.setAttribute("disabled", "");
  });
  await page.waitForTimeout(100);
  expect(commands).toEqual(["arm"]);
  await expect(manualButton).toBeDisabled();

  await expect(page.getByText(/디버그 상태 heartbeat가 만료되었습니다/)).toBeVisible({ timeout: 4_500 });
  await expect(manualButton).toBeDisabled();
  await expect(page.getByRole("alert")).toContainText("heartbeat가 만료");
  const staleInterlock = page.locator("#debug-operational-interlock");
  await expect(staleInterlock).toHaveClass(/warning/);
  await expect(staleInterlock).not.toHaveClass(/active/);
  await expect(staleInterlock).toContainText("개입 허용 상태를 기다리고 있습니다");
  await expect(page.locator(".debug-header-status").getByText("관찰 가능 · 개입 상태 대기")).toBeVisible();
  await page.getByRole("tab", { name: "ROS 연결" }).click();
  await page.setViewportSize({ width: 320, height: 800 });
  await page.waitForTimeout(300);
  const compactStaleOverflow = await page.evaluate(() => {
    const limit = document.documentElement.clientWidth;
    return [...document.querySelectorAll<HTMLElement>("body *")].flatMap((element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      if (style.display === "none" || style.visibility === "hidden" || rect.width <= 0 || rect.height <= 0) return [];
      return rect.left < -1 || rect.right > limit + 1 ? [{ element: element.className || element.tagName, left: Math.round(rect.left), right: Math.round(rect.right) }] : [];
    });
  });
  expect(compactStaleOverflow).toEqual([]);
  const ageBadge = page.locator(".debug-header-status .debug-status-badge").nth(1);
  const staleAge = await ageBadge.textContent();
  await page.waitForTimeout(750);
  await expect(ageBadge).not.toHaveText(staleAge || "");
  await manualButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForTimeout(100);
  expect(commands).toEqual(["arm"]);
});

test("serializes Debug exit cleanup while a stop command is pending", async ({ page }) => {
  const commands: string[] = [];
  await openDebugWorkspace(page, {
    commandResponseDelayMs: 250,
    onCommand: (operation) => commands.push(operation),
  });

  const exitButton = page.getByRole("button", { name: "운영 화면으로" });
  await exitButton.click();
  await expect(exitButton).toBeDisabled();
  await exitButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    button.setAttribute("disabled", "");
  });
  await page.waitForTimeout(100);
  expect(commands).toEqual(["stop_outputs"]);

  await expect.poll(() => commands).toEqual(["stop_outputs", "disarm"]);
  await expect(exitButton).toBeDisabled();
});

test("keeps the new Debug generation ready during rapid reconnect cleanup", async ({ page }) => {
  const bridge = await openDebugWorkspace(page, {
    statusForConnection: (connection) => debugStatus(`session-${connection}`, connection >= 2),
  });

  await expect(page.getByRole("button", { name: "수동 제어 활성화" })).toBeEnabled();
  await expect(page.getByText(/디버그 상태 heartbeat가 만료되었습니다/)).toBeVisible({ timeout: 4_500 });
  await page.getByRole("button", { name: "다시 연결" }).click();

  await expect.poll(bridge.connectionCount).toBe(2);
  const disarmButton = page.getByRole("button", { name: "수동 제어 해제" });
  await expect(disarmButton).toBeEnabled();
  await page.waitForTimeout(500);
  await expect(disarmButton).toBeEnabled();
});

test("separates individual diagnostics, integrated scenarios, and observability", async ({ page }) => {
  await openDebugWorkspace(page, {
    statusForConnection: () => manualControlsReadyStatus("debug-information-architecture"),
  });

  await expect(page.getByText("개별 기능", { exact: true })).toBeVisible();
  await expect(page.getByText("통합 시나리오", { exact: true })).toBeVisible();
  await expect(page.getByText("관측", { exact: true })).toBeVisible();
  await expect(page.getByRole("tab")).toHaveCount(11);
  await expect(page.getByRole("tab", { name: /^멀티캠 관제/ })).toBeVisible();

  const tabHeights = await page.getByRole("tab").evaluateAll((tabs) => tabs.map((tab) => tab.getBoundingClientRect().height));
  expect(tabHeights.every((height) => height >= 44)).toBe(true);

  await page.getByRole("tab", { name: /음성 도구전달/ }).click();
  const toolPipeline = page.locator('[data-slot="debug-integration-pipeline"]');
  await expect(toolPipeline).toContainText("/sensors/surgeon/sentence");
  await expect(toolPipeline).toContainText("/surgery/audio/observed_utterance");
  await expect(toolPipeline).toContainText("CommandRouter catalog");
  await expect(toolPipeline).toContainText("외부 실제 서버");
  await expect(toolPipeline).toContainText("직접 UI 요청만 외부 서버에 전달될 수 있습니다");

  await page.getByRole("tab", { name: /리트랙터 6개 명령/ }).click();
  const retractorPipeline = page.locator('[data-slot="debug-integration-pipeline"]');
  await expect(retractorPipeline).toContainText("CommandRouter catalog");
  await expect(retractorPipeline).toContainText("Retraction Service");

  await page.getByRole("tab", { name: /관측 로그/ }).click();
  await expect(page.getByRole("heading", { name: "확정 문장 관측" })).toBeVisible();
  await expect(page.getByRole("button", { name: "ASR 시작" })).toHaveCount(0);
});

test("keeps an observed-utterance stream pending until CommandRouter relays a final transcript", async ({ page }) => {
  const status = manualControlsReadyStatus("speech-adapter-waiting-final");
  status.inputs = [
    debugInput("surgeon_sentence", "/sensors/surgeon/sentence"),
    {
      ...debugInput("observed_surgeon_utterance", "/surgery/audio/observed_utterance"),
      message_count: 0,
      window_message_count: 0,
      last_sample: "",
      state: "WAITING_MESSAGES",
    },
  ];
  await openDebugWorkspace(page, { statusForConnection: () => status });

  await page.getByRole("tab", { name: /음성 도구전달/ }).click();
  const adapterStage = page.locator('[data-slot="debug-observed-utterance-stage"]');
  await expect(adapterStage).toHaveAttribute("data-stage-state", "PENDING_FINAL");
  await expect(adapterStage).toHaveClass(/pending/);
  await expect(adapterStage).toContainText("final utterance 대기");
  await expect(adapterStage).not.toHaveClass(/error/);
});

test("keeps observation available while the operational intervention gate is closed", async ({ page }) => {
  const reason = "pause or stop the operational scenario before manual control";
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = manualControlsReadyStatus("running-observation-only");
      status.runtime = {
        ...(status.runtime as Record<string, unknown>),
        blocked_nodes: ["simulation_manager", "tree_executor"],
        detected_planner_nodes: ["simulation_manager", "tree_executor"],
        operational_state: "running",
        operational_running: true,
        // Deliberately contradictory legacy fields prove they no longer admit UI writes.
        operational_runtime_stopped: true,
        manual_control_available: true,
        planner_coexistence_allowed: true,
        operational_intervention_allowed: false,
        operational_intervention_block_reason: reason,
        operational_control_window_open: false,
        manual_control_gate: "operational_state",
        network: {
          ...((status.runtime as Record<string, unknown>).network as Record<string, unknown>),
          locked_to_runtime: true,
        },
      };
      return status;
    },
  });

  const manualButton = page.getByRole("button", { name: "수동 제어 잠김" });
  await expect(manualButton).toBeDisabled();
  await page.getByRole("tab", { name: /음성 도구전달/ }).click();
  await expect(page.locator("#debug-operational-interlock")).toContainText(reason);
  await expect(page.locator("#debug-operational-interlock")).toContainText("단순 상태·토픽 관찰");
  await expect(page.getByRole("button", { name: "도구 전달 요청" })).toBeDisabled();
  await expect(page.locator("#debug-coexistence-checkbox")).toHaveCount(0);
});

test("fails closed when the optional operational intervention fields are absent", async ({ page }) => {
  const commands: string[] = [];
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = manualControlsReadyStatus("intervention-status-not-rolled-out");
      const runtime = status.runtime as Record<string, unknown>;
      delete runtime.operational_intervention_allowed;
      delete runtime.operational_intervention_block_reason;
      delete runtime.operational_control_window_open;
      delete runtime.manual_control_gate;
      // Legacy values must not become an implicit compatibility bypass.
      runtime.operational_runtime_stopped = true;
      runtime.manual_control_available = true;
      return status;
    },
    onCommand: (operation) => commands.push(operation),
  });

  await expect(page.getByRole("button", { name: "수동 제어 잠김" })).toBeDisabled();
  await page.getByRole("tab", { name: /음성 도구전달/ }).click();
  await expect(page.locator("#debug-operational-interlock")).toContainText(
    "operational_intervention_allowed 상태를 아직 제공하지 않았습니다",
  );
  const handoverButton = page.getByRole("button", { name: "도구 전달 요청" });
  await expect(handoverButton).toBeDisabled();
  await handoverButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.click();
  });
  await expect(page.getByRole("alert")).toContainText("시나리오 개입이 잠겼습니다");
  expect(commands).toEqual([]);
});

test("arms integrated Debug from a paused idle snapshot without planner acknowledgement", async ({ page }) => {
  const commands: Array<{ operation: string; payload: Record<string, unknown> }> = [];
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = manualControlsReadyStatus("paused-intervention-window");
      status.runtime = {
        ...(status.runtime as Record<string, unknown>),
        operational_state: "paused",
        operational_running: true,
        operational_runtime_stopped: false,
        operational_intervention_allowed: true,
        operational_intervention_block_reason: "",
        operational_control_window_open: true,
        manual_control_available: true,
        manual_control_gate: "operational_state",
        detected_planner_nodes: ["simulation_manager", "tree_executor"],
        network: {
          ...((status.runtime as Record<string, unknown>).network as Record<string, unknown>),
          locked_to_runtime: true,
        },
      };
      return status;
    },
    onCommand: (operation, payload) => commands.push({ operation, payload }),
  });

  const manualButton = page.getByRole("button", { name: "수동 제어 활성화" });
  await expect(manualButton).toBeEnabled();
  await manualButton.click();
  await expect.poll(() => commands).toEqual([{ operation: "arm", payload: {} }]);
  await expect(page.getByRole("button", { name: "수동 제어 해제" })).toBeVisible();
});

test("keeps an absent observed-utterance publisher in observation waiting state", async ({ page }) => {
  const status = manualControlsReadyStatus("observed-utterance-waiting-publisher");
  status.inputs = [
    debugInput("surgeon_sentence", "/sensors/surgeon/sentence"),
    {
      ...debugInput("observed_surgeon_utterance", "/surgery/audio/observed_utterance"),
      publisher_count: 0,
      publishers: [],
      message_count: 0,
      window_message_count: 0,
      last_sample: "",
      state: "WAITING_MESSAGES",
    },
  ];
  await openDebugWorkspace(page, { statusForConnection: () => status });

  await page.getByRole("tab", { name: /음성 도구전달/ }).click();
  const adapterStage = page.locator('[data-slot="debug-observed-utterance-stage"]');
  await expect(adapterStage).toHaveAttribute("data-stage-state", "WAITING_ROUTER");
  await expect(adapterStage).toHaveClass(/idle/);
  await expect(adapterStage).toContainText("CommandRouter publisher 대기");
});

test("switches explicit endpoint source only while disarmed and without robot dispatch", async ({ page }) => {
  const commands: Array<{ operation: string; payload: Record<string, unknown> }> = [];
  await openDebugWorkspace(page, {
    statusForConnection: () => debugStatus("endpoint-source-selector"),
    onCommand: (operation, payload) => commands.push({ operation, payload }),
  });

  await page.getByRole("tab", { name: /Service·Action 종단/ }).click();
  const source = page.locator('[data-slot="debug-robot-endpoint-source"]');
  await expect(source.getByRole("button", { name: /^외부 실제 서버/ })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText(/자동 fallback이나 두 서버 혼합 없이/)).toBeVisible();
  await source.getByRole("button", { name: /^가상 진단 서버/ }).click();
  await expect.poll(() => commands).toContainEqual({
    operation: "configure_robot_endpoint_source",
    payload: { source: "virtual" },
  });
  expect(commands.map(({ operation }) => operation)).not.toContain("retraction_command");
  expect(commands.map(({ operation }) => operation)).not.toContain("tool_handover");
});

test("labels a disabled virtual owner as unavailable without selecting a fallback", async ({ page }) => {
  const status = debugStatus("virtual-owner-not-started");
  status.virtual_robot = {
    ...(status.virtual_robot as Record<string, unknown>),
    enabled: false,
    selected_source: "external",
  };
  await openDebugWorkspace(page, { statusForConnection: () => status });

  await page.getByRole("tab", { name: /Service·Action 종단/ }).click();
  const source = page.locator('[data-slot="debug-robot-endpoint-source"]');
  await expect(source.getByRole("button", { name: /^외부 실제 서버/ })).toHaveAttribute("aria-pressed", "true");
  await expect(source.getByRole("button", { name: /^가상 진단 서버/ })).toBeDisabled();
  await expect(page.locator('[data-slot="debug-virtual-owner-not-started"]')).toContainText(
    "가상 Debug owner가 시작되지 않았습니다",
  );
});

test("locks endpoint source switching while manual control is armed", async ({ page }) => {
  await openDebugWorkspace(page, {
    statusForConnection: () => debugStatus("endpoint-source-armed", true),
  });

  await page.getByRole("tab", { name: /Service·Action 종단/ }).click();
  const sourceButtons = page.locator('[data-slot="debug-robot-endpoint-source"] button');
  await expect(sourceButtons).toHaveCount(2);
  expect(await sourceButtons.evaluateAll((buttons) => buttons.every((button) => (button as HTMLButtonElement).disabled))).toBe(true);
  await expect(page.locator('[data-slot="debug-endpoint-source-locked"]')).toContainText("수동 제어를 해제");
});

test("unlocks discovered Action and Service controls immediately after manual arm admission", async ({ page }) => {
  const commands: string[] = [];
  await openDebugWorkspace(page, {
    statusForConnection: () => manualControlsReadyStatus("manual-arm-session"),
    onCommand: (operation) => commands.push(operation),
  });

  await page.getByRole("tab", { name: /음성 도구전달/ }).click();
  const actionButton = page.getByRole("button", { name: "도구 전달 요청" });
  await expect(actionButton).toBeDisabled();

  await page.getByRole("button", { name: "수동 제어 활성화" }).click();
  await expect.poll(() => commands).toEqual(["arm"]);
  await expect(page.getByRole("button", { name: "수동 제어 해제" })).toBeVisible();
  await expect(actionButton).toBeEnabled();
  await page.getByRole("tab", { name: /리트랙터 6개 명령/ }).click();
  const serviceButton = page.getByRole("button", { name: "직접 교시 시작" });
  await expect(serviceButton).toBeEnabled();
  await expect(page.getByRole("button", { name: "Tool change" })).toBeEnabled();
  expect(commands).toEqual(["arm"]);
});

test("uses the single retraction Service contract without legacy jog fields", async ({ page }) => {
  const commands: Array<{ operation: string; payload: Record<string, unknown> }> = [];
  await openDebugWorkspace(page, {
    refreshStatusOnCommand: true,
    statusForConnection: () => retractionServiceStatus("retraction-service-session"),
    onCommand: (operation, payload) => commands.push({ operation, payload }),
  });

  await page.getByRole("tab", { name: /리트랙터 6개 명령/ }).click();
  await expect(page.getByRole("heading", { name: "리트랙터 명령" })).toBeVisible();
  await expect(page.getByText("/surgery/retraction/command")).toBeVisible();
  await expect(page.getByText("양측 동시")).toHaveCount(0);
  await expect(page.getByText(/방향·축·양측 조정과 arm_id·target_tool_id/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "요청 접수 결과" })).toBeVisible();
  await expect(page.getByRole("progressbar", { name: "Action 진행률" })).toHaveCount(0);
  await expect(page.getByText(/실제 물리 동작의 진행·완료·상태/)).toBeVisible();
  await expect(page.getByText("Debug 내부 상태")).toBeVisible();
  await expect(page.getByText("교시 완료", { exact: true })).toBeVisible();
  await expect(page.getByText("현재 허용 명령")).toBeVisible();
  await expect(page.getByRole("button", { name: "직접 교시 시작" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Retraction 시작" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Tool change" })).toBeDisabled();

  await page.getByRole("button", { name: "Retraction 시작" }).click();
  await expect.poll(() => commands.filter(({ operation }) => operation === "retraction_command")).toContainEqual({
    operation: "retraction_command",
    payload: {
      command: "start_retraction",
      target_side: "none",
      distance_m: 0,
    },
  });

});

test("allows every target field selection when finishing direct teach", async ({ page }) => {
  const commands: Array<{ operation: string; payload: Record<string, unknown> }> = [];
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = retractionServiceStatus("finish-direct-teach-side-session");
      status.voice = {
        auto_execute: false,
        last_sentence: "",
        last_parse: {},
        retraction: retractionVoiceStatus({
          internalState: "direct_teaching",
          allowedCommands: ["finish_direct_teach"],
          serviceReady: true,
        }),
      };
      return status;
    },
    onCommand: (operation, payload) => commands.push({ operation, payload }),
  });

  await page.getByRole("tab", { name: /리트랙터 6개 명령/ }).click();
  for (const [buttonName, targetSide] of [
    ["왼쪽 left", "left"],
    ["오른쪽 right", "right"],
    ["양쪽 both", "both"],
  ] as const) {
    await page.getByRole("button", { name: buttonName }).click();
    const finishButton = page.getByRole("button", { name: "직접 교시 종료" });
    await expect(finishButton).toBeEnabled();
    await finishButton.click();
    await expect.poll(() => commands).toContainEqual({
      operation: "retraction_command",
      payload: {
        command: "finish_direct_teach",
        target_side: targetSide,
        distance_m: 0,
      },
    });
  }
});

test("forces only the Debug retraction state to idle after explicit confirmation", async ({ page }) => {
  const commands: Array<{ operation: string; payload: Record<string, unknown> }> = [];
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = retractionServiceStatus("force-retraction-idle-session");
      status.session = {
        ...(status.session as Record<string, unknown>),
        state: "ARMED",
        armed: true,
      };
      status.voice = {
        auto_execute: false,
        last_sentence: "",
        last_parse: {},
        retraction: retractionVoiceStatus({
          internalState: "retraction_active",
          allowedCommands: ["adjust_retraction", "stop_retraction"],
          serviceReady: true,
        }),
      };
      return status;
    },
    onCommand: (operation, payload) => commands.push({ operation, payload }),
  });

  await page.getByRole("tab", { name: /리트랙터 6개 명령/ }).click();
  const reset = page.getByRole("button", { name: "IDLE로 강제 초기화" });
  await expect(reset).toBeEnabled();
  await expect(page.locator('[data-slot="debug-force-retraction-idle"]')).toContainText(
    "로봇 명령은 보내지 않고 Debug 로컬 상태만 IDLE로 바꾸며",
  );

  await reset.click();
  const dialog = page.getByRole("alertdialog", { name: "Debug 상태를 IDLE로 초기화할까요?" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("로봇이나 외부 Service에는 Stop 또는 다른 명령을 보내지 않습니다.");
  expect(commands).toEqual([]);

  await dialog.getByRole("button", { name: "IDLE로 강제 초기화" }).click();
  await expect.poll(() => commands).toContainEqual({
    operation: "force_retraction_idle",
    payload: { remote_motion_stopped_confirmed: true },
  });
  expect(commands.map(({ operation }) => operation)).not.toContain("retraction_command");
  await expect(page.locator(".debug-toast[role='status']")).toContainText("accepted");
});

test("keeps retraction buttons independent of the observed voice relay", async ({ page }) => {
  const commands: Array<{ operation: string; payload: Record<string, unknown> }> = [];
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = retractionServiceStatus("retraction-voice-mode-session");
      status.voice = {
        auto_execute: false,
        last_sentence: "오른쪽 5cm 더",
        last_parse: {},
        retraction: retractionVoiceStatus({
          internalState: "retraction_active",
          allowedCommands: ["adjust_retraction", "stop_retraction"],
          serviceReady: true,
        }),
      };
      status.asr = {
        ...debugAsrStatus(),
        state: "LISTENING",
        connected: true,
        audio_level_dbfs: -38.2,
        peak_level_dbfs: -22.4,
        partial_text: "오른쪽 5cm 더",
      };
      return status;
    },
    onCommand: (operation, payload) => commands.push({ operation, payload }),
  });

  await page.getByRole("tab", { name: /리트랙터 6개 명령/ }).click();
  await expect(page.locator('[data-slot="debug-retraction-voice-status"]')).toContainText("리트랙션 요청 접수");
  await expect(page.locator('[data-slot="debug-retraction-voice-ownership"]')).toContainText("음성 relay는 관측 전용입니다");
  await expect(page.locator('[data-slot="debug-retraction-voice-mode"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: "왼쪽 5 cm 더" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "직접 교시 시작" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Tool change" })).toBeDisabled();
  expect(commands.map(({ operation }) => operation)).not.toContain("asr_start");
  expect(commands.map(({ operation }) => operation)).not.toContain("asr_stop");
});

test("selects a reviewed Debug ASR route without sending a raw WebSocket URL", async ({ page }) => {
  const commands: Array<{ operation: string; payload: Record<string, unknown> }> = [];
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = debugStatus("debug-asr-route-session", true);
      const runtime = status.runtime as Record<string, unknown>;
      runtime.network = {
        ...(runtime.network as Record<string, unknown>),
        locked_to_runtime: true,
      };
      return status;
    },
    onCommand: (operation, payload) => commands.push({ operation, payload }),
  });

  await page.getByRole("tab", { name: /STT 입력·USB 캡처/ }).click();
  const routes = page.locator('[data-slot="debug-asr-endpoint-selector"]');
  await expect(routes).toBeVisible();
  await expect(routes.getByRole("button", { name: /^클라우드/ })).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("#debug-asr-server")).toHaveCount(0);

  await routes.getByRole("button", { name: /^LAN/ }).click();
  await expect(routes.getByRole("button", { name: /^LAN/ })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText(/LAN은 평문/)).toBeVisible();

  await page.getByRole("button", { name: "ASR 시작" }).click();
  await expect.poll(() => commands).toContainEqual({
    operation: "asr_start",
    payload: { device_id: 7, endpoint_id: "lan" },
  });
  expect(commands.find(({ operation }) => operation === "asr_start")?.payload)
    .not.toHaveProperty("server_url");
});

test("observes runtime owners and delegates a scoped restart to host runtime-control", async ({ page }) => {
  const ownerRequests: Array<{ body: unknown; requestId: string | undefined }> = [];
  await page.route(/\/api\/runtime\/owners\?mode=live$/, (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      mode: "live",
      owners: [
        {
          owner: "command",
          mode: "live",
          state: "running",
          service: "taskplanner-command",
          detail: "Up 2 minutes",
        },
        {
          owner: "core",
          mode: "live",
          state: "running",
          service: "taskplanner-state-core",
          detail: "Up 2 minutes",
        },
        {
          owner: "perception",
          mode: "live",
          state: "not-deployed",
          service: "taskplanner-perception",
          detail: "no owner container",
        },
        {
          owner: "simulation-input",
          mode: "live",
          state: "not-applicable",
          service: "",
          detail: "mode not supported",
        },
        {
          owner: "tts",
          mode: "live",
          state: "running",
          service: "taskplanner-tts",
          detail: "Up 4 minutes",
        },
      ],
    }),
  }));
  await page.route(/\/api\/runtime\/owners\/restart$/, async (route) => {
    const request = route.request();
    const body = request.postDataJSON() as { owner?: string; mode?: string };
    ownerRequests.push({
      body,
      requestId: request.headers()["x-taskplanner-request-id"],
    });
    if (body.owner === "command" || body.owner === "core") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          accepted: true,
          owner: body.owner,
          mode: "live",
          message: "The owner restarted.",
        }),
      });
      return;
    }
    if (body.owner === "tts") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          accepted: true,
          owner: "tts",
          mode: "live",
          message: "The owner restarted.",
        }),
      });
      return;
    }
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        accepted: false,
        owner: "perception",
        mode: "live",
        error: "The requested runtime mode is not active and ready.",
      }),
    });
  });

  await openDebugWorkspace(page, { runtimeMode: "live" });

  const panel = page.locator('[data-slot="debug-runtime-owner-panel"]');
  await expect(panel).toHaveAttribute("data-mode", "live");
  const commandControl = panel.locator('[data-slot="debug-command-owner-control"]');
  await expect(commandControl).toContainText("taskplanner-command");
  await expect(panel.locator('.debug-runtime-owner-list [data-owner="command"]')).toHaveCount(0);
  await expect(panel.locator('[data-owner="simulation-input"]')).toHaveCount(0);
  await expect(panel.locator('[data-owner="core"]')).toContainText("taskplanner-state-core");
  await expect(panel.locator('[data-owner="perception"]')).toContainText("no owner container");
  await expect(panel.getByRole("button", { name: "TTS owner만 재시작" })).toBeEnabled();
  await expect(panel.locator('[data-owner="tts"]')).toHaveCount(0);
  await commandControl.getByRole("button", { name: "Command owner만 재시작" }).click();
  await expect.poll(() => ownerRequests).toHaveLength(1);
  expect(ownerRequests[0]).toEqual({
    body: { owner: "command", mode: "live" },
    requestId: expect.stringMatching(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/),
  });
  await expect(panel).toContainText("The owner restarted.");
  await panel.locator('[data-owner="perception"]').getByRole("button", { name: "재시작 요청" }).click();
  await expect.poll(() => ownerRequests).toHaveLength(2);
  expect(ownerRequests[1]).toEqual({
    body: { owner: "perception", mode: "live" },
    requestId: expect.stringMatching(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/),
  });
  await expect(panel).toContainText("The requested runtime mode is not active and ready.");
  await panel.locator('[data-owner="core"]').getByRole("button", { name: "재시작 요청" }).click();
  await expect.poll(() => ownerRequests).toHaveLength(3);
  expect(ownerRequests[2]).toEqual({
    body: { owner: "core", mode: "live" },
    requestId: expect.stringMatching(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/),
  });
  await expect(panel).toContainText("The owner restarted.");
  await expect(panel.locator('[data-owner="core"]')).toBeVisible();
  await panel.getByRole("button", { name: "TTS owner만 재시작" }).click();
  await expect.poll(() => ownerRequests).toHaveLength(4);
  expect(ownerRequests[3]).toEqual({
    body: { owner: "tts", mode: "live" },
    requestId: expect.stringMatching(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/),
  });
});

test("shows only configured standalone Debug owners with scoped restart controls", async ({ page }) => {
  const ownerRequests: Array<{ owner?: string; mode?: string }> = [];
  await page.route(/\/api\/runtime\/owners\?mode=debug$/, (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      mode: "debug",
      owners: [
        {
          owner: "command",
          mode: "debug",
          state: "not-applicable",
          service: "",
          detail: "mode not supported",
        },
        {
          owner: "debug-observer",
          mode: "debug",
          state: "running",
          service: "taskplanner-debug-observer",
          detail: "Up 2 minutes",
        },
        {
          owner: "debug-control",
          mode: "debug",
          state: "running",
          service: "taskplanner-debug-control",
          detail: "Up 2 minutes",
        },
        {
          owner: "debug-virtual",
          mode: "debug",
          state: "not-deployed",
          service: "taskplanner-debug-virtual",
          detail: "no owner container",
        },
      ],
    }),
  }));
  await page.route("**/api/runtime/owners/restart", async (route) => {
    const body = route.request().postDataJSON() as { owner?: string; mode?: string };
    ownerRequests.push(body);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        accepted: true,
        owner: body.owner,
        mode: body.mode,
        message: "The owner restarted.",
      }),
    });
  });

  await openDebugWorkspace(page);

  const panel = page.locator('[data-slot="debug-runtime-owner-panel"]');
  await expect(panel).toHaveAttribute("data-mode", "debug");
  await expect(panel.locator('[data-owner="debug-observer"]')).toContainText("taskplanner-debug-observer");
  await expect(panel.locator('[data-owner="debug-control"]')).toContainText("taskplanner-debug-control");
  await expect(panel.locator('[data-owner="debug-virtual"]')).toContainText("taskplanner-debug-virtual");
  await expect(panel.locator('[data-owner="command"]')).toHaveCount(0);
  await panel.locator('[data-owner="debug-control"]').getByRole("button", { name: "재시작 요청" }).click();
  await expect.poll(() => ownerRequests).toEqual([{ owner: "debug-control", mode: "debug" }]);
});

test("holds retraction controls while a Service admission response is pending", async ({ page }) => {
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = retractionServiceStatus("retraction-in-flight-session");
      status.action = {
        route: "retraction_service",
        command_id: "retraction-command-pending",
        command: "adjust_retraction",
        response_semantics: "admission",
        request_accepted: null,
        result_code: null,
        response_message: "",
        state: "submitting",
        progress: 0,
        success: false,
        terminal: false,
        reason_code: "",
        recovery_required: false,
      };
      status.voice = {
        auto_execute: false,
        last_sentence: "",
        last_parse: {},
        retraction: retractionVoiceStatus({
          internalState: "retraction_active",
          allowedCommands: ["adjust_retraction", "stop_retraction"],
          serviceReady: true,
          inFlight: true,
        }),
      };
      return status;
    },
  });

  await page.getByRole("tab", { name: /리트랙터 6개 명령/ }).click();
  await expect(page.locator('[data-slot="debug-retraction-voice-status"]')).toContainText("접수 응답 대기");
  await expect(page.getByRole("button", { name: "왼쪽 5 cm 더" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "IDLE로 강제 초기화" })).toBeDisabled();
});

test("scopes SurgiMate lifecycle controls to standalone Debug", async ({ page }) => {
  let running = true;
  const actions: Array<{ action: string; requestId: string | undefined }> = [];
  await page.route("**/api/runtime/surgimate", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          active_mode: "debug",
          status: {
            owner: "surgimate",
            mode: "debug",
            state: running ? "running" : "exited",
            service: "taskplanner-surgimate",
            detail: running ? "Up 2 seconds" : "Exited (0)",
          },
        }),
      });
      return;
    }
    const payload = route.request().postDataJSON() as { action?: string };
    const action = payload.action || "";
    actions.push({
      action,
      requestId: route.request().headers()["x-taskplanner-request-id"],
    });
    if (action === "stop") running = false;
    if (action === "start" || action === "restart") running = true;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ accepted: true, action, message: `SurgiMate ${action} complete.` }),
    });
  });

  await openDebugWorkspace(page);
  const panel = page.locator('[data-slot="surgimate-sidecar-panel"]');
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute("data-state", "running");
  await expect(panel.getByRole("button", { name: "시작", exact: true })).toBeDisabled();

  await panel.getByRole("button", { name: "중지", exact: true }).click();
  await expect.poll(() => actions).toHaveLength(1);
  await expect(panel.getByRole("button", { name: "시작", exact: true })).toBeEnabled();
  await panel.getByRole("button", { name: "시작", exact: true }).click();
  await expect(panel).toHaveAttribute("data-state", "running");
  await expect(panel.getByRole("button", { name: "중지", exact: true })).toBeEnabled();
  await panel.getByRole("button", { name: "재시작", exact: true }).click();
  await expect.poll(() => actions).toHaveLength(3);
  expect(actions).toEqual([
    { action: "stop", requestId: expect.stringMatching(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/) },
    { action: "start", requestId: expect.stringMatching(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/) },
    { action: "restart", requestId: expect.stringMatching(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/) },
  ]);

  await page.route("http://127.0.0.1:5174/", (route) => route.fulfill({
    contentType: "text/html",
    body: "<main>SurgiMate same-tab navigation</main>",
  }));
  const pagesBeforeNavigation = page.context().pages().length;
  await Promise.all([
    page.waitForURL("http://127.0.0.1:5174/"),
    panel.getByRole("button", { name: "SurgiMate로 이동" }).click(),
  ]);
  await expect(page.locator("main")).toContainText("SurgiMate same-tab navigation");
  expect(page.context().pages()).toHaveLength(pagesBeforeNavigation);

  const otherPage = await page.context().newPage();
  await openDebugWorkspace(otherPage, { runtimeMode: "live" });
  await expect(otherPage.locator('[data-slot="surgimate-sidecar-panel"]')).toBeVisible();
  await otherPage.close();
});
