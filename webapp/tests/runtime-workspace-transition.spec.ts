import { expect, test, type Page, type WebSocketRoute } from "playwright/test";

const localFrontendBaseUrl =
  process.env.PLAYWRIGHT_BASE_URL ??
  `http://127.0.0.1:${process.env.PLAYWRIGHT_WEB_PORT ?? "4173"}`;
const asrRestartRequestIdPattern = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;

async function openLegacyMulticamWorkspace(page: Page) {
  await page.goto("/?workspace=multicam");
}

type LauncherMode = "live" | "llm-surgeon" | "replay" | "debug";

type RuntimeStatus = {
  phase: "idle" | "starting" | "failed";
  active_mode: LauncherMode | null;
  requested_mode: LauncherMode | null;
  message?: string;
  retryable: boolean;
  diagnostic_code?: "runtime_profile_mismatch" | null;
};

type RosbridgeStubOptions = {
  onSocketConnect?: (url: string) => void;
  shadowReplayState?: {
    state: string;
    loaded: boolean;
    running: boolean;
    paused: boolean;
    completed: boolean;
  };
  shadowReplayStateMessage?: unknown;
  shadowControlStateJson?: string;
  onShadowSubscription?: () => void;
  simulationState?: {
    running: boolean;
    execution_state: string;
  };
  simulationStateMessage?: unknown;
  worldStateMessage?: unknown;
  simulationStateHeartbeatCount?: number;
  simulationStateHeartbeatIntervalMs?: number;
  withholdSimulationStateSubscriptions?: number;
  onSimulationSubscription?: () => void;
  surgeonLlmDecision?: unknown;
  observerAvailable?: boolean;
  publishCaptureStatus?: boolean;
  captureStatusPublishLimit?: number;
  observerCaptureStatusMessage?: unknown;
  publishWorldStatus?: boolean;
  observerWorldStatusMessage?: unknown;
  publishObserverImages?: boolean;
  observerImagePublishLimit?: number;
  observerImageMessage?: unknown;
  publishVlmImages?: boolean;
  vlmImagePublishLimit?: number;
  vlmImageTopic?: string;
  vlmImageMessage?: unknown;
  vlmRequestContextMessage?: unknown;
  publishVlmHealth?: boolean;
  vlmHealthPublishLimit?: number;
  vlmHealthMessage?: unknown;
  vlmResultMessage?: unknown;
  skillStatusMessages?: unknown[];
  executionTraceMessages?: unknown[];
  respondToObserverServices?: boolean;
  liveAsrStatus?: Record<string, unknown>;
  liveAsrStatusMessage?: unknown;
  liveAsrStatusEnvelope?: Record<string, unknown>;
  onLiveAsrStatusPublisher?: (publish: (envelope: Record<string, unknown>) => void) => void;
  onLiveAsrControlRequest?: (args: Record<string, unknown>) => void;
  liveAsrControlResponse?:
    | Record<string, unknown>
    | ((args: Record<string, unknown>) => Record<string, unknown>);
  publishIntegrationReadiness?: boolean;
  integrationReadiness?: Record<string, unknown>;
  integrationReadinessMessage?: unknown;
  executionRouteState?: Record<string, unknown>;
  executionRouteCommandResponse?: Record<string, unknown>;
  onExecutionRouteCommand?: (args: Record<string, unknown>) => void;
  withholdMissionService?: string;
  missionControlResponseDelayMs?: Partial<Record<string, number>>;
  onServiceCall?: (service: string) => void;
  onMissionServiceCall?: (service: string) => void;
  onSelectBundleRequest?: (args: Record<string, unknown>) => void;
  selectBundleResponse?:
    | Record<string, unknown>
    | ((args: Record<string, unknown>) => Record<string, unknown>);
  debugStatus?: Record<string, unknown>;
};

function integratedDebugStatus(): Record<string, unknown> {
  return {
    schema: "taskplanner.integration_debug.status.v1",
    stamp_sec: Date.now() / 1_000,
    session: {
      session_id: "live-integrated-observation",
      state: "MONITOR_ONLY",
      armed: false,
      fault_locked: false,
      last_error: "",
      event_log_path: "/tmp/integration-debug-events.jsonl",
    },
    runtime: {
      ros_domain_id: "0",
      rmw_implementation: "rmw_cyclonedds_cpp",
      discovery_range: "SUBNET",
      blocked_nodes: ["taskplanner-runtime"],
      operational_runtime_stopped: false,
      manual_control_available: false,
      planner_coexistence_allowed: false,
      network: {
        primary_interface: "enp13s0",
        primary_ipv4: "192.168.1.4",
        prefix_length: 24,
        gateway_ipv4: "192.168.1.1",
        multicast_capable: true,
        interface_present: true,
        link_up: true,
        addresses: [],
        settings_path: "/tmp/debug-network.json",
        restart_supported: false,
        restart_scheduled: false,
        locked_to_runtime: true,
      },
    },
    inputs: [],
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
    voice: { auto_execute: false, last_sentence: "", last_parse: {} },
    asr: { available: false, state: "STOPPED", devices: [], finals: [] },
    surgery_record: { state: "IDLE", history: [] },
    recent_events: [],
  };
}

function freshStructuredVlmRequestContext(): Record<string, unknown> {
  const cam3Freshness = { status: "fresh", received_age_sec: 0.02 };
  const cam4Freshness = { status: "fresh", received_age_sec: 0.03 };
  const cam3VisualAlignment = { status: "not_compared_no_flir_reference" };
  const cam4VisualAlignment = {
    status: "misaligned",
    detector_stamp_sec: 1_700_000_000.25,
    offset_sec: 0.42,
  };
  return {
    stamp: { sec: 1_700_000_001, nanosec: 125_000_000 },
    compact_json: JSON.stringify({
      observable_perception: {
        schema: "taskplanner.rfdetr_multiview_tool_context.v1",
        source: "rfdetr_tool_observation_2d",
        ground_truth: false,
        flir_reference_stamp_sec: null,
        max_source_skew_sec: 0.35,
        mask_rle_forwarded_to_vlm: false,
        freshness: {
          cam_3: cam3Freshness,
          cam_4: cam4Freshness,
        },
        visual_alignment: {
          cam_3: cam3VisualAlignment,
          cam_4: cam4VisualAlignment,
        },
        tool_detection_views: [
          {
            view: "cam_3",
            source_stamp_sec: 1_700_000_000.15,
            sequence: 17,
            model_version: "cam3-rfdetr-tool-v1",
            ontology_version: "thyroid-tools-v1",
            truncated: false,
            detection_status: "detections",
            freshness: cam3Freshness,
            visual_alignment: cam3VisualAlignment,
            instances: [
              {
                tool_id: "T07",
                class_name: "Thyroid retractor",
                confidence: 0.91,
                bbox_xyxy_norm: [0.1, 0.2, 0.5, 0.6],
                center_uv_norm: [0.3, 0.4],
                observation_point_uv_norm: [0.31, 0.39],
                image_region: "middle_left",
                depth_m: 0.83,
              },
            ],
          },
          {
            view: "cam_4",
            source_stamp_sec: 1_700_000_000.25,
            sequence: 18,
            model_version: "cam4-rfdetr-tool-v1",
            ontology_version: "thyroid-tools-v1",
            truncated: false,
            detection_status: "no_detections",
            freshness: cam4Freshness,
            visual_alignment: cam4VisualAlignment,
            instances: [],
          },
        ],
      },
    }),
  };
}

function scenarioRevisionResponse(
  overrides: Partial<Record<
    | "success"
    | "message"
    | "active_bundle"
    | "spec_dir"
    | "active_config_revision"
    | "candidate_config_revision"
    | "changed"
    | "applied"
    | "disposition",
    unknown
  >> = {},
): Record<string, unknown> {
  return {
    success: true,
    message: "bundle preview detected configuration changes",
    active_bundle: "thyroidectomy_v1",
    spec_dir: "/workspace/specs/thyroidectomy_v1",
    active_config_revision: "sha256:11111111111111111111111111111111",
    candidate_config_revision: "sha256:22222222222222222222222222222222",
    changed: true,
    applied: false,
    disposition: "preview_change_available",
    ...overrides,
  };
}

function installRosbridgeStub(page: Page, options: RosbridgeStubOptions = {}) {
  let captureStatusPublishCount = 0;
  let observerImagePublishCount = 0;
  let vlmImagePublishCount = 0;
  let vlmHealthPublishCount = 0;
  let simulationSubscriptionCount = 0;
  const handleSocket = (socket: WebSocketRoute, observerSocket = false) => {
    socket.onMessage((raw) => {
      const message = JSON.parse(typeof raw === "string" ? raw : raw.toString()) as {
        op?: string;
        id?: string;
        service?: string;
        topic?: string;
        args?: Record<string, unknown>;
      };
      if (
        message.op === "subscribe" &&
        message.topic === "/shadow/replay_state" &&
        options.shadowReplayState
      ) {
        options.onShadowSubscription?.();
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: options.shadowReplayStateMessage ?? {
            stamp: { sec: 1, nanosec: 0 },
            run_id: "test-run",
            case_id: "0704_6",
            procedure_id: "thyroidectomy",
            mode: "elastic_demo",
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
            ...options.shadowReplayState,
          },
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/simulation/state" &&
        options.simulationState
      ) {
        options.onSimulationSubscription?.();
        simulationSubscriptionCount += 1;
        if (
          simulationSubscriptionCount <=
          (options.withholdSimulationStateSubscriptions ?? 0)
        ) {
          return;
        }
        const simulationMessage = options.simulationStateMessage ?? {
          procedure_id: "test-procedure",
          active_bundle: "thyroidectomy_v1",
          filtered_phase: "P03",
          instrument_states: [{
            instrument_id: "grasper",
            home_location_type: "rack",
            home_location_id: "grasper",
            location_type: "rack",
            location_id: "grasper",
            owner: "none",
            status: "available",
            confidence: 0.9,
            cleanliness_state: "sterile",
            contaminated: false,
            lifecycle_stage: "home_rack",
            reserved_for: "",
            last_holder: "none",
            next_required_transition: "",
            visual_anchor_id: "grasper",
          }],
          ...options.simulationState,
        };
        const publishSimulationState = () => socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: simulationMessage,
        }));
        publishSimulationState();
        let remainingHeartbeats = Math.max(0, Math.trunc(options.simulationStateHeartbeatCount ?? 0));
        const heartbeatIntervalMs = Math.max(250, options.simulationStateHeartbeatIntervalMs ?? 1_500);
        const publishHeartbeat = () => {
          if (remainingHeartbeats <= 0) return;
          remainingHeartbeats -= 1;
          publishSimulationState();
          if (remainingHeartbeats > 0) setTimeout(publishHeartbeat, heartbeatIntervalMs);
        };
        if (remainingHeartbeats > 0) setTimeout(publishHeartbeat, heartbeatIntervalMs);
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/twin/world_state" &&
        options.worldStateMessage !== undefined
      ) {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: options.worldStateMessage,
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/integration/debug/status" &&
        observerSocket &&
        options.debugStatus
      ) {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: { data: JSON.stringify(options.debugStatus) },
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/vlm/health" &&
        !observerSocket &&
        options.publishVlmHealth &&
        vlmHealthPublishCount < (options.vlmHealthPublishLimit ?? Number.POSITIVE_INFINITY)
      ) {
        vlmHealthPublishCount += 1;
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: options.vlmHealthMessage ?? {
            connected: true,
            healthy: true,
            model_id: "test-vlm",
            image_source: "test-camera",
            latency_sec: 0.2,
            prompt_chars: 0,
            output_chars: 32,
            parse_retry_count: 0,
            last_error: "",
            last_mode: "test",
          },
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/vlm/result" &&
        !observerSocket &&
        options.vlmResultMessage
      ) {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: options.vlmResultMessage,
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/context/vlm_request_context" &&
        options.vlmRequestContextMessage !== undefined
      ) {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: options.vlmRequestContextMessage,
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/skill/status" &&
        !observerSocket &&
        options.skillStatusMessages?.length
      ) {
        for (const status of options.skillStatusMessages) {
          socket.send(JSON.stringify({
            op: "publish",
            topic: message.topic,
            msg: status,
          }));
        }
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/surgery/execution_trace" &&
        !observerSocket &&
        options.executionTraceMessages?.length
      ) {
        for (const trace of options.executionTraceMessages) {
          socket.send(JSON.stringify({
            op: "publish",
            topic: message.topic,
            msg: trace,
          }));
        }
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/surgeon/llm_decision" &&
        !observerSocket &&
        options.surgeonLlmDecision
      ) {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: options.surgeonLlmDecision,
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/input/asr/runtime_status" &&
        (options.liveAsrStatus || options.liveAsrStatusMessage)
      ) {
        const publish = (envelope: Record<string, unknown>) => socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: { data: JSON.stringify(envelope) },
        }));
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            ...(options.liveAsrStatusMessage ?? {
              data: JSON.stringify({
                schema: "taskplanner.asr.status.v1",
                stamp_sec: 1,
                ...options.liveAsrStatusEnvelope,
                asr: options.liveAsrStatus,
              }),
            }),
          },
        }));
        options.onLiveAsrStatusPublisher?.(publish);
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/integration/readiness" &&
        options.publishIntegrationReadiness !== false
      ) {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            ...(options.integrationReadinessMessage ?? {
              data: JSON.stringify({
                schema: "taskplanner.integration_readiness.v1",
                stamp_sec: Date.now() / 1_000,
                ready: true,
                checks: {
                  contract_configuration: true,
                  surgeon_sentence_publisher: true,
                  tool_handover_action_server: true,
                  retraction_command_service: true,
                  perception_input: true,
                },
                missing: [],
                ...options.integrationReadiness,
                details: {
                  active_bundle: "thyroidectomy_v1",
                  procedure_type: "thyroidectomy",
                  robot_endpoint_source: "virtual",
                  retraction_state_machine_suppressed: true,
                  ...(
                    options.integrationReadiness?.details &&
                    typeof options.integrationReadiness.details === "object" &&
                    !Array.isArray(options.integrationReadiness.details)
                      ? options.integrationReadiness.details as Record<string, unknown>
                      : {}
                  ),
                },
              }),
            }),
          },
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/integration/execution_route/state" &&
        options.executionRouteState
      ) {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            data: JSON.stringify({
              schema: "taskplanner.execution_route_state.v1",
              stamp_sec: Date.now() / 1_000,
              revision: 1,
              initialization_revision: 1,
              selected_source: "virtual",
              run_endpoint_source: "",
              initialization_state: "initialized",
              action_server_ready: true,
              retraction_service_ready: true,
              route_control_enabled: true,
              active_request_count: 0,
              require_bed_robot_status: false,
              require_physical_stop_confirmation: false,
              retraction_state_machine_suppressed: true,
              ...options.executionRouteState,
            }),
          },
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === (options.vlmImageTopic ?? "/taskplanner/internal/vlm/model_visual/compressed") &&
        !observerSocket &&
        options.publishVlmImages &&
        vlmImagePublishCount < (options.vlmImagePublishLimit ?? Number.POSITIVE_INFINITY)
      ) {
        vlmImagePublishCount += 1;
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: options.vlmImageMessage ?? {
            header: { frame_id: "test-vlm-field" },
            format: "jpeg",
            data: "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////2wBDAf//////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAH/AP/EABQQAQAAAAAAAAAAAAAAAAAAACD/2gAIAQEAAQUCaf/EABQRAQAAAAAAAAAAAAAAAAAAACD/2gAIAQMBAT8Bcf/EABQRAQAAAAAAAAAAAAAAAAAAACD/2gAIAQIBAT8Bcf/EABQQAQAAAAAAAAAAAAAAAAAAACD/2gAIAQEABj8Ccf/Z",
          },
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/multicam_node/capture_status" &&
        observerSocket &&
        options.publishCaptureStatus &&
        captureStatusPublishCount < (options.captureStatusPublishLimit ?? Number.POSITIVE_INFINITY)
      ) {
        captureStatusPublishCount += 1;
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            ...(options.observerCaptureStatusMessage ?? {
              online_cameras: ["cam_1", "cam_2", "cam_3", "cam_4", "flir"],
              offline_cameras: [],
              all_cameras_online: true,
              uptime_sec: 12,
              cameras: [],
            }),
          },
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/world_anchor_node/status" &&
        observerSocket &&
        options.publishWorldStatus
      ) {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: options.observerWorldStatusMessage ?? {
            data: JSON.stringify({ collecting: false, reference_frame: "map", world_frame: "world", tags: {} }),
          },
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        observerSocket &&
        options.publishObserverImages &&
        message.topic?.startsWith("/synced/") &&
        observerImagePublishCount < (options.observerImagePublishLimit ?? Number.POSITIVE_INFINITY)
      ) {
        observerImagePublishCount += 1;
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            ...(options.observerImageMessage ?? {
              header: { frame_id: "test-camera" },
              format: "png",
              data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
            }),
          },
        }));
        return;
      }
      if (message.op !== "call_service" || !message.id || !message.service) return;
      if (observerSocket) options.onServiceCall?.(message.service);
      else options.onMissionServiceCall?.(message.service);
      if (!observerSocket && message.service === "/simulation/select_bundle") {
        options.onSelectBundleRequest?.(message.args ?? {});
      }
      if (!observerSocket && message.service === "/input/asr/control") {
        options.onLiveAsrControlRequest?.(message.args ?? {});
      }
      if (observerSocket && options.respondToObserverServices === false) return;
      if (!observerSocket && message.service === options.withholdMissionService) return;
      const routeRequest = !observerSocket && message.service === "/integration/execution_route/command";
      if (routeRequest) options.onExecutionRouteCommand?.(message.args ?? {});
      const selectedRouteSources = (() => {
        try {
          const payload = JSON.parse(String(message.args?.payload_json ?? "{}")) as {
            source?: unknown;
            tool_handover_source?: unknown;
            retraction_source?: unknown;
          };
          const legacySource = payload.source === "external" || payload.source === "virtual"
            ? payload.source
            : "virtual";
          return {
            toolHandover: payload.tool_handover_source === "external" || payload.tool_handover_source === "virtual"
              ? payload.tool_handover_source
              : legacySource,
            retraction: payload.retraction_source === "external" || payload.retraction_source === "virtual"
              ? payload.retraction_source
              : legacySource,
          };
        } catch {
          return { toolHandover: "virtual", retraction: "virtual" } as const;
        }
      })();
      const selectBundleRequest = !observerSocket && message.service === "/simulation/select_bundle";
      const liveAsrControlRequest = !observerSocket && message.service === "/input/asr/control";
      const selectBundleArgs = message.args ?? {};
      const selectedBundle = String(selectBundleArgs.bundle_name ?? "thyroidectomy_v1");
      const selectedBundleResponse = typeof options.selectBundleResponse === "function"
        ? options.selectBundleResponse(selectBundleArgs)
        : options.selectBundleResponse;
      const liveAsrControlResponse = typeof options.liveAsrControlResponse === "function"
        ? options.liveAsrControlResponse(message.args ?? {})
        : options.liveAsrControlResponse;
      const values = routeRequest
        ? options.executionRouteCommandResponse ?? {
            accepted: true,
            command_id: "",
            message: "execution route changed and initialized",
            result_json: JSON.stringify({
              schema: "taskplanner.execution_route_state.v1",
              stamp_sec: Date.now() / 1_000,
              revision: 2,
              initialization_revision: 2,
              selected_source: selectedRouteSources.toolHandover,
              retraction_source: selectedRouteSources.retraction,
              run_endpoint_source: "",
              run_retraction_source: "",
              initialization_state: "initialized",
              action_server_ready: true,
              retraction_service_ready: true,
              route_control_enabled: true,
              active_request_count: 0,
              require_bed_robot_status: selectedRouteSources.retraction === "external",
              require_physical_stop_confirmation: selectedRouteSources.retraction === "external",
              retraction_state_machine_suppressed: selectedRouteSources.retraction === "virtual",
              digital_twin_reset: true,
            }),
          }
        : selectBundleRequest
          ? selectedBundleResponse ?? {
              success: true,
              message: `bundle preview found no configuration changes for ${selectedBundle}`,
              active_bundle: "thyroidectomy_v1",
              spec_dir: `/workspace/specs/${selectedBundle}`,
              active_config_revision: "sha256:active000000000000",
              candidate_config_revision: "sha256:active000000000000",
              changed: false,
              applied: false,
              disposition: "preview_unchanged",
            }
        : liveAsrControlRequest
          ? liveAsrControlResponse ?? {
              accepted: true,
              message: "ASR control accepted",
              result_json: "",
            }
        : observerSocket && message.service === "/integration/debug/command"
        ? { accepted: true, command_id: "debug-command", message: "accepted", result_json: "{}" }
        : options.shadowControlStateJson && message.service === "/shadow/control_replay"
        ? { success: true, message: "ok", state_json: options.shadowControlStateJson }
        : message.service === "/surgeon_actor/get_parameters"
          ? { values: [{ type: 1, bool_value: true }] }
        : message.service === "/multicam_observer/rosapi/topics"
          ? options.observerAvailable === false
            ? { topics: [], types: [] }
            : {
                topics: ["/multicam_node/capture_status"],
                types: ["arpa_multicam_msgs/msg/CaptureStatus"],
              }
          : { success: true, message: "ok", model_ids: [] };
      const controlCommand = message.service === "/simulation/control"
        ? String(message.args?.command ?? "")
        : "";
      const responseDelayMs = !observerSocket && controlCommand
        ? Math.max(0, options.missionControlResponseDelayMs?.[controlCommand] ?? 0)
        : 0;
      const sendResponse = () => socket.send(JSON.stringify({
        op: "service_response",
        id: message.id,
        service: message.service,
        result: true,
        values,
      }));
      if (responseDelayMs > 0) {
        setTimeout(sendResponse, responseDelayMs);
      } else {
        sendResponse();
      }
    });
  };
  const observeSocket = (socket: WebSocketRoute) => {
    options.onSocketConnect?.(socket.url());
    return socket;
  };
  return Promise.all([
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9090\/?$/, (socket) => handleSocket(observeSocket(socket))),
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9091(?:\/multicam)?\/?$/, (socket) => handleSocket(observeSocket(socket), true)),
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9099\/?$/, (socket) => handleSocket(observeSocket(socket))),
  ]);
}

async function installRemoteFrontendProxy(
  page: Page,
  remoteOrigin: string,
  expectedBridgeUrl: string,
) {
  let bridgeConnected = false;
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };

  await page.route(`${remoteOrigin}/**`, async (route) => {
    const requestedUrl = new URL(route.request().url());
    if (requestedUrl.pathname === "/api/runtime/status") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) });
      return;
    }
    if (requestedUrl.pathname === "/api/runtime/transition") {
      await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify(runtime) });
      return;
    }
    const localUrl = new URL(`${requestedUrl.pathname}${requestedUrl.search}`, localFrontendBaseUrl);
    const response = await page.request.fetch(localUrl.toString());
    await route.fulfill({ response });
  });
  await page.routeWebSocket(expectedBridgeUrl, (socket) => {
    bridgeConnected = true;
    socket.onMessage((raw) => {
      const message = JSON.parse(typeof raw === "string" ? raw : raw.toString()) as {
        op?: string;
        id?: string;
        service?: string;
        topic?: string;
      };
      if (message.op === "subscribe" && message.topic === "/simulation/state") {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            procedure_id: "test-procedure",
            active_bundle: "thyroidectomy_v1",
            filtered_phase: "P03",
            running: false,
            execution_state: "idle",
            instrument_states: [],
          },
        }));
        return;
      }
      if (message.op !== "call_service" || !message.id || !message.service) return;
      socket.send(JSON.stringify({
        op: "service_response",
        id: message.id,
        service: message.service,
        result: true,
        values: { success: true, message: "ok", model_ids: [] },
      }));
    });
  });

  return () => bridgeConnected;
}

for (const remoteRoute of [
  {
    label: "MagicDNS over HTTPS",
    origin: "https://taskplanner.arpa-tailnet.ts.net:4173",
    expectedBridgeUrl: "wss://taskplanner.arpa-tailnet.ts.net:9091/llm",
  },
  {
    label: "remote IPv6",
    origin: "http://[fd7a:115c:a1e0::42]:4173",
    expectedBridgeUrl: "ws://[fd7a:115c:a1e0::42]:9091/llm",
  },
]) {
  test(`routes ${remoteRoute.label} through the configured path router`, async ({ page }) => {
    const bridgeConnected = await installRemoteFrontendProxy(
      page,
      remoteRoute.origin,
      remoteRoute.expectedBridgeUrl,
    );

    await page.goto(`${remoteRoute.origin}/`);

    await expect.poll(bridgeConnected).toBe(true);
    await expect(page.locator(".runtime-endpoint")).toHaveAttribute("title", remoteRoute.expectedBridgeUrl);
    await expect(page.locator(".runtime-mode-select select")).toHaveValue("llm");
  });
}

for (const recoveryCase of ["no active runtime", "unavailable status service"] as const) {
  test(`starts the displayed default mode with ${recoveryCase}`, async ({ page }) => {
    const runtime: RuntimeStatus = {
      phase: "idle",
      active_mode: null,
      requested_mode: null,
      retryable: false,
    };
    const starting: RuntimeStatus = {
      phase: "starting",
      active_mode: null,
      requested_mode: "live",
      retryable: false,
    };
    const requestedModes: LauncherMode[] = [];

    await installRosbridgeStub(page);
    await page.route("**/api/runtime/status", (route) => {
      if (recoveryCase === "unavailable status service") {
        return route.abort("failed");
      }
      return route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) });
    });
    await page.route("**/api/runtime/transition", async (route) => {
      requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
      await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify(starting) });
    });

    await page.goto("/");
    const modeSelect = page.locator(".runtime-mode-select select");
    await expect(modeSelect).toHaveValue("live");
    if (recoveryCase === "unavailable status service") {
      await expect(page.locator(".runtime-transition-feedback.error")).toBeVisible();
      await page.getByRole("button", { name: "다시 시도" }).click();
    } else {
      await expect(page.locator(".runtime-transition-feedback.error")).toContainText(
        "실행 중인 런타임이 없습니다.",
      );
      await page.getByRole("button", { name: "현재 모드 시작" }).click();
    }

    await expect.poll(() => requestedModes).toEqual(["live"]);
    await expect(modeSelect).toHaveValue("live");
  });
}

test("treats an oversized runtime status response as unavailable without changing mode", async ({ page }) => {
  await installRosbridgeStub(page);
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        phase: "idle",
        active_mode: "llm-surgeon",
        requested_mode: null,
        retryable: false,
        message: "x".repeat(128 * 1024 + 1),
      }),
    }));

  await page.goto("/");
  await expect(page.locator(".runtime-transition-feedback.error")).toContainText(
    "자동 시작 서비스에 연결할 수 없습니다",
  );
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("live");
});

test("shows the authoritative procedure name, target site, and approach in the stage header", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    simulationStateMessage: {
      procedure_id: "thyroidectomy_demo",
      active_bundle: "thyroidectomy_demo",
      filtered_phase: "P03",
      running: false,
      execution_state: "idle",
      instrument_states: [],
      layout_json: JSON.stringify({
        entities: [],
        anchors: [],
        metadata: {
          procedure: { id: "catalog" },
          bundles: [{
            id: "thyroidectomy_demo",
            display_name: "Thyroidectomy",
            display_name_ko: "갑상선절제술(시연)",
            target_site: "Right Lobectomy",
            target_site_ko: "Right Lobectomy",
            approach: "Open",
            approach_ko: "Open",
            default_phase_id: "P03",
            normal_phase_ids: ["P03"],
            interrupt_phase_ids: [],
            phases: [{
              id: "P03",
              display_name: "Central-field dissection",
              display_name_ko: "중앙 수술야 박리",
            }],
            instruments: [],
          }],
        },
      }),
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const stageHeader = page.locator(".stage-header");
  await expect(stageHeader.getByRole("heading", { level: 2 })).toHaveText("갑상선절제술(시연)");
  const procedureDetails = stageHeader.locator('dl[aria-label="수술 정보"]');
  await expect(procedureDetails.locator("dt")).toHaveText(["표적 부위", "접근법"]);
  await expect(procedureDetails.locator("dd")).toHaveText(["Right Lobectomy", "Open"]);
});

test("renders every authoritative rack slot when a procedure has more than ten tools", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const tools = Array.from({ length: 11 }, (_, index) => ({
    id: `T${String(index + 1).padStart(2, "0")}`,
    display_name: `Tool ${index + 1}`,
    home_location_id: `main_tray_slot_${index + 1}`,
    home_location_type: "tray_slot",
  }));
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    simulationStateMessage: {
      procedure_id: "expanded_tools",
      active_bundle: "expanded_tools",
      filtered_phase: "P01",
      running: false,
      execution_state: "idle",
      robot_state: "idle",
      instrument_states: [],
      layout_json: JSON.stringify({
        entities: [],
        anchors: tools.map((tool, index) => ({
          id: tool.home_location_id,
          attached_to: "instrument_rack",
          x: 14.5 + (index % 2) * 9,
          y: 50 + Math.floor(index / 2) * 4,
          label: String(index + 1),
        })),
        metadata: {
          procedure: { id: "expanded_tools", display_name: "Expanded tools" },
          instruments: tools,
          bundles: [{ id: "expanded_tools", display_name: "Expanded tools" }],
        },
      }),
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const slots = page.locator(".rack-slots-layer [data-slot-id]");
  await expect(slots).toHaveCount(11);
  await expect(slots.last()).toHaveAttribute("data-slot-id", "main_tray_slot_11");
  const rackBox = await page.locator('[data-holder-id="rack"]').boundingBox();
  const finalSlotBox = await slots.last().boundingBox();
  expect(rackBox).not.toBeNull();
  expect(finalSlotBox).not.toBeNull();
  expect(finalSlotBox!.y + finalSlotBox!.height).toBeLessThanOrEqual(
    rackBox!.y + rackBox!.height + 1,
  );
});

test("treats the same mode as a no-op after active-mode authority arrives", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  let statusRequests = 0;
  const requestedModes: LauncherMode[] = [];
  let releaseStatus: () => void = () => undefined;
  const statusGate = new Promise<void>((resolve) => {
    releaseStatus = resolve;
  });

  await installRosbridgeStub(page);
  await page.route("**/api/runtime/status", async (route) => {
    statusRequests += 1;
    // React StrictMode may issue the initial status read twice. Hold both
    // reads so the UI remains in its explicitly locked checking state.
    if (statusRequests <= 2) await statusGate;
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) });
  });
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify(runtime) });
  });

  await page.goto("/");
  const modeSelect = page.locator(".runtime-mode-select select");
  await expect(modeSelect).toBeDisabled();
  // Before authority arrives the configured product default remains Live; the
  // delayed server response below is what authoritatively selects LLM.
  await expect(modeSelect).toHaveValue("live");
  await expect(page.getByRole("button", { name: "독립 Debug" })).toHaveCount(0);
  // The bridge is intentionally held closed until runtime authority arrives;
  // surface that as a pending handshake instead of a false transport failure.
  await expect(page.getByText("런타임 확인 중")).toBeVisible();
  releaseStatus();
  await expect(modeSelect).toBeEnabled();
  await expect(modeSelect).toHaveValue("llm");
  const statusRequestsBeforeSelection = statusRequests;
  await modeSelect.selectOption("llm");

  await expect.poll(() => statusRequests).toBeGreaterThan(statusRequestsBeforeSelection);
  expect(requestedModes).toEqual([]);
});

test("opens integrated Debug observation from Live without requesting a runtime transition", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const requestedModes: LauncherMode[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    debugStatus: integratedDebugStatus(),
  });
  await page.route("**/api/runtime/status", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify(runtime),
  }));
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify(runtime) });
  });

  await page.goto("/");
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("live");
  await expect(page.getByRole("button", { name: "수술 시작", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "통합 Debug 관측 열기" }).click();

  await expect(page.locator('[data-slot="debug-workspace"]')).toBeVisible();
  await expect(page).toHaveURL(/\?workspace=debug/);
  expect(requestedModes).toEqual([]);

  await page.getByRole("button", { name: "운영 화면으로" }).click();
  await expect(page.getByRole("heading", { name: "수술실 디지털 트윈" })).toBeVisible();
  await expect.poll(() => requestedModes).toEqual([]);
  await expect.poll(() => new URL(page.url()).searchParams.get("workspace")).toBeNull();
});

test("opens the clean /debug route as integrated observation without requesting a runtime transition", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const requestedModes: LauncherMode[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    debugStatus: integratedDebugStatus(),
  });
  await page.route("**/api/runtime/status", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify(runtime),
  }));
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify(runtime) });
  });

  await page.goto("/debug");
  await expect(page.locator('[data-slot="debug-workspace"]')).toBeVisible();
  await expect(page).toHaveURL(/\/debug$/);
  expect(requestedModes).toEqual([]);

  await page.getByRole("button", { name: "운영 화면으로" }).click();
  await expect(page.getByRole("heading", { name: "수술실 디지털 트윈" })).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
  await expect.poll(() => requestedModes).toEqual([]);
});

test("observes actor policy without mutating it on mount or workspace return", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One lifecycle viewport is enough for the read-only actor-policy guard.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: null,
    retryable: false,
  };
  const missionServices: string[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    onMissionServiceCall: (service) => missionServices.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const actorToggle = page.getByRole("button", { name: "켜짐" });
  await expect(actorToggle).toBeEnabled();
  expect(missionServices.filter((service) => service === "/surgeon_actor/set_parameters")).toEqual([]);
  expect(missionServices).toContain("/surgeon_actor/get_parameters");

  await openLegacyMulticamWorkspace(page);
  await expect(page.getByRole("heading", { name: "멀티캠 관제 콘솔" })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("heading", { name: "수술실 디지털 트윈" })).toBeVisible();
  await expect(page.getByRole("button", { name: "켜짐" })).toBeEnabled();
  expect(missionServices.filter((service) => service === "/surgeon_actor/set_parameters")).toEqual([]);

  await page.getByRole("button", { name: "켜짐" }).click();
  await expect.poll(() =>
    missionServices.filter((service) => service === "/surgeon_actor/set_parameters").length,
  ).toBe(1);
});

test("uses the active runtime instead of a stale stored mode", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "replay",
    requested_mode: "replay",
    retryable: false,
  };
  await page.addInitScript(() => {
    for (const suffix of ["live", "llm", "shadow", "debug"]) {
      window.localStorage.setItem(`taskplanner.runtimeMode.${suffix}`, "llm");
    }
  });
  const openedSockets: string[] = [];
  await installRosbridgeStub(page, { onSocketConnect: (url) => openedSockets.push(url) });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");

  await expect(page.locator(".runtime-mode-select select")).toHaveValue("shadow");
  await expect.poll(() => page.evaluate(() =>
    Object.entries(window.localStorage).some(
      ([key, value]) => key.startsWith("taskplanner.runtimeMode.") && value === "shadow",
    ),
  )).toBe(true);
  await expect.poll(() => openedSockets).toContain("ws://127.0.0.1:9099/");
  expect(openedSockets.filter((url) => url === "ws://127.0.0.1:9090/")).toEqual([]);
});

test("shows the requested mode while keeping the active ROS endpoint during startup", async ({ page }) => {
  let transitionAccepted = false;
  const idle: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const starting: RuntimeStatus = {
    phase: "starting",
    active_mode: "llm-surgeon",
    requested_mode: "replay",
    retryable: false,
  };

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(transitionAccepted ? starting : idle),
    }));
  await page.route("**/api/runtime/transition", async (route) => {
    transitionAccepted = true;
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify(starting) });
  });

  await page.goto("/");
  await page.locator(".runtime-mode-select select").selectOption("shadow");

  await expect(page.locator(".runtime-mode-select select")).toHaveValue("shadow");
  await expect(page.locator(".runtime-transition-feedback.pending")).toContainText("리플레이 (Shadow) 모드");
  await expect(page.locator(".runtime-endpoint")).toHaveAttribute("title", "ws://127.0.0.1:9090");
});

test("keeps runtime transitions single-flight while the controller is starting", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const starting: RuntimeStatus = {
    phase: "starting",
    active_mode: "llm-surgeon",
    requested_mode: "replay",
    retryable: false,
  };
  const requestedModes: LauncherMode[] = [];
  let releaseTransition: () => void = () => undefined;
  const transitionGate = new Promise<void>((resolve) => {
    releaseTransition = resolve;
  });

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await transitionGate;
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify(starting) });
  });

  await page.goto("/");
  const modeSelect = page.locator(".runtime-mode-select select");
  await modeSelect.selectOption("shadow");
  await expect.poll(() => requestedModes).toEqual(["replay"]);
  // The controller request is still unresolved while the status poll is
  // allowed to return a stale idle snapshot. That snapshot must not release
  // the transition lock or hide the requested mode.
  await page.waitForTimeout(1600);
  await expect(page.locator(".runtime-transition-feedback.pending")).toBeVisible();
  await expect(modeSelect).toBeDisabled();
  await modeSelect.evaluate((element) => {
    element.removeAttribute("disabled");
    (element as HTMLSelectElement).value = "live";
    element.dispatchEvent(new Event("change", { bubbles: true }));
    element.setAttribute("disabled", "");
  });
  await page.waitForTimeout(150);
  expect(requestedModes).toEqual(["replay"]);
  await page.waitForTimeout(10_500);
  await expect(page.locator(".runtime-transition-feedback.pending")).toContainText(/기동 응답을 \d+초째 기다리는 중/);
  await page.setViewportSize({ width: 390, height: 844 });
  const pendingBounds = await page.locator(".runtime-transition-feedback.pending").evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return { left: bounds.left, right: bounds.right, viewport: window.innerWidth };
  });
  expect(pendingBounds.left).toBeGreaterThanOrEqual(-1);
  expect(pendingBounds.right).toBeLessThanOrEqual(pendingBounds.viewport + 1);
  releaseTransition();
});

test("reconciles a lost transition response without allowing a duplicate request", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One timed admission path is enough for the bounded response guard.");
  const idle: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const starting: RuntimeStatus = {
    phase: "starting",
    active_mode: "llm-surgeon",
    requested_mode: "replay",
    retryable: false,
  };
  const requestedModes: LauncherMode[] = [];
  let transitionAdmitted = false;
  let releaseTransition: () => void = () => undefined;
  const transitionGate = new Promise<void>((resolve) => {
    releaseTransition = resolve;
  });

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(transitionAdmitted ? starting : idle),
    }));
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    transitionAdmitted = true;
    await transitionGate;
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify(starting) });
  });

  await page.goto("/");
  const modeSelect = page.locator(".runtime-mode-select select");
  await modeSelect.selectOption("shadow");
  await expect.poll(() => requestedModes).toEqual(["replay"]);
  await expect(page.locator(".runtime-transition-feedback.pending")).toBeVisible();

  // The POST response stays missing past the 15-second client bound. The
  // immediate status reconciliation must recover the host's starting state
  // without unlocking the selector or issuing another POST.
  await page.waitForTimeout(15_500);
  await expect(page.locator(".runtime-transition-feedback.pending")).toContainText(
    "리플레이 (Shadow) 모드 시작 중입니다",
  );
  await expect(modeSelect).toBeDisabled();
  expect(requestedModes).toEqual(["replay"]);
  releaseTransition();
});

test("locks runtime switching while a run is active", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const requestedModes: LauncherMode[] = [];
  let simulationSubscribed = false;

  await installRosbridgeStub(page, {
    simulationState: { running: true, execution_state: "running" },
    onSimulationSubscription: () => {
      simulationSubscribed = true;
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify(runtime) });
  });

  await page.goto("/");
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("live");
  await expect.poll(() => simulationSubscribed).toBe(true);
  await expect(page.locator(".runtime-mode-select select")).toBeDisabled();
  await expect(page.locator("#runtime-mode-lock-note")).toContainText("먼저 실행을 정지");
  await expect(page.getByRole("button", { name: "독립 Debug" })).toHaveCount(0);
  await page.waitForTimeout(100);
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("live");
  await expect(page.locator(".mission-layout")).toBeVisible();
  expect(requestedModes).toEqual([]);
});

test("locks all runtime entry points while a control service is pending", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const requestedModes: LauncherMode[] = [];
  const missionServiceCalls: string[] = [];

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    withholdMissionService: "/simulation/control",
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify(runtime) });
  });

  await page.goto("/");
  const startButton = page.getByRole("button", { name: "수술 시작", exact: true });
  await startButton.click();
  await expect(page.locator(".dock-action-message.pending")).toContainText("Starting simulation");
  await startButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForTimeout(100);
  expect(missionServiceCalls.filter((service) => service === "/simulation/control")).toHaveLength(1);
  const modeSelect = page.locator(".runtime-mode-select select");
  await expect(modeSelect).toBeDisabled();
  await expect(page.getByRole("button", { name: "독립 Debug" })).toHaveCount(0);
  await expect(page.locator("#runtime-mode-lock-note")).toContainText("제어 요청의 결과");

  await modeSelect.evaluate((select) => {
    select.removeAttribute("disabled");
    (select as HTMLSelectElement).value = "debug";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await page.waitForTimeout(150);

  await expect(page.locator(".mission-layout")).toBeVisible();
  expect(requestedModes).toEqual([]);
});

test("keeps bundle revision previews single-flight while the read-only service call is pending", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    withholdMissionService: "/simulation/select_bundle",
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const bundleSelect = page.locator(".control-stack select").nth(1);
  await expect(bundleSelect).toBeEnabled();
  const bundleValues = await bundleSelect.locator("option").evaluateAll((options) =>
    options.map((option) => (option as HTMLOptionElement).value),
  );
  expect(bundleValues.length).toBeGreaterThan(0);
  const previewButton = page.getByRole("button", { name: "변경 확인", exact: true });
  await previewButton.click();
  await expect.poll(() =>
    missionServiceCalls.filter((service) => service === "/simulation/select_bundle").length,
  ).toBe(1);
  await expect(page.locator('[data-slot="scenario-revision-control"]')).toHaveAttribute(
    "data-phase",
    "previewing",
  );
  await previewButton.evaluate((element) => {
    element.removeAttribute("disabled");
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForTimeout(100);

  expect(missionServiceCalls.filter((service) => service === "/simulation/select_bundle")).toHaveLength(1);
});

test("keeps revision preview available while Live is running without sending an apply request", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const requests: Record<string, unknown>[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: true, execution_state: "running" },
    onSelectBundleRequest: (args) => requests.push(args),
    selectBundleResponse: scenarioRevisionResponse(),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const bundleSelect = page.locator(".control-stack label").filter({ hasText: "수술" }).locator("select");
  await expect(bundleSelect).toBeEnabled();
  await page.getByRole("button", { name: "변경 확인", exact: true }).click();

  await expect.poll(() => requests).toHaveLength(1);
  expect(requests[0]).toEqual({
    bundle_name: "thyroidectomy_v1",
    restart_if_running: false,
    preview_only: true,
    reload_if_changed: false,
    expected_candidate_revision: "",
  });
  const revision = page.locator('[data-slot="scenario-revision-control"]');
  await expect(revision).toHaveAttribute("data-phase", "previewed");
  await expect(revision).toContainText("11111111111111…");
  await expect(revision).toContainText("22222222222222…");
  await expect(revision).toContainText("변경 있음");
  await expect(page.getByRole("button", { name: "revision 적용", exact: true })).toBeDisabled();
  await expect(revision).toContainText("현재 번들의 reload는 진행 상태를 초기화");
});

test("keeps Live start admission bound to the active bundle while selecting a revision candidate", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    simulationStateHeartbeatCount: 20,
    simulationStateHeartbeatIntervalMs: 250,
    simulationStateMessage: {
      procedure_id: "thyroidectomy_v1",
      active_bundle: "thyroidectomy_v1",
      filtered_phase: "P03",
      running: false,
      execution_state: "idle",
      robot_state: "idle",
      instrument_states: [{
        instrument_id: "T01",
        home_location_type: "tray_slot",
        home_location_id: "main_tray_slot_1",
        location_type: "tray_slot",
        location_id: "main_tray_slot_1",
        owner: "none",
        status: "available",
        confidence: 1,
        cleanliness_state: "sterile",
        contaminated: false,
        lifecycle_stage: "home_rack",
        reserved_for: "",
        last_holder: "none",
        next_required_transition: "",
        visual_anchor_id: "main_tray_slot_1",
      }],
      layout_json: JSON.stringify({
        entities: [],
        anchors: [{
          id: "main_tray_slot_1",
          attached_to: "instrument_rack",
          x: 14.5,
          y: 50,
          label: "1",
        }],
        metadata: {
          procedure: { id: "thyroidectomy_v1" },
          instruments: [{
            id: "T01",
            display_name: "Scalpel",
            home_location_id: "main_tray_slot_1",
            home_location_type: "tray_slot",
          }],
          bundles: [
            { id: "thyroidectomy_v1", display_name_ko: "갑상선절제술" },
            { id: "inguinal_hernia_demo", display_name_ko: "서혜부 탈장" },
          ],
        },
      }),
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const startButton = page.getByRole("button", { name: "수술 시작", exact: true });
  await expect(startButton).toBeEnabled();
  const bundleSelect = page.locator(".control-stack label").filter({ hasText: "수술" }).locator("select");
  await bundleSelect.selectOption("inguinal_hernia_demo");

  await expect(startButton).toBeEnabled();
  await expect(page.locator('[data-slot="integration-readiness"]')).not.toContainText("번들 불일치");
  expect(missionServiceCalls.filter(
    (service) => service === "/simulation/select_bundle" || service === "/simulation/control",
  )).toEqual([]);
});

test("applies a stopped Live bundle only after preview with the additive service contract", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const requests: Record<string, unknown>[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    onSelectBundleRequest: (args) => requests.push(args),
    selectBundleResponse: (args) => Boolean(args.preview_only)
      ? scenarioRevisionResponse()
      : scenarioRevisionResponse({
          message: "bundle revision applied",
          active_config_revision: "sha256:22222222222222222222222222222222",
          applied: true,
          disposition: "applied",
        }),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await page.getByRole("button", { name: "변경 확인", exact: true }).click();
  const applyButton = page.getByRole("button", { name: "revision 적용", exact: true });
  await expect(applyButton).toBeEnabled();
  await applyButton.click();

  await expect.poll(() => requests).toHaveLength(2);
  expect(requests[1]).toEqual({
    bundle_name: "thyroidectomy_v1",
    restart_if_running: false,
    preview_only: false,
    reload_if_changed: true,
    expected_candidate_revision: "sha256:22222222222222222222222222222222",
  });
  const revision = page.locator('[data-slot="scenario-revision-control"]');
  await expect(revision).toHaveAttribute("data-phase", "applied");
  await expect(revision).toContainText("bundle revision applied");
});

test("keeps same-bundle reload locked while paused and makes the deferred contract explicit", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const requests: Record<string, unknown>[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: true, execution_state: "paused" },
    onSelectBundleRequest: (args) => requests.push(args),
    selectBundleResponse: scenarioRevisionResponse(),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await page.getByRole("button", { name: "변경 확인", exact: true }).click();
  const applyButton = page.getByRole("button", { name: "revision 적용", exact: true });
  await expect(applyButton).toBeDisabled();
  await expect(page.locator('[data-slot="scenario-revision-control"]')).toContainText(
    "현재 번들의 reload는 진행 상태를 초기화합니다. 시나리오를 완전히 정지",
  );
  await applyButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForTimeout(100);
  expect(requests).toHaveLength(1);
});

test("uses an explicit paused restart contract for a different non-Live bundle", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const requests: Record<string, unknown>[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: true, execution_state: "paused" },
    simulationStateMessage: {
      procedure_id: "thyroidectomy_v1",
      active_bundle: "thyroidectomy_v1",
      filtered_phase: "P03",
      running: true,
      execution_state: "paused",
      robot_state: "idle",
      instrument_states: [],
      layout_json: JSON.stringify({
        entities: [],
        anchors: [],
        metadata: {
          procedure: { id: "catalog" },
          bundles: [
            { id: "thyroidectomy_v1", display_name_ko: "갑상선절제술" },
            { id: "inguinal_hernia_demo", display_name_ko: "서혜부 탈장" },
          ],
        },
      }),
    },
    onSelectBundleRequest: (args) => requests.push(args),
    selectBundleResponse: (args) => Boolean(args.preview_only)
      ? scenarioRevisionResponse({
          spec_dir: "/workspace/specs/inguinal_hernia_demo",
        })
      : scenarioRevisionResponse({
          message: "paused scenario changed and restarted",
          active_bundle: "inguinal_hernia_demo",
          spec_dir: "/workspace/specs/inguinal_hernia_demo",
          active_config_revision: "sha256:22222222222222222222222222222222",
          applied: true,
          disposition: "applied",
        }),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const stageHeading = page.locator(".stage-header").getByRole("heading", { level: 2 });
  await expect(stageHeading).toHaveText("갑상선절제술");
  const bundleSelect = page.locator(".control-stack label").filter({ hasText: "수술" }).locator("select");
  await bundleSelect.selectOption("inguinal_hernia_demo");
  await page.getByRole("button", { name: "변경 확인", exact: true }).click();
  const applyButton = page.getByRole("button", { name: "revision 적용", exact: true });
  await expect(applyButton).toBeEnabled();
  await applyButton.click();

  await expect.poll(() => requests).toHaveLength(2);
  expect(requests[1]).toEqual({
    bundle_name: "inguinal_hernia_demo",
    restart_if_running: true,
    preview_only: false,
    reload_if_changed: false,
    expected_candidate_revision: "sha256:22222222222222222222222222222222",
  });
  await expect(page.locator('[data-slot="scenario-revision-control"]')).toContainText(
    "paused scenario changed and restarted",
  );
  await expect(stageHeading).toHaveText("갑상선절제술");
});

test("surfaces a backend bundle rejection without claiming that the candidate became active", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const rejection = "live bundle reload requires a fully stopped safe runtime: executor still settling";
  const requests: Record<string, unknown>[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    onSelectBundleRequest: (args) => requests.push(args),
    selectBundleResponse: (args) => Boolean(args.preview_only)
      ? scenarioRevisionResponse()
      : scenarioRevisionResponse({
          success: false,
          message: rejection,
          disposition: "deferred",
        }),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await page.getByRole("button", { name: "변경 확인", exact: true }).click();
  await page.getByRole("button", { name: "revision 적용", exact: true }).click();

  const revision = page.locator('[data-slot="scenario-revision-control"]');
  await expect(revision).toHaveAttribute("data-phase", "failed");
  await expect(revision.getByRole("alert")).toContainText(rejection);
  await expect(revision.locator(".scenario-revision-summary")).toContainText("thyroidectomy_v1");
  expect(requests).toHaveLength(2);
});

test("rejects an oversized scenario revision response before it reaches apply authority", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const requests: Record<string, unknown>[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    onSelectBundleRequest: (args) => requests.push(args),
    selectBundleResponse: scenarioRevisionResponse({
      candidate_config_revision: `sha256:${"f".repeat(300)}`,
    }),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await page.getByRole("button", { name: "변경 확인", exact: true }).click();

  const revision = page.locator('[data-slot="scenario-revision-control"]');
  await expect(revision).toHaveAttribute("data-phase", "failed");
  await expect(revision.getByRole("alert")).toContainText(
    "Scenario revision response field candidate_config_revision is invalid.",
  );
  await expect(page.getByRole("button", { name: "revision 적용", exact: true })).toBeDisabled();
  expect(requests).toHaveLength(1);
});

test("keeps a preview for a different active bundle non-authoritative", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const requests: Record<string, unknown>[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    onSelectBundleRequest: (args) => requests.push(args),
    selectBundleResponse: scenarioRevisionResponse({
      active_bundle: "stale_server_bundle",
    }),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await page.getByRole("button", { name: "변경 확인", exact: true }).click();

  const revision = page.locator('[data-slot="scenario-revision-control"]');
  await expect(revision).toHaveAttribute("data-phase", "previewed");
  await expect(page.getByRole("button", { name: "revision 적용", exact: true })).toBeDisabled();
  await expect(revision.locator(".scenario-revision-summary")).not.toContainText(
    "22222222222222…",
  );
  expect(requests).toHaveLength(1);
});

test("disables revision apply as soon as the authoritative state age is stale", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const requests: Record<string, unknown>[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    onSelectBundleRequest: (args) => requests.push(args),
    selectBundleResponse: scenarioRevisionResponse(),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await page.getByRole("button", { name: "변경 확인", exact: true }).click();
  const applyButton = page.getByRole("button", { name: "revision 적용", exact: true });
  await expect(applyButton).toBeEnabled();

  // Force a render in the narrow interval where the socket is still marked
  // connected but the authoritative frame is already older than its bound.
  await page.evaluate(() => {
    const originalNow = Date.now;
    Date.now = () => originalNow() + 5_000;
  });
  await page.getByRole("button", { name: "English", exact: true }).click();
  const englishApplyButton = page.getByRole("button", { name: "Apply revision", exact: true });
  await expect(englishApplyButton).toBeDisabled();
  await expect(page.locator('[data-slot="scenario-revision-control"]')).toContainText(
    "Wait for a fresh server runtime state before applying changes.",
  );
  await englishApplyButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForTimeout(100);
  expect(requests).toHaveLength(1);
});

test("keeps live ASR controls single-flight while the service admission is pending", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    liveAsrStatus: {
      available: true,
      state: "IDLE",
      connected: false,
      recording_active: false,
      devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16000, default: true }],
      device_id: 7,
      device_name: "Test USB microphone",
      device_status: "READY",
      device_message: "ready",
      route_policy: "cloud",
      lan_health: { state: "UNKNOWN", age_ms: null, latency_ms: null },
    },
    withholdMissionService: "/input/asr/control",
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const startButton = page.getByRole("button", { name: "ASR 시작" });
  await expect(startButton).toBeEnabled();
  await startButton.click();
  await expect.poll(() =>
    missionServiceCalls.filter((service) => service === "/input/asr/control").length,
  ).toBe(1);
  await page.evaluate(() => {
    const button = Array.from(document.querySelectorAll("button"))
      .find((candidate) => candidate.textContent?.includes("ASR 시작"));
    if (!button) throw new Error("ASR start button not found");
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    button.setAttribute("disabled", "");
  });
  await page.waitForTimeout(100);

  expect(missionServiceCalls.filter((service) => service === "/input/asr/control")).toHaveLength(1);
  await expect(startButton).toBeDisabled();
});

test("restarts only the ASR node and restores capture plus recording after new source proof", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One ASR hot-restart workflow run is sufficient.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const oldInstanceId = "11111111-1111-4111-8111-111111111111";
  const newInstanceId = "22222222-2222-4222-8222-222222222222";
  const oldRevision = "a".repeat(64);
  const newRevision = "b".repeat(64);
  const asrControls: Record<string, unknown>[] = [];
  let publishAsrStatus: ((envelope: Record<string, unknown>) => void) | null = null;
  let restartPosted = false;
  let restartPostCount = 0;
  let restartStatusReads = 0;
  let restartedAtMs = 0;
  let restartRequestId = "";
  const asrSnapshot = (overrides: Record<string, unknown> = {}) => ({
    available: true,
    state: "STOPPED",
    topic: "/sensors/surgeon/utterance",
    output_mode: "typed_utterance",
    output_topic: "/sensors/surgeon/utterance",
    devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16_000, default: true }],
    device_id: 7,
    device_name: "Test USB microphone",
    device_status: "READY",
    device_message: "ready",
    route_policy: "auto",
    endpoint_id: "lan",
    lan_health: { state: "READY", age_ms: 10, latency_ms: 2 },
    connected: false,
    recording_active: false,
    ...overrides,
  });
  const newEnvelope = (asr: Record<string, unknown>) => ({
    schema: "taskplanner.asr.status.v1",
    stamp_sec: Date.now() / 1_000,
    node_instance_id: newInstanceId,
    node_started_at_sec: restartedAtMs / 1_000,
    source_revision: newRevision,
    asr,
  });

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    liveAsrStatus: asrSnapshot({ state: "LISTENING", connected: true, recording_active: true }),
    liveAsrStatusEnvelope: {
      node_instance_id: oldInstanceId,
      node_started_at_sec: 1_700_000_000,
      source_revision: oldRevision,
    },
    onLiveAsrStatusPublisher: (publish) => { publishAsrStatus = publish; },
    onLiveAsrControlRequest: (args) => asrControls.push(args),
    liveAsrControlResponse: (args) => {
      const operation = String(args.operation ?? "");
      const state = operation === "set_route_policy" ? "STOPPED" : "LISTENING";
      const connected = operation !== "set_route_policy";
      const recordingActive = operation === "start_recording";
      return {
        accepted: true,
        message: `${operation} accepted`,
        result_json: JSON.stringify(newEnvelope(asrSnapshot({
          state,
          connected,
          recording_active: recordingActive,
        }))),
      };
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/asr/restart", async (route) => {
    restartPostCount += 1;
    expect(route.request().method()).toBe("POST");
    expect(route.request().postData()).toBe("{}");
    restartRequestId = route.request().headers()["x-taskplanner-request-id"] ?? "";
    expect(restartRequestId).toMatch(asrRestartRequestIdPattern);
    restartPosted = true;
    restartedAtMs = Date.now();
    setTimeout(() => publishAsrStatus?.(newEnvelope(asrSnapshot())), 80);
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        accepted: true,
        phase: "queued",
        generation: 4,
        job_id: "asr-job-4",
        request_id: restartRequestId,
        message: "queued",
        retryable: false,
        source_revision: newRevision,
        container_started_at: null,
        before_pid: null,
        after_pid: null,
      }),
    });
  });
  await page.route("**/api/runtime/asr/status", (route) => {
    const phase = restartPosted
      ? ++restartStatusReads < 2 ? "verifying" : "succeeded"
      : "idle";
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        phase,
        generation: restartPosted ? 4 : 3,
        job_id: restartPosted ? "asr-job-4" : null,
        request_id: restartPosted ? restartRequestId : null,
        message: phase,
        retryable: false,
        source_revision: restartPosted ? newRevision : oldRevision,
        container_started_at: restartPosted ? new Date(restartedAtMs).toISOString() : null,
        before_pid: restartPosted ? 101 : null,
        after_pid: phase === "succeeded" ? 202 : null,
      }),
    });
  });

  await page.goto("/");
  const restartButton = page.getByRole("button", { name: "ASR 노드 새로 시작", exact: true });
  await expect(restartButton).toBeEnabled();
  await restartButton.click();
  await expect(page.locator(".live-asr-state")).toContainText("ASR 노드 다시 시작 중");
  await expect(page.locator(".live-asr-actions button").first()).toBeDisabled();

  // Even a forced stale DOM event cannot admit a duplicate restart POST.
  await restartButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    button.setAttribute("disabled", "");
  });
  await expect.poll(() => restartPostCount).toBe(1);
  await expect(page.locator(".live-asr-message")).toContainText(
    "ASR 노드 새로 시작 완료 · 코드 bbbbbbbb · 마이크 캡처 복원됨 · 녹화는 새 세그먼트로 복원됨",
    { timeout: 10_000 },
  );
  expect(asrControls.map((request) => request.operation)).toEqual([
    "set_route_policy",
    "start",
    "start_recording",
  ]);
  expect(asrControls[0]?.route_policy).toBe("auto");
  expect(asrControls[1]?.device_id).toBe(7);
  await expect(page.locator(".live-asr-runtime-revision")).toContainText("코드 bbbbbbbb");
  await expect(restartButton).toBeEnabled();
});

test("does not reopen ASR after uncertain second restart repeats a succeeded baseline", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One concurrent ASR control CAS run is sufficient.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  // Legacy status can lack a canonical UUID. Empty IDs remain the old process
  // for CAS matching until the restarted node publishes its first UUID.
  const oldInstanceId = "";
  const newInstanceId = "34343434-3434-4434-8434-343434343434";
  const oldRevision = "2".repeat(64);
  const newRevision = "3".repeat(64);
  const asrControls: Record<string, unknown>[] = [];
  let publishAsrStatus: ((envelope: Record<string, unknown>) => void) | null = null;
  let restartPosted = false;
  let restartStatusReads = 0;
  let containerStartedAtMs = 0;
  let restartRequestId = "";
  const previousRequestId = "05050505-0505-4505-8505-050505050505";
  const asrSnapshot = (overrides: Record<string, unknown> = {}) => ({
    available: true,
    state: "STOPPED",
    topic: "/sensors/surgeon/utterance",
    output_mode: "typed_utterance",
    output_topic: "/sensors/surgeon/utterance",
    devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16_000, default: true }],
    device_id: 7,
    device_name: "Test USB microphone",
    device_status: "READY",
    route_policy: "auto",
    endpoint_id: "lan",
    lan_health: { state: "READY", age_ms: 10, latency_ms: 2 },
    connected: false,
    recording_active: false,
    ...overrides,
  });
  const envelope = (
    nodeInstanceId: string,
    sourceRevision: string,
    nodeStartedAtSec: number,
    asr: Record<string, unknown>,
  ) => ({
    schema: "taskplanner.asr.status.v1",
    stamp_sec: Date.now() / 1_000,
    node_instance_id: nodeInstanceId,
    node_started_at_sec: nodeStartedAtSec,
    source_revision: sourceRevision,
    asr,
  });

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    liveAsrStatus: asrSnapshot({ state: "LISTENING", connected: true, recording_active: true }),
    liveAsrStatusEnvelope: {
      node_instance_id: oldInstanceId,
      node_started_at_sec: 1_700_000_000,
      source_revision: oldRevision,
    },
    onLiveAsrStatusPublisher: (publish) => { publishAsrStatus = publish; },
    onLiveAsrControlRequest: (args) => asrControls.push(args),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/asr/restart", async (route) => {
    expect(route.request().postData()).toBe("{}");
    restartRequestId = route.request().headers()["x-taskplanner-request-id"] ?? "";
    expect(restartRequestId).toMatch(asrRestartRequestIdPattern);
    restartPosted = true;
    containerStartedAtMs = Date.now();
    setTimeout(() => publishAsrStatus?.(envelope(
      oldInstanceId,
      oldRevision,
      1_700_000_000,
      asrSnapshot({ state: "STOPPED", connected: false, recording_active: false }),
    )), 50);
    setTimeout(() => publishAsrStatus?.(envelope(
      newInstanceId,
      newRevision,
      (containerStartedAtMs + 500) / 1_000,
      asrSnapshot({ state: "STOPPED", route_policy: "cloud" }),
    )), 950);
    await route.abort("failed");
  });
  await page.route("**/api/runtime/asr/status", (route) => {
    let phase = "succeeded";
    if (restartPosted) {
      restartStatusReads += 1;
      phase = restartStatusReads === 1
        ? "succeeded"
        : restartStatusReads <= 3
        ? "preflighting"
        : restartStatusReads === 4 ? "restarting" : "succeeded";
    }
    const targetSnapshot = restartPosted && restartStatusReads > 1;
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        phase,
        generation: targetSnapshot ? 6 : 5,
        job_id: targetSnapshot ? "asr-job-6" : "asr-job-5",
        request_id: targetSnapshot ? restartRequestId : previousRequestId,
        message: phase,
        retryable: false,
        source_revision: targetSnapshot ? newRevision : oldRevision,
        container_started_at: targetSnapshot ? new Date(containerStartedAtMs).toISOString() : null,
        before_pid: targetSnapshot ? 111 : null,
        after_pid: targetSnapshot && phase === "succeeded" ? 222 : null,
      }),
    });
  });

  await page.goto("/");
  const restartButton = page.getByRole("button", { name: "ASR 노드 새로 시작", exact: true });
  await restartButton.click();
  await expect(page.locator(".live-asr-message")).toContainText(
    "다른 ASR 제어를 감지해 이전 경로·마이크·녹음을 복원하지 않음",
    { timeout: 10_000 },
  );
  expect(asrControls).toEqual([]);
  await expect(restartButton).toBeEnabled();
});

test("reconciles an uncertain ASR restart response without repeating the POST", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One uncertain-response workflow run is sufficient.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const sourceRevision = "c".repeat(64);
  const asrControls: Record<string, unknown>[] = [];
  let publishAsrStatus: ((envelope: Record<string, unknown>) => void) | null = null;
  let restartPosted = false;
  let restartPostCount = 0;
  let restartStatusReads = 0;
  let hostContainerStartedAtMs = 0;
  let restartRequestId = "";
  const foreignRequestId = "fafafafa-fafa-4afa-8afa-fafafafafafa";
  const restartedEnvelope = () => ({
    schema: "taskplanner.asr.status.v1",
    stamp_sec: hostContainerStartedAtMs / 1_000,
    node_instance_id: "44444444-4444-4444-8444-444444444444",
    node_started_at_sec: (hostContainerStartedAtMs + 250) / 1_000,
    source_revision: sourceRevision,
    asr: {
      available: true,
      state: "STOPPED",
      topic: "/sensors/surgeon/utterance",
      output_mode: "typed_utterance",
      output_topic: "/sensors/surgeon/utterance",
      devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16_000, default: true }],
      device_id: 7,
      device_name: "Test USB microphone",
      device_status: "READY",
      route_policy: "cloud",
      connected: false,
      recording_active: false,
      lan_health: { state: "UNKNOWN", age_ms: null, latency_ms: null },
    },
  });
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    liveAsrStatus: {
      available: true,
      state: "STOPPED",
      topic: "/sensors/surgeon/utterance",
      output_mode: "typed_utterance",
      output_topic: "/sensors/surgeon/utterance",
      devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16_000, default: true }],
      device_id: 7,
      device_name: "Test USB microphone",
      device_status: "READY",
      route_policy: "cloud",
      connected: false,
      recording_active: false,
      lan_health: { state: "UNKNOWN", age_ms: null, latency_ms: null },
    },
    liveAsrStatusEnvelope: {
      node_instance_id: "33333333-3333-4333-8333-333333333333",
      node_started_at_sec: 1_700_000_000,
      source_revision: "d".repeat(64),
    },
    onLiveAsrStatusPublisher: (publish) => { publishAsrStatus = publish; },
    onLiveAsrControlRequest: (args) => asrControls.push(args),
    liveAsrControlResponse: () => ({
      accepted: true,
      message: "route policy restored",
      result_json: JSON.stringify(restartedEnvelope()),
    }),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/asr/restart", async (route) => {
    restartPostCount += 1;
    expect(route.request().postData()).toBe("{}");
    restartRequestId = route.request().headers()["x-taskplanner-request-id"] ?? "";
    expect(restartRequestId).toMatch(asrRestartRequestIdPattern);
    restartPosted = true;
    // The ASR host clock is deliberately five minutes ahead of the browser.
    // Provenance must compare the host's Docker and node timestamps only.
    hostContainerStartedAtMs = Date.now() + 5 * 60_000;
    setTimeout(() => publishAsrStatus?.(restartedEnvelope()), 80);
    await route.abort("failed");
  });
  await page.route("**/api/runtime/asr/status", (route) => {
    if (restartPosted) restartStatusReads += 1;
    const foreignSnapshot = restartPosted && restartStatusReads === 1;
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        phase: !restartPosted ? "idle" : foreignSnapshot ? "queued" : "succeeded",
        generation: !restartPosted ? 8 : foreignSnapshot ? 9 : 10,
        job_id: !restartPosted ? null : foreignSnapshot ? "asr-job-foreign" : "asr-job-10",
        request_id: !restartPosted ? null : foreignSnapshot ? foreignRequestId : restartRequestId,
        message: !restartPosted ? "idle" : foreignSnapshot ? "foreign queued" : "succeeded",
        retryable: false,
        source_revision: sourceRevision,
        container_started_at: restartPosted ? new Date(hostContainerStartedAtMs).toISOString() : null,
        before_pid: restartPosted ? 303 : null,
        after_pid: restartPosted && !foreignSnapshot ? 404 : null,
      }),
    });
  });

  await page.goto("/");
  const restartButton = page.getByRole("button", { name: "ASR 노드 새로 시작", exact: true });
  await restartButton.click();
  await expect(page.locator(".live-asr-message")).toContainText(
    "ASR 노드 새로 시작 완료 · 코드 cccccccc",
    { timeout: 10_000 },
  );
  expect(restartPostCount).toBe(1);
  expect(restartStatusReads).toBeGreaterThanOrEqual(2);
  expect(asrControls.map((request) => request.operation)).toEqual(["set_route_policy"]);
  expect(asrControls[0]?.route_policy).toBe("cloud");
  await expect(restartButton).toBeEnabled();
});

test("refuses capture restore when host and ASR source revisions differ", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One source-proof rejection run is sufficient.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const heartbeatRevision = "e".repeat(64);
  const hostRevision = "f".repeat(64);
  const asrControls: Record<string, unknown>[] = [];
  let publishAsrStatus: ((envelope: Record<string, unknown>) => void) | null = null;
  let restartPosted = false;
  let restartedAtMs = 0;
  let restartRequestId = "";
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    liveAsrStatus: {
      available: true,
      state: "LISTENING",
      devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16_000, default: true }],
      device_id: 7,
      device_name: "Test USB microphone",
      device_status: "READY",
      route_policy: "cloud",
      connected: true,
      recording_active: false,
      lan_health: { state: "UNKNOWN", age_ms: null, latency_ms: null },
    },
    liveAsrStatusEnvelope: {
      node_instance_id: "55555555-5555-4555-8555-555555555555",
      node_started_at_sec: 1_700_000_000,
      source_revision: "1".repeat(64),
    },
    onLiveAsrStatusPublisher: (publish) => { publishAsrStatus = publish; },
    onLiveAsrControlRequest: (args) => asrControls.push(args),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/asr/restart", async (route) => {
    restartRequestId = route.request().headers()["x-taskplanner-request-id"] ?? "";
    expect(restartRequestId).toMatch(asrRestartRequestIdPattern);
    restartPosted = true;
    restartedAtMs = Date.now();
    setTimeout(() => publishAsrStatus?.({
      schema: "taskplanner.asr.status.v1",
      stamp_sec: Date.now() / 1_000,
      node_instance_id: "66666666-6666-4666-8666-666666666666",
      node_started_at_sec: restartedAtMs / 1_000,
      source_revision: heartbeatRevision,
      asr: {
        available: true,
        state: "STOPPED",
        devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16_000, default: true }],
        device_id: 7,
        device_name: "Test USB microphone",
        device_status: "READY",
        route_policy: "cloud",
        connected: false,
        recording_active: false,
        lan_health: { state: "UNKNOWN", age_ms: null, latency_ms: null },
      },
    }), 80);
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        accepted: true,
        phase: "queued",
        generation: 12,
        job_id: "asr-job-12",
        request_id: restartRequestId,
        message: "queued",
        retryable: false,
        source_revision: hostRevision,
        container_started_at: null,
        before_pid: null,
        after_pid: null,
      }),
    });
  });
  await page.route("**/api/runtime/asr/status", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      phase: restartPosted ? "succeeded" : "idle",
      generation: restartPosted ? 12 : 11,
      job_id: restartPosted ? "asr-job-12" : null,
      request_id: restartPosted ? restartRequestId : null,
      message: restartPosted ? "succeeded" : "idle",
      retryable: false,
      source_revision: restartPosted ? hostRevision : "1".repeat(64),
      container_started_at: restartPosted ? new Date(restartedAtMs).toISOString() : null,
      before_pid: restartPosted ? 505 : null,
      after_pid: restartPosted ? 606 : null,
    }),
  }));

  await page.goto("/");
  const restartButton = page.getByRole("button", { name: "ASR 노드 새로 시작", exact: true });
  await restartButton.click();
  await expect(page.locator(".live-asr-message.error")).toContainText(
    "source_revision이 일치하지 않아 코드 반영을 확인할 수 없습니다",
    { timeout: 10_000 },
  );
  expect(asrControls).toEqual([]);
  await expect(restartButton).toBeEnabled();
  await expect(page.locator(".live-asr-message.error")).toContainText("이 버튼으로 다시 시도하세요");
});

test("refuses route or capture restore when the restarted ASR output contract is legacy", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One ASR output-contract rejection run is sufficient.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const sourceRevision = "9".repeat(64);
  const asrControls: Record<string, unknown>[] = [];
  let publishAsrStatus: ((envelope: Record<string, unknown>) => void) | null = null;
  let restartPosted = false;
  let containerStartedAtMs = 0;
  let restartRequestId = "";
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    liveAsrStatus: {
      available: true,
      state: "LISTENING",
      topic: "/sensors/surgeon/utterance",
      output_mode: "typed_utterance",
      output_topic: "/sensors/surgeon/utterance",
      devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16_000, default: true }],
      device_id: 7,
      device_name: "Test USB microphone",
      device_status: "READY",
      route_policy: "auto",
      connected: true,
      recording_active: false,
      lan_health: { state: "READY", age_ms: 10, latency_ms: 2 },
    },
    liveAsrStatusEnvelope: {
      node_instance_id: "99999999-9999-4999-8999-999999999999",
      node_started_at_sec: 1_700_000_000,
      source_revision: "8".repeat(64),
    },
    onLiveAsrStatusPublisher: (publish) => { publishAsrStatus = publish; },
    onLiveAsrControlRequest: (args) => asrControls.push(args),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/asr/restart", async (route) => {
    restartRequestId = route.request().headers()["x-taskplanner-request-id"] ?? "";
    expect(restartRequestId).toMatch(asrRestartRequestIdPattern);
    restartPosted = true;
    containerStartedAtMs = Date.now();
    setTimeout(() => publishAsrStatus?.({
      schema: "taskplanner.asr.status.v1",
      stamp_sec: containerStartedAtMs / 1_000,
      node_instance_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      node_started_at_sec: (containerStartedAtMs + 250) / 1_000,
      source_revision: sourceRevision,
      asr: {
        available: true,
        state: "STOPPED",
        topic: "/sensors/surgeon/sentence",
        output_mode: "sentence_text",
        output_topic: "/sensors/surgeon/sentence",
        devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16_000, default: true }],
        device_id: 7,
        device_name: "Test USB microphone",
        device_status: "READY",
        route_policy: "cloud",
        connected: false,
        recording_active: false,
        lan_health: { state: "UNKNOWN", age_ms: null, latency_ms: null },
      },
    }), 80);
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        accepted: true,
        phase: "queued",
        generation: 16,
        job_id: "asr-job-16",
        request_id: restartRequestId,
        message: "queued",
        retryable: false,
        source_revision: sourceRevision,
        container_started_at: null,
        before_pid: null,
        after_pid: null,
      }),
    });
  });
  await page.route("**/api/runtime/asr/status", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      phase: restartPosted ? "succeeded" : "idle",
      generation: restartPosted ? 16 : 15,
      job_id: restartPosted ? "asr-job-16" : null,
      request_id: restartPosted ? restartRequestId : null,
      message: restartPosted ? "succeeded" : "idle",
      retryable: false,
      source_revision: sourceRevision,
      container_started_at: restartPosted ? new Date(containerStartedAtMs).toISOString() : null,
      before_pid: restartPosted ? 909 : null,
      after_pid: restartPosted ? 1_010 : null,
    }),
  }));

  await page.goto("/");
  const restartButton = page.getByRole("button", { name: "ASR 노드 새로 시작", exact: true });
  await restartButton.click();
  await expect(page.locator(".live-asr-message.error")).toContainText(
    "Live typed_utterance 출력 계약(/sensors/surgeon/utterance)을 충족하지 않습니다",
    { timeout: 10_000 },
  );
  expect(asrControls).toEqual([]);
  await expect(restartButton).toBeEnabled();
});

test("rejects a late matching-source ASR publisher with an old node start time", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One ASR start-provenance rejection run is sufficient.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const sourceRevision = "7".repeat(64);
  const asrControls: Record<string, unknown>[] = [];
  let publishAsrStatus: ((envelope: Record<string, unknown>) => void) | null = null;
  let restartPosted = false;
  let containerStartedAtMs = 0;
  let restartRequestId = "";
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    liveAsrStatus: {
      available: true,
      state: "LISTENING",
      devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16_000, default: true }],
      device_id: 7,
      device_name: "Test USB microphone",
      device_status: "READY",
      route_policy: "cloud",
      connected: true,
      recording_active: false,
      lan_health: { state: "UNKNOWN", age_ms: null, latency_ms: null },
    },
    liveAsrStatusEnvelope: {
      node_instance_id: "77777777-7777-4777-8777-777777777777",
      node_started_at_sec: 1_700_000_000,
      source_revision: "6".repeat(64),
    },
    onLiveAsrStatusPublisher: (publish) => { publishAsrStatus = publish; },
    onLiveAsrControlRequest: (args) => asrControls.push(args),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/asr/restart", async (route) => {
    restartRequestId = route.request().headers()["x-taskplanner-request-id"] ?? "";
    expect(restartRequestId).toMatch(asrRestartRequestIdPattern);
    restartPosted = true;
    containerStartedAtMs = Date.now();
    setTimeout(() => publishAsrStatus?.({
      schema: "taskplanner.asr.status.v1",
      stamp_sec: Date.now() / 1_000,
      node_instance_id: "88888888-8888-4888-8888-888888888888",
      // A stray publisher can have a new UUID and matching source hash while
      // still belonging to a process that predates this restart job.
      node_started_at_sec: (containerStartedAtMs - 60_000) / 1_000,
      source_revision: sourceRevision,
      asr: {
        available: true,
        state: "STOPPED",
        devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16_000, default: true }],
        device_id: 7,
        device_name: "Test USB microphone",
        device_status: "READY",
        route_policy: "cloud",
        connected: false,
        recording_active: false,
        lan_health: { state: "UNKNOWN", age_ms: null, latency_ms: null },
      },
    }), 80);
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        accepted: true,
        phase: "queued",
        generation: 14,
        job_id: "asr-job-14",
        request_id: restartRequestId,
        message: "queued",
        retryable: false,
        source_revision: sourceRevision,
        container_started_at: null,
        before_pid: null,
        after_pid: null,
      }),
    });
  });
  await page.route("**/api/runtime/asr/status", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      phase: restartPosted ? "succeeded" : "idle",
      generation: restartPosted ? 14 : 13,
      job_id: restartPosted ? "asr-job-14" : null,
      request_id: restartPosted ? restartRequestId : null,
      message: restartPosted ? "succeeded" : "idle",
      retryable: false,
      source_revision: sourceRevision,
      container_started_at: restartPosted ? new Date(containerStartedAtMs).toISOString() : null,
      before_pid: restartPosted ? 707 : null,
      after_pid: restartPosted ? 808 : null,
    }),
  }));

  await page.goto("/");
  const restartButton = page.getByRole("button", { name: "ASR 노드 새로 시작", exact: true });
  await restartButton.click();
  await expect(page.locator(".live-asr-message.error")).toContainText(
    "node_started_at_sec가 호스트 container_started_at 재시작 구간과 일치하지 않습니다",
    { timeout: 10_000 },
  );
  expect(asrControls).toEqual([]);
  await expect(restartButton).toBeEnabled();
});

test("keeps Live surgical start fail-closed when integration preflight reports a blocker", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    integrationReadiness: {
      ready: false,
      checks: {
        contract_configuration: true,
        surgeon_sentence_publisher: true,
        tool_handover_action_server: true,
        retraction_command_service: true,
        perception_input: false,
      },
      missing: ["perception_input"],
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const startButton = page.getByRole("button", { name: "준비 중", exact: true });
  await expect(startButton).toBeDisabled();
  await expect(page.locator('[data-slot="integration-readiness"]')).toContainText(
    "인식 입력",
  );

  // A stale DOM action must not bypass the hook-level gate or invoke the
  // control Service; this is still a stub-only transport assertion.
  await startButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForTimeout(100);
  expect(missionServiceCalls.filter((service) => service === "/simulation/control")).toEqual([]);
});

test("reports a Live runtime-profile mismatch instead of leaving procedure start in perpetual preparation", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "failed",
    active_mode: null,
    requested_mode: "live",
    retryable: true,
    diagnostic_code: "runtime_profile_mismatch",
  };
  const missionServiceCalls: string[] = [];
  const requestedModes: LauncherMode[] = [];
  const socketConnections: string[] = [];

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
    onSocketConnect: (url) => socketConnections.push(url),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        phase: "starting",
        active_mode: null,
        requested_mode: "live",
        retryable: false,
      }),
    });
  });

  await page.goto("/");

  const bridgeStatus = page.locator('[data-slot="mission-command-bar"] [data-authority-status="blocked"]');
  await expect(bridgeStatus).toContainText("ROS 연결 차단됨");
  await expect(bridgeStatus).toHaveAttribute("aria-label", /의도적으로 차단했습니다/);
  await expect(page.getByText("ROS 끊김", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "VLM model provider and model" })).toHaveAttribute(
    "title",
    "ROS access blocked by runtime-contract mismatch",
  );
  await page.waitForTimeout(100);
  expect(socketConnections).toEqual([]);
  await expect(page.getByRole("button", { name: "런타임 복구 필요", exact: true })).toBeDisabled();
  await expect(page.locator("#runtime-transition-status")).toContainText(
    "자동으로 실제 장비 모드로 바꾸지 않았습니다",
  );
  await expect(page.locator('[data-slot="integration-readiness"]')).toContainText(
    "통합 점검 발행자가 없으므로 시작하지 않습니다",
  );
  const recoverButton = page.getByRole("button", { name: "현재 모드 시작", exact: true });
  await expect(recoverButton).toBeVisible();
  await recoverButton.click();
  await expect.poll(() => requestedModes).toEqual(["live"]);
  expect(missionServiceCalls.filter((service) => service === "/simulation/control")).toEqual([]);
});

test("shows the complete Live preflight while allowing a genuinely stopped bundle switch", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const stampSec = Date.now() / 1_000;
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    integrationReadiness: {
      stamp_sec: stampSec,
      ready: false,
      checks: {
        contract_configuration: true,
        surgeon_sentence_publisher: true,
        tool_handover_action_server: true,
        retraction_command_service: true,
        controller_contract: true,
        asr_runtime_status: false,
        bed_robot_arm_status: true,
        perception_input: false,
      },
      checklist: [
        { id: "contract_configuration", required: true, status: "pass", reason: "passed", detail: "Ready" },
        { id: "surgeon_sentence_publisher", required: true, status: "pass", reason: "passed", detail: "Ready" },
        { id: "tool_handover_action_server", required: true, status: "pass", reason: "passed", detail: "Ready" },
        { id: "retraction_command_service", required: true, status: "pass", reason: "passed", detail: "Ready" },
        { id: "controller_contract", required: true, status: "pass", reason: "passed", detail: "Ready" },
        { id: "asr_runtime_status", required: true, status: "pending", reason: "server_not_ready", detail: "ASR node is stopped" },
        { id: "bed_robot_arm_status", required: false, status: "pass", reason: "not_required", detail: "Service-only retraction path" },
        { id: "perception_input", required: true, status: "fail", reason: "source_stale", detail: "Waiting for RF-DETR health" },
      ],
      missing: ["asr_runtime_status", "perception_input"],
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const preflight = page.locator('[data-slot="integration-preflight-checklist"]');
  await expect(preflight).toHaveAttribute("data-preflight-state", "fail");
  await expect(preflight.locator("li")).toHaveCount(6);
  const perception = preflight.locator('[data-check-id="perception_input"]');
  await expect(perception).toHaveAttribute("data-check-status", "fail");
  await expect(perception).toContainText("source_stale");
  await expect(perception).toContainText("Waiting for RF-DETR health");
  await expect(perception.locator("[data-check-stamp]")).toHaveAttribute("data-check-stamp", /\d/);
  await expect(preflight.locator('[data-check-id="controller_contract"]')).toHaveCount(0);
  await expect(preflight.locator('[data-check-id="bed_robot_arm_status"]')).toHaveCount(0);

  const bundleSelect = page.locator(".control-stack label").filter({ hasText: "수술" }).locator("select");
  await expect(bundleSelect).toBeEnabled();
  await expect(page.getByRole("button", { name: "준비 중", exact: true })).toBeDisabled();
  await expect(page.locator('[data-slot="surgical-monitor-handoff"]')).toContainText("시작 전 관제 준비");
  await page.getByRole("button", { name: "수술 관제 열기", exact: true }).click();
  await expect(page).toHaveURL(/workspace=monitor/);
});

test("switches a stopped Live Action route while preserving the Service route", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const routeRequests: Record<string, unknown>[] = [];
  const missionServiceCalls: string[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    executionRouteState: {},
    onExecutionRouteCommand: (args) => routeRequests.push(args),
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const selector = page.locator('[data-slot="execution-route-selector"]');
  const actionRoutes = selector.getByRole("group", { name: "도구 전달 Action 서버 선택" });
  const serviceRoutes = selector.getByRole("group", { name: "리트랙션 Service 서버 선택" });
  const externalAction = actionRoutes.locator('button[data-source="external"]');
  const virtualAction = actionRoutes.locator('button[data-source="virtual"]');
  const externalService = serviceRoutes.locator('button[data-source="external"]');
  const virtualService = serviceRoutes.locator('button[data-source="virtual"]');
  await expect(selector).toBeVisible();
  await expect(externalAction).toBeEnabled();
  await expect(virtualAction).toBeDisabled();
  await expect(externalService).toBeEnabled();
  await expect(virtualService).toBeDisabled();

  await externalAction.click();
  await expect.poll(() => routeRequests).toHaveLength(1);
  expect(routeRequests[0]).toEqual({
    operation: "configure_execution_endpoints",
    payload_json: JSON.stringify({
      tool_handover_source: "external",
      retraction_source: "virtual",
    }),
  });
  const routeSummary = selector.locator(".execution-route-selector-summary > div");
  await expect(routeSummary.nth(0)).toContainText("실제 통합 서버");
  await expect(routeSummary.nth(1)).toContainText("가상 실행 서버");
  await expect(selector).toContainText("경로 변경과 초기화가 완료");
  // The browser cannot reuse the old virtual preflight after a route reset.
  await expect(page.getByRole("button", { name: "준비 중", exact: true })).toBeDisabled();
  expect(missionServiceCalls.filter((service) =>
    service === "/surgery/tool_handover" ||
    service === "/surgery/retraction/command",
  )).toEqual([]);
});

test("allows a stopped completed Live procedure to select its next Action/Service route", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "completed" },
    executionRouteState: {},
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(
    page.getByRole("group", { name: "도구 전달 Action 서버 선택" })
      .locator('button[data-source="external"]'),
  ).toBeEnabled();
});

test("locks Live start and another route switch while route initialization awaits preflight acknowledgement", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    executionRouteState: { initialization_state: "initializing" },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const selector = page.locator('[data-slot="execution-route-selector"]');
  await expect(
    selector.getByRole("group", { name: "도구 전달 Action 서버 선택" })
      .locator('button[data-source="external"]'),
  ).toBeDisabled();
  await expect(
    selector.getByRole("group", { name: "리트랙션 Service 서버 선택" })
      .locator('button[data-source="external"]'),
  ).toBeDisabled();
  await expect(page.getByRole("button", { name: "준비 중", exact: true })).toBeDisabled();
  await expect(page.locator('[data-slot="integration-readiness"]')).toContainText(
    "새 Action·Service 경로를 통합 시작 점검에 적용하는 중입니다",
  );
});

test("does not mark a Live Action/Service route change ready without a confirmed DT reset", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    executionRouteState: {},
    executionRouteCommandResponse: {
      accepted: true,
      command_id: "",
      message: "route changed without reset acknowledgement",
      result_json: JSON.stringify({
        schema: "taskplanner.execution_route_state.v1",
        stamp_sec: Date.now() / 1_000,
        revision: 2,
        initialization_revision: 2,
        selected_source: "external",
        retraction_source: "virtual",
        run_endpoint_source: "",
        run_retraction_source: "",
        initialization_state: "initialized",
        action_server_ready: true,
        retraction_service_ready: true,
        route_control_enabled: true,
        active_request_count: 0,
        require_bed_robot_status: false,
        require_physical_stop_confirmation: false,
        retraction_state_machine_suppressed: true,
        digital_twin_reset: false,
      }),
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const selector = page.locator('[data-slot="execution-route-selector"]');
  await selector.getByRole("group", { name: "도구 전달 Action 서버 선택" })
    .locator('button[data-source="external"]')
    .click();
  await expect(selector.getByRole("alert")).toContainText(
    "did not confirm a completed digital-twin reset",
  );
  await expect(selector).not.toContainText("경로 변경과 초기화가 완료");
});

test("rejects even a forced Live Action/Service route click while execution is active", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const routeRequests: Record<string, unknown>[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: true, execution_state: "running" },
    executionRouteState: {
      run_endpoint_source: "virtual",
      initialization_state: "running",
    },
    onExecutionRouteCommand: (args) => routeRequests.push(args),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const external = page.getByRole("group", { name: "도구 전달 Action 서버 선택" })
    .locator('button[data-source="external"]');
  await expect(external).toBeDisabled();
  await external.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    button.setAttribute("disabled", "");
  });
  await page.waitForTimeout(120);
  expect(routeRequests).toEqual([]);
});

test("renders fresh CAM3 CAM4 RF-DETR facts as VLM location evidence without detector images", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    vlmRequestContextMessage: freshStructuredVlmRequestContext(),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const evidence = page.locator('[data-slot="vlm-tool-detection-evidence"]');
  await expect(evidence).toHaveAttribute("data-evidence-state", "ready");
  await expect(evidence).toHaveAttribute("data-evidence-kind", "structured-not-image");
  await expect(evidence.locator("img")).toHaveCount(0);
  const cam3 = evidence.locator('[data-view="cam_3"]');
  await expect(cam3).toHaveAttribute("data-freshness", "fresh");
  await expect(cam3).toHaveAttribute("data-visual-alignment", "not_compared_no_flir_reference");
  await expect(cam3).toContainText("T07");
  await expect(cam3).toContainText("bbox [0.10, 0.20 – 0.50, 0.60]");
  await expect(cam3).toContainText("관측점 [0.31, 0.39]");
  const cam4 = evidence.locator('[data-view="cam_4"]');
  await expect(cam4).toHaveAttribute("data-freshness", "fresh");
  await expect(cam4).toHaveAttribute("data-visual-alignment", "misaligned");
  await expect(cam4).toContainText("새 프레임 · 명시적 탐지 없음");
  await expect(cam4).toContainText("시각 불일치");
});

test("rejects a malformed VLM request context instead of displaying a tool location", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const malformedContext = freshStructuredVlmRequestContext();
  const parsedContext = JSON.parse(String(malformedContext.compact_json)) as {
    observable_perception: { tool_detection_views: Array<{ instances: Array<{ bbox_xyxy_norm: unknown }> }> };
  };
  parsedContext.observable_perception.tool_detection_views[0].instances[0].bbox_xyxy_norm = [0.1, 0.2, 0.5];
  malformedContext.compact_json = JSON.stringify(parsedContext);
  await installRosbridgeStub(page, { vlmRequestContextMessage: malformedContext });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const evidence = page.locator('[data-slot="vlm-tool-detection-evidence"]');
  await expect(evidence).toHaveAttribute("data-evidence-state", "waiting");
  await expect(evidence).toContainText("구조화 관측 대기");
  await expect(evidence.locator('[data-detection-tool="T07"]')).toHaveCount(0);
});

test("keeps Live resume fail-closed when its integration readiness expires or fails", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];

  await installRosbridgeStub(page, {
    simulationState: { running: true, execution_state: "paused" },
    integrationReadiness: {
      ready: false,
      checks: {
        contract_configuration: true,
        surgeon_sentence_publisher: false,
        tool_handover_action_server: true,
        retraction_command_service: true,
        perception_input: true,
      },
      missing: ["surgeon_sentence_publisher"],
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const resumeButton = page.getByRole("button", { name: "재생", exact: true });
  await expect(resumeButton).toBeDisabled();
  await expect(resumeButton).toHaveAttribute("aria-describedby", "integration-readiness-status");

  await resumeButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForTimeout(100);
  expect(missionServiceCalls.filter((service) => service === "/simulation/control")).toEqual([]);
});

test("sends one priority planner stop while a Live start response is still pending", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    missionControlResponseDelayMs: { start: 700, stop: 300 },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await page.getByRole("button", { name: "수술 시작", exact: true }).click();
  await expect.poll(() =>
    missionServiceCalls.filter((service) => service === "/simulation/control").length,
  ).toBe(1);

  const stopButton = page.getByRole("button", { name: "실행 중지", exact: true });
  await expect(stopButton).toBeEnabled();
  await stopButton.click();
  await expect.poll(() =>
    missionServiceCalls.filter((service) => service === "/simulation/control").length,
  ).toBe(2);

  // A stale DOM event must not duplicate the high-priority stop request.
  await stopButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForTimeout(100);
  expect(missionServiceCalls.filter((service) => service === "/simulation/control")).toHaveLength(2);
  await expect(page.locator("#live-stop-scope-note")).toContainText("독립 안전 절차");

  const controlMessage = page.locator(
    ".dock-action-message:not(.integration-readiness-message)",
  );
  await expect(controlMessage).toContainText(
    "Verify external robot safety state separately.",
    { timeout: 2_000 },
  );
  // The delayed successful Start reply belongs to the superseded control run
  // and must not overwrite the Stop result or make a physical-stop claim.
  await page.waitForTimeout(500);
  await expect(controlMessage).not.toContainText("simulation started");
});

test("marks a last-known Live ASR heartbeat stale before presenting it as stopped", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One ASR heartbeat freshness run is sufficient.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    liveAsrStatus: {
      available: true,
      state: "STOPPED",
      topic: "/sensors/surgeon/utterance",
      output_mode: "typed_utterance",
      output_topic: "/sensors/surgeon/utterance",
      devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16_000, default: true }],
      device_id: 7,
      device_name: "Test USB microphone",
      device_status: "READY",
      route_policy: "cloud",
      connected: false,
      recording_active: false,
      lan_health: { state: "UNKNOWN", age_ms: null, latency_ms: null },
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const asrState = page.locator(".live-asr-state");
  await expect(asrState).toContainText("ASR 정지");
  await expect(asrState).toContainText("상태 지연", { timeout: 7_000 });
  await expect(asrState).toHaveClass(/stale/);
  await expect(asrState).toHaveAttribute("data-status-fresh", "false");
  await expect(page.getByRole("button", { name: "ASR 시작" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "ASR 노드 새로 시작", exact: true })).toBeEnabled();
  await expect(page.locator(".live-asr-message.error")).toContainText("ASR sidecar 상태가 5초 이상 갱신되지 않았습니다.");
});

test("explains that Live ASR control stays unavailable until the local sidecar is ready", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const startButton = page.getByRole("button", { name: "ASR 시작" });
  await expect(startButton).toBeDisabled();
  await expect(page.getByRole("button", { name: "ASR 노드 새로 시작", exact: true })).toBeEnabled();
  await expect(startButton).toHaveAttribute("aria-describedby", "live-asr-start-help");
  await expect(page.locator("#live-asr-start-help")).toContainText(
    "로컬 ASR sidecar의 상태·제어 Service를 기다리는 중입니다.",
  );
});

test("marks a last-known VLM heartbeat stale before presenting it as healthy", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One active-run viewport is enough for the VLM heartbeat freshness guard.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    simulationState: { running: true, execution_state: "running" },
    simulationStateHeartbeatCount: 6,
    simulationStateHeartbeatIntervalMs: 1_500,
    publishVlmHealth: true,
    vlmHealthPublishLimit: 1,
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const modelHealth = page.locator(".ribbon-model-control > strong");
  await expect(modelHealth).toHaveText("정상");
  await expect(modelHealth).toHaveText("상태 지연", { timeout: 9_000 });
  await expect(page.locator(".ribbon-model-control")).toHaveClass(/warn/);
  await expect(page.locator(".ribbon-model-control")).toHaveAttribute(
    "title",
    /VLM health 신호가 6초 이상 없습니다/,
  );
});

test("shows system-final tool ranks, VLM Mayo evidence, and actual dispatch as separate authorities", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One integrated operation-observability run is sufficient.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    simulationState: { running: true, execution_state: "running" },
    simulationStateHeartbeatCount: 8,
    simulationStateHeartbeatIntervalMs: 1_000,
    simulationStateMessage: {
      procedure_id: "thyroidectomy_demo",
      active_bundle: "thyroidectomy_v1",
      filtered_phase: "P03",
      running: true,
      execution_state: "running",
      instrument_states: [
        {
          instrument_id: "T02",
          home_location_type: "rack",
          home_location_id: "T02",
          location_type: "rack",
          location_id: "T02",
          owner: "none",
          status: "available",
          confidence: 0.94,
          cleanliness_state: "sterile",
          contaminated: false,
          lifecycle_stage: "home_rack",
          reserved_for: "",
          last_holder: "none",
          next_required_transition: "",
          visual_anchor_id: "main_tray_slot_2",
        },
        {
          instrument_id: "T04",
          home_location_type: "rack",
          home_location_id: "T04",
          location_type: "rack",
          location_id: "T04",
          owner: "none",
          status: "available",
          confidence: 0.94,
          cleanliness_state: "sterile",
          contaminated: false,
          lifecycle_stage: "home_rack",
          reserved_for: "",
          last_holder: "none",
          next_required_transition: "",
          visual_anchor_id: "main_tray_slot_4",
        },
        {
          instrument_id: "T07",
          instance_id: "T07#2",
          home_location_type: "rack",
          home_location_id: "T07",
          location_type: "mayo_stand",
          location_id: "mayo_stand",
          owner: "none",
          status: "available",
          confidence: 0.91,
          cleanliness_state: "sterile",
          contaminated: false,
          lifecycle_stage: "mayo_recovery",
          reserved_for: "",
          last_holder: "surgeon",
          next_required_transition: "recover_left",
          visual_anchor_id: "mayo_recovery_zone",
        },
      ],
    },
    vlmResultMessage: {
      source: "rfdetr_tool_observation_2d",
      schema_version: "4",
      raw_json: JSON.stringify({
        v: "4",
        phase: [],
        // Deliberately disagree with the reducer output below. Stage cards must
        // display the system-final ranking, not these raw VLM candidates.
        tool: [["T04", 0.85], ["T02", 0.1], ["T07", 0.05], ["T01", 0]],
        intent: ["", "", 0],
        // T02 is physically in the rack, so a raw VLM suggestion must never
        // surface as a Mayo policy badge or observability decision.
        mayo: [["T07", "recover", 0.91], ["T02", "reuse", 0.88]],
        mayo_retrieve: ["T07", 0.91],
        u: 0.1,
        sum: "test observation",
        bed_robot_arm_group: null,
      }),
      summary: "test observation",
      phase_ids: [],
      phase_confidences: [],
      observed_tool_ids: [],
      observed_location_ids: [],
      observed_location_types: [],
      observed_confidences: [],
      uncertainty: 0.1,
    },
    worldStateMessage: {
      predicted_tool: "T07",
      predicted_tool_confidence: 0.5,
      predicted_tool_stability_sec: 2.3,
      ranked_tool_predictions: [
        { rank: 1, instrument_id: "T07", confidence: 0.5, stability_sec: 2.3 },
        { rank: 2, instrument_id: "T02", confidence: 0.3, stability_sec: 0 },
        { rank: 3, instrument_id: "T04", confidence: 0.2, stability_sec: 0 },
      ],
      implicit_request_visible: true,
      implicit_request_tool: "",
      implicit_request_hand_pose: "right_open_palm_up",
      implicit_request_confidence: 0.93,
      implicit_request_stability_sec: 0.4,
      implicit_request_generation: 7,
    },
    liveAsrStatus: {
      available: true,
      state: "RUNNING",
      route_policy: "cloud",
      connected: false,
      recording_active: false,
      devices: [],
      device_status: "READY",
      finals: [{ stamp: "2026-08-23T11:14:12.100Z", text: "애드슨 포셉 주세요" }],
    },
    skillStatusMessages: [{
      command_id: "handover-action-1",
      action: "tool_handover",
      instrument_id: "T02",
      instrument_instance_id: "T02#1",
      state: "accepted",
      success: false,
      message: "accepted",
      arm: "right",
      source_location_id: "T02",
      source_location_type: "rack",
      target_location_id: "surgeon",
      target_location_type: "surgeon",
      target_owner: "surgeon",
      cleaning_required: false,
      mode: "virtual",
      progress: 0.1,
      elapsed_sec: 0.1,
      remaining_sec: 0.5,
    }],
    executionTraceMessages: [
      {
        sequence: 1,
        command_id: "handover-action-1",
        route: "handover",
        transport: "action",
        endpoint: "/bt/skill_command",
        stage: "accepted",
        dispatch_submitted: true,
        terminal: false,
        evidence: "action_goal_accepted",
        reason_code: "",
      },
      {
        sequence: 2,
        command_id: "retraction-service-2",
        route: "retraction",
        transport: "service",
        endpoint: "/external/bed_robot_arm/execute_retraction",
        stage: "accepted",
        dispatch_submitted: true,
        terminal: false,
        evidence: "service_admission_only",
        reason_code: "",
        retraction_command: 4,
        retraction_target_side: 0,
        retraction_distance_m: 0.005,
      },
      {
        sequence: 3,
        command_id: "finish-direct-teach-both-3",
        route: "retraction",
        transport: "service",
        endpoint: "/external/bed_robot_arm/execute_retraction",
        stage: "accepted",
        dispatch_submitted: true,
        terminal: true,
        evidence: "service_admission_only",
        reason_code: "",
        retraction_command: 2,
        retraction_target_side: 0,
        retraction_distance_m: 0,
      },
    ],
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const firstNextTool = page.locator('[data-slot="operation-vlm-tool-evidence"][data-vlm-next-tool-rank="1"]');
  await expect(firstNextTool).toContainText("다음 도구 1순위");
  await expect(firstNextTool).toContainText("50%");
  await expect(firstNextTool).toHaveAttribute("data-next-tool-authority", "system");
  const secondNextTool = page.locator('[data-slot="operation-vlm-tool-evidence"][data-vlm-next-tool-rank="2"]');
  await expect(secondNextTool).toContainText("다음 도구 2순위");
  await expect(secondNextTool).toContainText("30%");
  const thirdNextTool = page.locator('[data-slot="operation-vlm-tool-evidence"][data-vlm-next-tool-rank="3"]');
  await expect(thirdNextTool).toContainText("다음 도구 3순위");
  await expect(thirdNextTool).toContainText("20%");
  await expect(page.locator('[data-vlm-next-tool-rank="4"]')).toHaveCount(0);
  const mayoRecovery = page.locator('[data-tool-holder-id="mayo"] [data-slot="operation-vlm-tool-evidence"][data-vlm-mayo="recover"]');
  await expect(mayoRecovery).toContainText("회수 필요");
  await expect(mayoRecovery).toContainText("91%");
  const mayoInstance = page.locator('[data-tool-holder-id="mayo"] [data-slot="stage-tool-instance-marker"]');
  await expect(mayoInstance).toHaveText("#2");
  await expect(mayoInstance).toHaveAttribute("data-tool-instance-id", "T07#2");
  const rackSecondRank = page.locator('[data-tool-holder-id="rack"] [data-slot="operation-vlm-tool-evidence"][data-vlm-next-tool-rank="2"]');
  await expect(rackSecondRank).toHaveAttribute("data-vlm-mayo", "none");

  const handoutPopup = page.locator('[data-slot="hand-handover-signal-popup"]');
  await expect(handoutPopup).toBeVisible();
  await expect(handoutPopup).toHaveAttribute("data-source", "reducer-world-state");
  await expect(handoutPopup).toHaveAttribute("data-generation", "7");
  await expect(handoutPopup).toContainText("오른손 펼침 · 손바닥 위");
  await expect(handoutPopup).toContainText("0.4초 유지");
  await expect(handoutPopup).toContainText("93%");
  await expect(handoutPopup).toContainText("리듀서 게이트 통과");
  await expect(handoutPopup).not.toContainText("T02");

  const asrFinalPopup = page.locator('[data-slot="surgeon-asr-final-popup"]');
  await expect(asrFinalPopup).toBeVisible();
  await expect(asrFinalPopup).toContainText("애드슨 포셉 주세요");
  await expect(asrFinalPopup).toContainText("액션 또는 서비스 요청이 아닙니다");

  const dispatchPopup = page.locator('[data-slot="operation-execution-dispatch-popup"]');
  await expect(dispatchPopup).toHaveAttribute("data-dispatch-kind", "service");
  await expect(dispatchPopup).toHaveAttribute("data-dispatch-state", "accepted");
  await expect(dispatchPopup).toContainText("서비스 접수 확인 · 물리 동작 완료 아님");
  const dispatchFeed = page.locator('[data-slot="operation-execution-dispatch-feed"]');
  await expect(dispatchFeed).toContainText("대상 도구 · T02");
  await expect(dispatchFeed).toContainText("/bt/skill_command");
  await expect(dispatchFeed).toContainText("/external/bed_robot_arm/execute_retraction");
  const handoverDispatch = dispatchFeed.locator('[data-command-id="handover-action-1"]');
  await expect(handoverDispatch).toHaveAttribute("data-tool-instance-id", "T02#1");
  await expect(handoverDispatch).toContainText("대상 도구 · T02 #1");
  await expect(handoverDispatch.locator('[data-slot="operation-dispatch-tool-flow"]')).toContainText(
    "도구 랙 · T02",
  );
  await expect(handoverDispatch.locator('[data-slot="operation-dispatch-tool-flow"]')).toContainText("집도의");
  await expect(handoverDispatch).toContainText("작업 경로");
  await expect(handoverDispatch).toContainText("handover");
  await expect(handoverDispatch).toContainText("명령 ID");
  await expect(handoverDispatch).toContainText("handover-action-1");
  await expect(handoverDispatch).toContainText("시퀀스");
  await expect(handoverDispatch).toContainText("1");
  const retractionDispatch = dispatchFeed.locator('[data-command-id="retraction-service-2"]');
  await expect(retractionDispatch).toHaveAttribute("data-retraction-command", "4");
  await expect(retractionDispatch).toHaveAttribute("data-retraction-target-side", "0");
  const retractionPayload = retractionDispatch.locator(
    '[data-slot="operation-dispatch-retraction-payload"]',
  );
  await expect(retractionPayload).toContainText("명령 종류");
  await expect(retractionPayload).toContainText("리트랙션 조정");
  await expect(retractionPayload).toContainText("대상 쪽");
  await expect(retractionPayload).toContainText("양쪽");
  await expect(retractionPayload).toContainText("이동 거리");
  await expect(retractionPayload).toContainText("0.5 cm");
  await expect(retractionDispatch.locator('[data-slot="operation-dispatch-tool-flow"]')).toHaveCount(0);
  const finishDirectTeachDispatch = dispatchFeed.locator(
    '[data-command-id="finish-direct-teach-both-3"]',
  );
  await expect(finishDirectTeachDispatch).toContainText("직접 교시 종료");
  await expect(finishDirectTeachDispatch).toContainText("양쪽");
  await expect(finishDirectTeachDispatch).toContainText("0 cm");

  await page.getByRole("tab", { name: "VLM" }).click();
  const vlmPanel = page.locator("#observability-panel-vlm");
  await expect(vlmPanel).not.toContainText("손 전달 신호");
  await expect(vlmPanel).toContainText("1순위");
  await expect(vlmPanel).toContainText("2순위");
  await expect(vlmPanel).toContainText("3순위");
  const rawToolRanking = vlmPanel.locator(".detail-card").filter({ hasText: "VLM 제안 다음 도구" });
  await expect(rawToolRanking).toContainText("85%");
  await expect(rawToolRanking).toContainText("10%");
  await expect(rawToolRanking).toContainText("5%");
  const systemToolRanking = vlmPanel.locator(".detail-card").filter({ hasText: "시스템 최종 다음 도구" });
  await expect(systemToolRanking).toContainText("50%");
  await expect(systemToolRanking).toContainText("30%");
  await expect(systemToolRanking).toContainText("20%");
  const rawMayo = vlmPanel.locator(".detail-card").filter({ hasText: "Mayo VLM 원시 판단" });
  await expect(rawMayo).toContainText("91%");
  await expect(rawMayo).not.toContainText("88%");
});

test("keeps a client-sent Action with unknown remote state visible in the operation popup", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One dispatch uncertainty run is sufficient.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    simulationState: { running: true, execution_state: "running" },
    executionTraceMessages: [
      {
        sequence: 1,
        command_id: "handover-action-unknown",
        route: "handover",
        transport: "action",
        endpoint: "/bt/skill_command",
        stage: "unknown",
        dispatch_submitted: true,
        terminal: false,
        evidence: "response_unavailable",
        reason_code: "controller_response_timeout",
      },
    ],
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const popup = page.locator('[data-slot="operation-execution-dispatch-popup"]');
  await expect(popup).toBeVisible();
  await expect(popup).toHaveAttribute("data-dispatch-kind", "action");
  await expect(popup).toHaveAttribute("data-dispatch-state", "unknown");
  await expect(popup).toContainText("상태 미확인");
  await expect(popup).toContainText("controller_response_timeout");
});

test("uses the reducer-visible bit without re-evaluating hand evidence in the browser", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    simulationState: { running: true, execution_state: "running" },
    worldStateMessage: {
      implicit_request_visible: false,
      implicit_request_tool: "T02",
      implicit_request_hand_pose: "right_open_palm_up",
      implicit_request_confidence: 0.98,
      implicit_request_stability_sec: 0.8,
      implicit_request_generation: 4,
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.locator('[data-slot="hand-handover-signal-popup"]')).toHaveCount(0);
  const btPanel = page.locator("#observability-panel-bt");
  await expect(btPanel).toContainText("손 전달 신호 · 리듀서");
  await expect(btPanel).toContainText("신호 대기");
});

test("localizes recent LLM speech age labels with the selected language", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    surgeonLlmDecision: {
      speech: "15번 메스를 준비합니다.",
      action: "handover",
      tool: "scalpel",
      request_mode: "voice",
      accepted: true,
      reject_reason: "",
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const speechAge = page.locator(".llm-speech-log-item small").first();
  await expect(page.locator(".llm-speech-log-item strong").first()).toHaveText("15번 메스를 준비합니다.");
  await expect(speechAge).toContainText("방금");

  await page.getByRole("button", { name: "English", exact: true }).click();
  await expect(speechAge).toContainText("now");
});

test("pauses model catalog polling while the Mission document is hidden", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const catalogServices = [
    "/real_vlm_node/list_model_catalog",
    "/surgeon_actor/list_model_catalog",
  ];
  const serviceCalls: string[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    simulationStateHeartbeatCount: 8,
    simulationStateHeartbeatIntervalMs: 750,
    onMissionServiceCall: (service) => serviceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect.poll(() => serviceCalls.filter((service) => catalogServices.includes(service)).length).toBeGreaterThanOrEqual(2);
  const baseline = serviceCalls.filter((service) => catalogServices.includes(service)).length;
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
  });
  await page.waitForTimeout(5_200);
  expect(serviceCalls.filter((service) => catalogServices.includes(service)).length).toBe(baseline);

  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => serviceCalls.filter((service) => catalogServices.includes(service)).length).toBeGreaterThan(baseline);
});

test("defers initial model catalog polling until the Mission document is visible", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const catalogServices = [
    "/real_vlm_node/list_model_catalog",
    "/surgeon_actor/list_model_catalog",
  ];
  const serviceCalls: string[] = [];
  await page.addInitScript(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
  });
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    onMissionServiceCall: (service) => serviceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await page.waitForTimeout(1_200);
  expect(serviceCalls.filter((service) => catalogServices.includes(service))).toHaveLength(0);

  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => serviceCalls.filter((service) => catalogServices.includes(service)).length).toBeGreaterThanOrEqual(2);
});

test("pauses runtime status polling while the Mission document is hidden", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "starting",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  let statusRequests = 0;
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
  });
  await page.route("**/api/runtime/status", (route) => {
    statusRequests += 1;
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) });
  });

  await page.goto("/");
  await expect.poll(() => statusRequests).toBeGreaterThan(0);
  const baseline = statusRequests;
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
  });
  await page.waitForTimeout(3_200);
  expect(statusRequests).toBe(baseline);

  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => statusRequests).toBeGreaterThan(baseline);
});

test("fails closed when the Live ASR status payload exceeds the UI bound", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    liveAsrStatusMessage: { data: "x".repeat(256 * 1024 + 1) },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.goto("/");

  const panel = page.locator('[data-slot="live-asr-panel"]');
  await expect(panel).toBeVisible();
  await expect(page.getByRole("button", { name: "ASR 시작" })).toBeDisabled();
  await expect(panel).toContainText("ASR 시작 전");
});

test("fails closed when the Live ASR status structure exceeds the UI bound", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const oversizedAsr = {
    schema: "taskplanner.asr.status.v1",
    stamp_sec: 1,
    asr: Object.fromEntries(Array.from({ length: 513 }, (_, index) => [`extra-${index}`, index])),
  };

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    liveAsrStatusMessage: { data: JSON.stringify(oversizedAsr) },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.goto("/");

  const panel = page.locator('[data-slot="live-asr-panel"]');
  await expect(panel).toBeVisible();
  await expect(page.getByRole("button", { name: "ASR 시작" })).toBeDisabled();
  await expect(panel).toContainText("ASR 시작 전");
});

test("rejects malformed Live ASR booleans and route policy instead of coercing them", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  let publishAsrStatus: ((envelope: Record<string, unknown>) => void) | null = null;
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    liveAsrStatus: {
      available: true,
      state: "STOPPED",
      topic: "/sensors/surgeon/utterance",
      output_mode: "typed_utterance",
      output_topic: "/sensors/surgeon/utterance",
      devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16_000, default: true }],
      device_id: 7,
      device_name: "Test USB microphone",
      device_status: "READY",
      route_policy: "auto",
      connected: false,
      recording_active: false,
      lan_health: { state: "READY", age_ms: 10, latency_ms: 2 },
    },
    onLiveAsrStatusPublisher: (publish) => { publishAsrStatus = publish; },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.goto("/");

  const panel = page.locator('[data-slot="live-asr-panel"]');
  await expect(panel.locator(".live-asr-state")).toContainText("ASR 정지");
  publishAsrStatus?.({
    schema: "taskplanner.asr.status.v1",
    stamp_sec: Date.now() / 1_000,
    asr: {
      available: "false",
      state: "LISTENING",
      topic: "/sensors/surgeon/utterance",
      output_mode: "typed_utterance",
      output_topic: "/sensors/surgeon/utterance",
      devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16_000, default: true }],
      device_id: 7,
      route_policy: "nearest-server",
      connected: "false",
      recording_active: "false",
      lan_health: { state: "READY", age_ms: 10, latency_ms: 2 },
    },
  });
  await page.waitForTimeout(250);

  // Keep the last valid snapshot; string "false" must not become true and an
  // unknown route must not silently become the cloud policy.
  await expect(panel.locator(".live-asr-state")).toContainText("ASR 정지");
  await expect(panel.locator('input[type="radio"][value="auto"]')).toBeChecked();
  await expect(page.getByRole("button", { name: "ASR 중지" })).toBeDisabled();
});

test("bounds Live ASR device history and rendered transcript text", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const devices = Array.from({ length: 80 }, (_, id) => ({
    id,
    name: `Input ${id}`,
    input_channels: 1,
    default_samplerate: 16_000,
    default: id === 0,
  }));

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    liveAsrStatus: {
      available: true,
      state: "STOPPED",
      topic: "/sensors/surgeon/utterance",
      output_mode: "typed_utterance",
      output_topic: "/sensors/surgeon/utterance",
      devices,
      device_id: 0,
      device_name: "Input 0",
      device_status: "READY",
      route_policy: "cloud",
      connected: false,
      recording_active: false,
      partial_text: "p".repeat(10_000),
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.goto("/");

  await expect(page.locator('[data-slot="live-asr-panel"]')).toBeVisible();
  await expect.poll(() => page.locator("#live-asr-device option").count()).toBe(64);
  const partialText = await page.locator(".live-asr-live p strong").textContent();
  expect(partialText?.length).toBe(4_096);
});

test("keeps Live Mission columns inside the viewport at compact and wide ratios", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "Resize matrix is intentionally covered once.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    liveAsrStatus: {
      available: true,
      state: "STOPPED",
      topic: "/sensors/surgeon/utterance",
      output_mode: "typed_utterance",
      output_topic: "/sensors/surgeon/utterance",
      devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16000, default: true }],
      device_id: 7,
      device_name: "Test USB microphone",
      device_status: "READY",
      device_message: "ready",
      route_policy: "cloud",
      connected: false,
      recording_active: false,
      lan_health: { state: "UNKNOWN", age_ms: null, latency_ms: null },
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.goto("/");

  for (const viewport of [
    { width: 320, height: 800 },
    { width: 390, height: 844 },
    { width: 1024, height: 768 },
    { width: 1280, height: 800 },
    { width: 1366, height: 768 },
    { width: 1440, height: 900 },
    { width: 1536, height: 864 },
    { width: 1920, height: 1080 },
    { width: 2560, height: 1080 },
    { width: 3440, height: 1440 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(page.locator(".mission-layout")).toBeVisible();
    await expect(page.locator('[data-slot="live-asr-panel"]')).toBeVisible();
    const metrics = await page.evaluate(() => {
      const root = document.documentElement;
      const selectors = [".stage-area", ".runtime-area", '[data-slot="live-asr-panel"]'];
      const boxes = selectors.map((selector) => {
        const element = document.querySelector(selector);
        const rect = element?.getBoundingClientRect();
        return {
          selector,
          left: rect?.left ?? 0,
          right: rect?.right ?? 0,
          width: rect?.width ?? 0,
          visible: Boolean(element && rect && rect.width > 0 && rect.height > 0),
        };
      });
      return {
        scrollWidth: root.scrollWidth,
        clientWidth: root.clientWidth,
        boxes,
        overflowing: [...document.querySelectorAll("body *")].filter((element) => {
          const style = getComputedStyle(element);
          const bounds = element.getBoundingClientRect();
          return (
            style.display !== "none" &&
            style.visibility !== "hidden" &&
            style.display !== "inline" &&
            bounds.width > 0 &&
            (bounds.left < -1 || bounds.right > root.clientWidth + 1) &&
            !element.classList.contains("skip-link")
          );
        }).map((element) => element.className || element.tagName),
      };
    });
    expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth + 1);
    expect(metrics.overflowing, `${viewport.width}x${viewport.height} overflow: ${JSON.stringify(metrics.overflowing)}`).toEqual([]);
    for (const box of metrics.boxes) {
      expect(box.visible, `${box.selector} should remain visible at ${viewport.width}px`).toBe(true);
      expect(box.left, `${box.selector} left edge should stay in view`).toBeGreaterThanOrEqual(-1);
      expect(box.right, `${box.selector} right edge should stay in view`).toBeLessThanOrEqual(viewport.width + 1);
    }
  }
});

test("keeps a paused Replay when standalone Debug entry is forced", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "replay",
    requested_mode: "replay",
    retryable: false,
  };
  const requestedModes: LauncherMode[] = [];
  let shadowSubscribed = false;

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    shadowReplayState: {
      state: "paused",
      loaded: true,
      running: false,
      paused: true,
      completed: false,
    },
    onShadowSubscription: () => {
      shadowSubscribed = true;
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify(runtime) });
  });

  await page.goto("/");
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("shadow");
  await expect.poll(() => shadowSubscribed).toBe(true);
  await expect(page.locator(".shadow-replay-dock")).toContainText("사용자 일시정지");

  await expect(page.getByRole("button", { name: "독립 Debug" })).toHaveCount(0);
  await page.waitForTimeout(100);

  await expect(page.locator(".runtime-mode-select select")).toHaveValue("shadow");
  await expect(page.locator(".shadow-replay-dock")).toContainText("사용자 일시정지");
  await expect(page.locator(".debug-main")).toHaveCount(0);
  expect(requestedModes).toEqual([]);
});

test("does not let a stale status failure overwrite an accepted transition", async ({ page }) => {
  let transitionAccepted = false;
  const startingStatus: RuntimeStatus = {
    phase: "starting",
    active_mode: "llm-surgeon",
    requested_mode: "live",
    retryable: false,
  };
  const requestedModes: LauncherMode[] = [];

  await installRosbridgeStub(page);
  await page.route("**/api/runtime/status", async (route) => {
    const staleRequest = !transitionAccepted;
    if (staleRequest) {
      await new Promise((resolve) => setTimeout(resolve, 450));
      await route.abort("failed");
      return;
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(startingStatus) });
  });
  await page.route("**/api/runtime/transition", async (route) => {
    transitionAccepted = true;
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify(startingStatus) });
  });

  await page.goto("/");
  await page.locator(".runtime-mode-select select").selectOption("live");
  await expect.poll(() => requestedModes).toEqual(["live"]);
  await page.waitForTimeout(600);

  await expect(page.locator(".runtime-transition-feedback.pending")).toBeVisible();
  await expect(page.locator(".runtime-transition-feedback.error")).toHaveCount(0);
});

test("shows the runtime controller reason when a transition is blocked", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    message: "Selected runtime is ready.",
    retryable: false,
  };
  const blockedMessage =
    "Could not verify that the active runtime is stopped. Retry after its state becomes available.";
  const requestedModes: LauncherMode[] = [];

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        ...runtime,
        requested_mode: "live",
        message: blockedMessage,
      }),
    });
  });

  await page.goto("/");
  await page.locator(".runtime-mode-select select").selectOption("live");

  await expect.poll(() => requestedModes).toEqual(["live"]);
  await expect(page.locator(".runtime-transition-feedback.error")).toContainText(blockedMessage);
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("llm");
  await expect(page.locator(".mission-layout")).toBeVisible();
});

test("waits for a fresh simulation state after changing bridge generation", async ({ page }) => {
  let runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  let socketGeneration = 0;
  let releaseLiveState: (() => void) | null = null;
  const serviceCalls: Array<{ generation: number; service: string }> = [];

  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9090\/?$/, (socket) => {
    socketGeneration += 1;
    const generation = socketGeneration;
    socket.onMessage((raw) => {
      const message = JSON.parse(typeof raw === "string" ? raw : raw.toString()) as {
        op?: string;
        id?: string;
        service?: string;
        topic?: string;
      };
      if (message.op === "subscribe" && message.topic === "/simulation/state") {
        const publishState = () => socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            procedure_id: "test-procedure",
            active_bundle: "thyroidectomy_v1",
            running: false,
            execution_state: "idle",
            filtered_phase: "P03",
            instrument_states: [{
              instrument_id: "grasper",
              home_location_type: "rack",
              home_location_id: "grasper",
              location_type: "rack",
              location_id: "grasper",
              owner: "none",
              status: "available",
              confidence: 0.9,
              cleanliness_state: "sterile",
              contaminated: false,
              lifecycle_stage: "home_rack",
              reserved_for: "",
              last_holder: "none",
              next_required_transition: "",
              visual_anchor_id: "grasper",
            }],
          },
        }));
        if (generation === 1) publishState();
        else releaseLiveState = publishState;
        return;
      }
      if (message.op === "subscribe" && message.topic === "/integration/readiness") {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            data: JSON.stringify({
              schema: "taskplanner.integration_readiness.v1",
              stamp_sec: Date.now() / 1_000,
              ready: true,
              checks: {
                contract_configuration: true,
                surgeon_sentence_publisher: true,
                tool_handover_action_server: true,
                retraction_command_service: true,
                perception_input: true,
              },
              missing: [],
              details: {
                active_bundle: "thyroidectomy_v1",
                procedure_type: "thyroidectomy",
                robot_endpoint_source: "virtual",
                retraction_state_machine_suppressed: true,
              },
            }),
          },
        }));
        return;
      }
      if (message.op !== "call_service" || !message.id || !message.service) return;
      serviceCalls.push({ generation, service: message.service });
      socket.send(JSON.stringify({
        op: "service_response",
        id: message.id,
        service: message.service,
        result: true,
        values: message.service === "/rosapi/topics"
          ? { topics: [], types: [] }
          : { success: true, message: "ok", model_ids: [] },
      }));
    });
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/transition", async (route) => {
    const request = route.request().postDataJSON() as { mode: LauncherMode };
    runtime = {
      phase: "starting",
      active_mode: runtime.active_mode,
      requested_mode: request.mode,
      retryable: false,
    };
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify(runtime) });
    setTimeout(() => {
      runtime = {
        phase: "idle",
        active_mode: request.mode,
        requested_mode: request.mode,
        retryable: false,
      };
    }, 80);
  });

  await page.goto("/");
  const startButton = page.getByRole("button", { name: "준비 중" });
  await expect.poll(() => socketGeneration).toBe(1);
  await expect(page.getByRole("button", { name: "수술 시작", exact: true })).toBeEnabled();
  serviceCalls.length = 0;

  await page.locator(".runtime-mode-select select").selectOption("live");
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("live", { timeout: 5_000 });
  await expect.poll(() => socketGeneration).toBeGreaterThanOrEqual(2);
  await expect.poll(() => releaseLiveState !== null).toBe(true);
  await expect(startButton).toBeDisabled();
  await page.waitForTimeout(250);
  expect(serviceCalls.filter((call) => call.generation >= 2)).toEqual([]);

  releaseLiveState?.();
  await expect(page.getByRole("button", { name: "수술 시작", exact: true })).toBeEnabled();
});

test("locks mission commands when the simulation-state heartbeat expires", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];
  let missionSocketCount = 0;
  let simulationSubscriptionCount = 0;

  await installRosbridgeStub(page, {
    simulationState: { running: true, execution_state: "running" },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
    onSocketConnect: (url) => {
      if (/ws:\/\/127\.0\.0\.1:9090\/?$/.test(url)) missionSocketCount += 1;
    },
    onSimulationSubscription: () => {
      simulationSubscriptionCount += 1;
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const pauseButton = page.getByRole("button", { name: "일시정지" });
  await expect(pauseButton).toBeEnabled();
  missionServiceCalls.length = 0;

  await expect(pauseButton).toBeDisabled({ timeout: 5_500 });
  // The WebSocket remains open, but the authoritative simulation heartbeat
  // has expired. Surface that distinction instead of calling a live transport
  // disconnected while keeping all mission commands locked.
  await expect(page.getByText("브리지 연결 · 상태 만료")).toBeVisible();
  await expect(page.locator(".dock-action-message.error")).toContainText("상태 갱신이 4초 이상 끊겨");
  await expect(page.locator(".dock-action-message.error")).toHaveAttribute("role", "alert");
  await pauseButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForTimeout(100);
  expect(missionServiceCalls).toEqual([]);

  // If the socket itself stays open but its state subscription silently dies,
  // fail closed first and then rebuild the transport instead of remaining
  // stale forever. The replacement stub publishes a fresh authoritative state.
  await expect.poll(() => missionSocketCount, { timeout: 10_000 }).toBeGreaterThanOrEqual(2);
  await expect.poll(() => simulationSubscriptionCount).toBeGreaterThanOrEqual(2);
  await expect(page.getByText("ROS 런타임 연결됨")).toBeVisible();
  await expect(page.getByRole("button", { name: "일시정지" })).toBeEnabled();
  expect(missionServiceCalls.filter((service) => service === "/simulation/control")).toEqual([]);
});

test("rejects a mission service call synchronously after heartbeat freshness expires", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];

  await installRosbridgeStub(page, {
    simulationState: { running: true, execution_state: "running" },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  const pauseButton = page.getByRole("button", { name: "일시정지" });
  await expect(pauseButton).toBeEnabled();
  missionServiceCalls.length = 0;

  // Dispatch the click in the same task as the stale clock jump. This closes
  // the gap before the periodic freshness sweep gets a chance to disable the
  // button and proves the bridge boundary, not only the DOM state, is safe.
  await page.evaluate(() => {
    const originalNow = Date.now;
    Date.now = () => originalNow() + 5_000;
  });
  await pauseButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForTimeout(100);
  expect(missionServiceCalls).toEqual([]);
});

test("distinguishes an open ROS transport from missing authoritative state", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  await installRosbridgeStub(page);
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.getByText("브리지 연결 · 상태 대기")).toBeVisible();
  await expect(
    page.getByText("ROS bridge connected. Waiting for fresh runtime state...", { exact: true }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: "준비 중", exact: true })).toBeDisabled();
});

test("reconnects an open ROS transport when its first authoritative state never arrives", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One real-time recovery timer run is sufficient.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  let missionSocketCount = 0;
  let simulationSubscriptionCount = 0;
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    withholdSimulationStateSubscriptions: 1,
    onSocketConnect: (url) => {
      if (/ws:\/\/127\.0\.0\.1:9090\/?$/.test(url)) missionSocketCount += 1;
    },
    onSimulationSubscription: () => {
      simulationSubscriptionCount += 1;
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.getByText("브리지 연결 · 상태 대기")).toBeVisible();
  await expect(page.getByText("ROS 재연결 중")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(/Fresh runtime state did not arrive/)).toHaveCount(0);
  await expect.poll(() => missionSocketCount, { timeout: 12_000 }).toBeGreaterThanOrEqual(2);
  await expect.poll(() => simulationSubscriptionCount).toBeGreaterThanOrEqual(2);
  await expect(page.getByText("ROS 런타임 연결됨")).toBeVisible();
  await expect(page.getByRole("button", { name: "수술 시작", exact: true })).toBeEnabled();
});

test("fails closed when the simulation state payload is malformed", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    simulationStateMessage: {
      procedure_id: "test-procedure",
      active_bundle: "thyroidectomy_v1",
      filtered_phase: "P03",
      running: false,
      execution_state: "corrupted",
      instrument_states: [],
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.getByText("브리지 연결 · 상태 오류")).toBeVisible();
  const startButton = page.getByRole("button", { name: "준비 중", exact: true });
  await expect(startButton).toBeDisabled();
  await startButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForTimeout(100);
  expect(missionServiceCalls).toEqual([]);
});

test("fails closed when the simulation state collection exceeds the UI bound", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];
  const instrument = {
    instrument_id: "grasper",
    home_location_type: "rack",
    home_location_id: "grasper",
    location_type: "rack",
    location_id: "grasper",
    owner: "none",
    status: "available",
    confidence: 0.9,
    cleanliness_state: "sterile",
    contaminated: false,
    lifecycle_stage: "home_rack",
  };
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    simulationStateMessage: {
      procedure_id: "test-procedure",
      active_bundle: "thyroidectomy_v1",
      filtered_phase: "P03",
      running: false,
      execution_state: "idle",
      instrument_states: Array.from({ length: 257 }, () => instrument),
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.getByText("브리지 연결 · 상태 오류")).toBeVisible();
  await expect(page.getByRole("button", { name: "준비 중", exact: true })).toBeDisabled();
  expect(missionServiceCalls).toEqual([]);
});

test("fails closed when the simulation authority identity is incomplete", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    simulationStateMessage: {
      procedure_id: "",
      active_bundle: "",
      filtered_phase: "P03",
      running: false,
      execution_state: "idle",
      instrument_states: [],
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.getByText("브리지 연결 · 상태 오류")).toBeVisible();
  await expect(page.getByRole("button", { name: "준비 중", exact: true })).toBeDisabled();
  expect(missionServiceCalls).toEqual([]);
});

test("fails closed when an instrument authority entry is malformed", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    simulationStateMessage: {
      procedure_id: "test-procedure",
      active_bundle: "thyroidectomy_v1",
      filtered_phase: "P03",
      running: false,
      execution_state: "idle",
      instrument_states: [{}],
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.getByText("브리지 연결 · 상태 오류")).toBeVisible();
  await expect(page.getByRole("button", { name: "준비 중", exact: true })).toBeDisabled();
  expect(missionServiceCalls).toEqual([]);
});

test("fails closed when instrument instance identities are duplicated", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];
  const instrument = {
    instrument_id: "grasper",
    instance_id: "grasper#1",
    home_location_type: "rack",
    home_location_id: "grasper",
    location_type: "rack",
    location_id: "grasper",
    owner: "none",
    status: "available",
    confidence: 0.9,
    cleanliness_state: "sterile",
    contaminated: false,
    lifecycle_stage: "home_rack",
    reserved_for: "",
    last_holder: "none",
    next_required_transition: "",
    visual_anchor_id: "grasper",
  };
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    simulationStateMessage: {
      procedure_id: "test-procedure",
      active_bundle: "thyroidectomy_v1",
      filtered_phase: "P03",
      running: false,
      execution_state: "idle",
      instrument_states: [instrument, { ...instrument, instrument_id: "scissors" }],
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.getByText("브리지 연결 · 상태 오류")).toBeVisible();
  await expect(page.getByRole("button", { name: "준비 중", exact: true })).toBeDisabled();
  expect(missionServiceCalls).toEqual([]);
});

test("fails closed when an instrument confidence is outside the probability range", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    simulationStateMessage: {
      procedure_id: "test-procedure",
      active_bundle: "thyroidectomy_v1",
      filtered_phase: "P03",
      running: false,
      execution_state: "idle",
      instrument_states: [{
        instrument_id: "grasper",
        home_location_type: "rack",
        home_location_id: "grasper",
        location_type: "rack",
        location_id: "grasper",
        owner: "none",
        status: "available",
        confidence: 1.2,
        cleanliness_state: "sterile",
        contaminated: false,
        lifecycle_stage: "home_rack",
        reserved_for: "",
        last_holder: "none",
        next_required_transition: "",
        visual_anchor_id: "grasper",
      }],
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.getByText("브리지 연결 · 상태 오류")).toBeVisible();
  await expect(page.getByRole("button", { name: "준비 중", exact: true })).toBeDisabled();
  expect(missionServiceCalls).toEqual([]);
});

test("fails closed when the simulation phase is missing", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    simulationStateMessage: {
      procedure_id: "test-procedure",
      active_bundle: "thyroidectomy_v1",
      filtered_phase: "",
      running: false,
      execution_state: "idle",
      instrument_states: [{
        instrument_id: "grasper",
        home_location_type: "rack",
        home_location_id: "grasper",
        location_type: "rack",
        location_id: "grasper",
        owner: "none",
        status: "available",
        confidence: 0.9,
        cleanliness_state: "sterile",
        contaminated: false,
        lifecycle_stage: "home_rack",
        reserved_for: "",
        last_holder: "none",
        next_required_transition: "",
        visual_anchor_id: "grasper",
      }],
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.getByText("브리지 연결 · 상태 오류")).toBeVisible();
  await expect(page.getByRole("button", { name: "준비 중", exact: true })).toBeDisabled();
  expect(missionServiceCalls).toEqual([]);
});

test("fails closed when the Replay authority payload is malformed", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "replay",
    requested_mode: "replay",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    shadowReplayState: {
      state: "paused",
      loaded: true,
      running: false,
      paused: true,
      completed: false,
    },
    shadowReplayStateMessage: {
      run_id: "test-run",
      procedure_id: "thyroidectomy",
      state: "corrupted",
      loaded: true,
      running: false,
      paused: true,
      completed: false,
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.getByText("브리지 연결 · 상태 오류")).toBeVisible();
  const startButton = page.getByRole("button", { name: "준비 중", exact: true });
  await expect(startButton).toBeDisabled();
  await startButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForTimeout(100);
  expect(missionServiceCalls).toEqual([]);
});

test("accepts the controller's initial loaded Replay state before a run ID exists", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "replay",
    requested_mode: "replay",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "halted" },
    shadowReplayState: {
      state: "ready",
      loaded: true,
      running: false,
      paused: false,
      completed: false,
    },
    shadowReplayStateMessage: {
      stamp: { sec: 1, nanosec: 0 },
      run_id: "",
      case_id: "0704_6",
      procedure_id: "thyroidectomy_demo",
      state: "ready",
      mode: "elastic_demo",
      loaded: true,
      running: false,
      paused: false,
      completed: false,
      source_time_sec: 0,
      duration_sec: 163,
      image_duration_sec: 138.4284,
      wall_elapsed_sec: 0,
      playback_rate: 0,
      elastic_hold_sec: 0,
      hold_reason: "",
      last_error: "",
      published_image_count: 0,
      published_transcript_count: 0,
      completed_vlm_count: 0,
      pending_vlm_count: 0,
      active_skill_count: 0,
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.locator('.system-pill[data-authority-status="ready"]')).toContainText(
    "ROS 런타임 연결됨",
  );
  const startButton = page.getByRole("button", { name: "수술 시작", exact: true });
  await expect(startButton).toBeEnabled();
  await expect(page.getByRole("combobox", { name: "재생 케이스 선택" })).toBeEnabled();

  // Stub-only command proof: an accepted pre-start heartbeat must cross the
  // synchronous authority gate. This does not run a real controller.
  await startButton.click();
  await expect.poll(() =>
    missionServiceCalls.filter((service) => service === "/simulation/control").length,
  ).toBe(1);
});

test("still rejects an active Replay state without a run ID", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "replay",
    requested_mode: "replay",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "halted" },
    shadowReplayState: {
      state: "running",
      loaded: true,
      running: true,
      paused: false,
      completed: false,
    },
    shadowReplayStateMessage: {
      stamp: { sec: 1, nanosec: 0 },
      run_id: "",
      case_id: "0704_6",
      procedure_id: "thyroidectomy_demo",
      state: "running",
      mode: "elastic_demo",
      loaded: true,
      running: true,
      paused: false,
      completed: false,
      source_time_sec: 1,
      duration_sec: 163,
      image_duration_sec: 138.4284,
      wall_elapsed_sec: 1,
      playback_rate: 1,
      elastic_hold_sec: 0,
      hold_reason: "",
      last_error: "",
      published_image_count: 1,
      published_transcript_count: 0,
      completed_vlm_count: 0,
      pending_vlm_count: 0,
      active_skill_count: 0,
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.locator('.system-pill[data-authority-status="invalid"]')).toContainText(
    "브리지 연결 · 상태 오류",
  );
  await expect(page.getByRole("button", { name: "준비 중", exact: true })).toBeDisabled();
  expect(missionServiceCalls.filter((service) => service === "/simulation/control")).toEqual([]);
});

test("explains why Replay controls wait for a case", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "replay",
    requested_mode: "replay",
    retryable: false,
  };

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    shadowReplayState: {
      state: "ready",
      loaded: false,
      running: false,
      paused: false,
      completed: false,
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("shadow");
  await expect(page.locator(".shadow-replay-status-note")).toContainText(
    "재생 케이스가 아직 로드되지 않았습니다",
  );
  await expect(page.getByRole("button", { name: "준비 중", exact: true })).toBeDisabled();
  await expect(page.getByRole("combobox", { name: "재생 케이스 선택" })).toBeEnabled();
});

test("keeps Replay waiting guidance inside compact viewports", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "replay",
    requested_mode: "replay",
    retryable: false,
  };

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    shadowReplayState: {
      state: "ready",
      loaded: false,
      running: false,
      paused: false,
      completed: false,
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.locator(".shadow-replay-status-note")).toBeVisible();
  for (const viewport of [
    { width: 320, height: 800 },
    { width: 390, height: 844 },
    { width: 768, height: 900 },
    { width: 1280, height: 800 },
  ]) {
    await page.setViewportSize(viewport);
    const metrics = await page.evaluate(() => {
      const root = document.documentElement;
      const note = document.querySelector<HTMLElement>(".shadow-replay-status-note");
      const noteBox = note?.getBoundingClientRect();
      const overflowing = Array.from(document.body.querySelectorAll<HTMLElement>("*"))
        .filter((element) => {
          const style = window.getComputedStyle(element);
          const bounds = element.getBoundingClientRect();
          return style.display !== "none" &&
            style.visibility !== "hidden" &&
            bounds.width > 0 &&
            (bounds.left < -1 || bounds.right > root.clientWidth + 1);
        })
        .map((element) => element.className || element.tagName);
      return {
        clientWidth: root.clientWidth,
        scrollWidth: root.scrollWidth,
        note: noteBox
          ? { left: noteBox.left, right: noteBox.right, width: noteBox.width }
          : null,
        overflowing,
      };
    });
    expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth + 1);
    expect(metrics.overflowing, `${viewport.width}px overflow: ${JSON.stringify(metrics.overflowing)}`).toEqual([]);
    expect(metrics.note?.width, `${viewport.width}px Replay note should render`).toBeGreaterThan(0);
    expect(metrics.note?.left).toBeGreaterThanOrEqual(-1);
    expect(metrics.note?.right).toBeLessThanOrEqual(metrics.clientWidth + 1);
  }
});

test("locks Replay case controls while a case is loading", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "replay",
    requested_mode: "replay",
    retryable: false,
  };

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    shadowReplayState: {
      state: "loading",
      loaded: false,
      running: false,
      paused: false,
      completed: false,
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.locator(".shadow-replay-status-note")).toContainText(
    "재생 케이스를 불러오는 중입니다",
  );
  await expect(page.getByRole("combobox", { name: "재생 케이스 선택" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "동기화" })).toBeDisabled();
});

test("ignores an oversized Replay service state snapshot", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "replay",
    requested_mode: "replay",
    retryable: false,
  };
  const oversizedState = JSON.stringify({
    state: "running",
    loaded: true,
    running: true,
    paused: false,
    ...Object.fromEntries(Array.from({ length: 513 }, (_, index) => [`extra-${index}`, index])),
  });

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    shadowReplayState: {
      state: "ready",
      loaded: true,
      running: false,
      paused: false,
      completed: false,
    },
    shadowControlStateJson: oversizedState,
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("shadow");
  await expect(page.locator(".shadow-replay-dock h2")).toHaveText("준비");
  await page.getByRole("button", { name: "동기화" }).click();
  await expect(page.locator(".shadow-replay-dock h2")).toHaveText("준비");
});

test("fails closed when Replay authority time or metrics are malformed", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "replay",
    requested_mode: "replay",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    shadowReplayState: {
      state: "ready",
      loaded: true,
      running: false,
      paused: false,
      completed: false,
    },
    shadowReplayStateMessage: {
      stamp: { sec: 1, nanosec: 1_000_000_000 },
      run_id: "test-run",
      case_id: "0704_6",
      procedure_id: "thyroidectomy",
      state: "ready",
      mode: "elastic_demo",
      loaded: true,
      running: false,
      paused: false,
      completed: false,
      source_time_sec: "invalid",
      duration_sec: 1,
      image_duration_sec: 1,
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
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.getByText("브리지 연결 · 상태 오류")).toBeVisible();
  await expect(page.getByRole("button", { name: "준비 중", exact: true })).toBeDisabled();
  expect(missionServiceCalls).toEqual([]);
});

test("uses Replay state freshness as the authoritative command gate", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "replay",
    requested_mode: "replay",
    retryable: false,
  };
  const missionServiceCalls: string[] = [];

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    shadowReplayState: {
      state: "running",
      loaded: true,
      running: true,
      paused: false,
      completed: false,
    },
    onMissionServiceCall: (service) => missionServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("shadow");
  const pauseButton = page.getByRole("button", { name: "일시정지" });
  await expect(pauseButton).toBeEnabled();
  missionServiceCalls.length = 0;

  await expect(pauseButton).toBeDisabled({ timeout: 5_500 });
  await expect(page.locator(".shadow-replay-dock h2")).toHaveText("unavailable");
  await pauseButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForTimeout(100);
  expect(missionServiceCalls).toEqual([]);
});

test("shows a reload recovery when the Multicam lazy chunk fails", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  await installRosbridgeStub(page);
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/src/components/multicam/MulticamOpsWorkspace.tsx*", (route) =>
    route.abort("failed"));

  await openLegacyMulticamWorkspace(page);

  await expect(page.getByRole("alert")).toContainText("멀티캠 관제 화면을 불러오지 못했습니다.");
  await expect(page.getByRole("button", { name: "페이지 다시 불러오기" })).toBeVisible();
});

test("opens the dedicated multicam observer without replacing Live", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const requestedModes: LauncherMode[] = [];
  const observerServiceCalls: string[] = [];

  await installRosbridgeStub(page, {
    publishCaptureStatus: true,
    captureStatusPublishLimit: 1,
    onServiceCall: (service) => observerServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify(runtime) });
  });

  await page.goto("/");
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("live");
  await openLegacyMulticamWorkspace(page);

  await expect(page.getByRole("heading", { name: "멀티캠 관제 콘솔" })).toBeVisible({ timeout: 5_000 });
  await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("multicam-main");
  await expect(page.getByText(/멀티캠 observer ready · CaptureStatus fresh/)).toBeVisible();
  await expect(page.getByText("Graph topic 발견")).toBeVisible();
  await expect(page.getByText(/color frame fresh 0\/5/)).toBeVisible();
  for (const buttonName of ["샘플 수집 시작", "수집 중지", "Solve · 저장 · TF 발행", "저장된 Anchor 다시 발행"]) {
    await expect(page.getByRole("button", { name: buttonName })).toBeDisabled();
  }
  expect(requestedModes).toEqual([]);
  expect(observerServiceCalls.length).toBeGreaterThan(0);
  expect(observerServiceCalls.every(
    (service) => service === "/multicam_observer/rosapi/topics",
  )).toBe(true);
  await expect(page.getByText(/\/multicam$/)).toBeVisible();
  await expect(page.getByText(/멀티캠 observer degraded · CaptureStatus stale/)).toBeVisible({ timeout: 5_000 });
  await expect.poll(
    () => page.locator(".ops-capture-card .ops-metric").evaluateAll((metrics) =>
      metrics.every((metric) => metric.className.includes("tone-warn")),
    ),
    { timeout: 5_000, intervals: [250] },
  ).toBe(true);
  const staleMetricClasses = await page.locator(".ops-capture-card .ops-metric").evaluateAll((metrics) =>
    metrics.map((metric) => metric.className),
  );
  expect(staleMetricClasses.every((className) => className.includes("tone-warn"))).toBe(true);
  await expect.poll(
    () => page.locator(".ops-capture-card tbody .ops-inline-status").evaluateAll((statuses) =>
      statuses.length >= 15 && statuses.every((status) => status.className.includes("warn")),
    ),
    { timeout: 5_000, intervals: [250] },
  ).toBe(true);
  const staleTableStatusClasses = await page.locator(".ops-capture-card tbody .ops-inline-status").evaluateAll((statuses) =>
    statuses.map((status) => status.className),
  );
  expect(staleTableStatusClasses.length).toBeGreaterThanOrEqual(15);
  expect(staleTableStatusClasses.every((className) => className.includes("warn"))).toBe(true);
  await expect(page.locator(".ops-capture-card tbody").first()).toContainText("마지막 상태");
  await page.getByRole("button", { name: "멀티캠 observer 재연결" }).click();
  await expect(page.getByText("CaptureStatus 대기")).toBeVisible();
  await expect(page.getByText(/CaptureStatus 토픽 발견 · 실제 메시지 수신 대기/)).toBeVisible();

  await page.getByRole("button", { name: "미션 화면" }).click();
  await expect(page.getByRole("heading", { name: "수술실 디지털 트윈" })).toBeVisible({ timeout: 5_000 });
  await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("mission-main");
  expect(requestedModes).toEqual([]);
});

test("returns safely from a direct Multicam deep link", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };

  await installRosbridgeStub(page, {
    publishCaptureStatus: true,
    captureStatusPublishLimit: 1,
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/?workspace=multicam");
  await expect(page.getByRole("heading", { name: "멀티캠 관제 콘솔" })).toBeVisible({ timeout: 5_000 });
  await page.getByRole("button", { name: "미션 화면" }).click();
  await expect(page.getByRole("heading", { name: "수술실 디지털 트윈" })).toBeVisible({ timeout: 5_000 });
  await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("mission-main");
  expect(new URL(page.url()).search).toBe("");
});

test("keeps silent observer discovery single-flight and cancels it on workspace exit", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One viewport is enough for the timed lifecycle stress case.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const observerServiceCalls: string[] = [];

  await installRosbridgeStub(page, {
    respondToObserverServices: false,
    onServiceCall: (service) => observerServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await openLegacyMulticamWorkspace(page);
  await expect(page.getByRole("heading", { name: "멀티캠 관제 콘솔" })).toBeVisible();
  await expect.poll(() => observerServiceCalls.length).toBe(1);
  await page.waitForTimeout(750);
  expect(observerServiceCalls).toEqual(["/multicam_observer/rosapi/topics"]);

  // The first call times out before the 5 s discovery interval, allowing one
  // bounded retry instead of accumulating overlapping response listeners.
  await expect.poll(() => observerServiceCalls.length, { timeout: 6_000 }).toBe(2);
  await page.getByRole("button", { name: "미션 화면" }).click();
  await expect(page.getByRole("heading", { name: "수술실 디지털 트윈" })).toBeVisible();
  await page.waitForTimeout(5_250);
  expect(observerServiceCalls).toEqual([
    "/multicam_observer/rosapi/topics",
    "/multicam_observer/rosapi/topics",
  ]);
});

test("reclaims multicam frame object URLs across rapid view changes and exit", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One viewport is enough for the object-URL lifecycle stress case.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  await page.addInitScript(() => {
    const audit = { created: [] as string[], revoked: [] as string[] };
    (window as unknown as { __taskplannerObjectUrlAudit: typeof audit }).__taskplannerObjectUrlAudit = audit;
    const createObjectURL = URL.createObjectURL.bind(URL);
    const revokeObjectURL = URL.revokeObjectURL.bind(URL);
    URL.createObjectURL = (blob: Blob | MediaSource) => {
      const value = createObjectURL(blob);
      audit.created.push(value);
      return value;
    };
    URL.revokeObjectURL = (value: string) => {
      audit.revoked.push(value);
      revokeObjectURL(value);
    };
  });
  await installRosbridgeStub(page, {
    publishCaptureStatus: true,
    publishObserverImages: true,
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  const createdCount = () => page.evaluate(() =>
    (window as unknown as { __taskplannerObjectUrlAudit: { created: string[] } })
      .__taskplannerObjectUrlAudit.created.length);
  const colorStreamCount = 5;
  const depthStreamCount = 2;
  const afterDepthCount = colorStreamCount + depthStreamCount;
  const afterColorReturnCount = afterDepthCount + colorStreamCount;
  await openLegacyMulticamWorkspace(page);
  await expect.poll(createdCount).toBeGreaterThanOrEqual(colorStreamCount);
  await page.getByRole("tab", { name: "Depth" }).click();
  await expect.poll(createdCount).toBeGreaterThanOrEqual(afterDepthCount);
  await page.getByRole("tab", { name: "Color" }).click();
  await expect.poll(createdCount).toBeGreaterThanOrEqual(afterColorReturnCount);
  await page.getByRole("button", { name: "미션 화면" }).click();
  await page.waitForTimeout(500);

  const audit = await page.evaluate(() =>
    (window as unknown as { __taskplannerObjectUrlAudit: { created: string[]; revoked: string[] } })
      .__taskplannerObjectUrlAudit);
  expect(audit.created.length).toBeGreaterThanOrEqual(afterColorReturnCount);
  const revoked = new Set(audit.revoked);
  expect(audit.created.every((value) => revoked.has(value))).toBe(true);
});

test("expires the last-known VLM input frame when its publisher stops", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One bounded VLM frame-lifecycle run is sufficient.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    publishVlmImages: true,
    // The page starts from the Production Live default before the runtime
    // controller authoritatively reveals LLM. Supply stopped simulation
    // authority so heavy Lab observations may subscribe, then publish one
    // frame for each possible bridge generation.
    vlmImagePublishLimit: 2,
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await page.getByRole("tab", { name: "VLM", exact: true }).click();
  const imageCard = page.locator(".detail-card").filter({ hasText: "VLM 모델 시각 문맥" });
  await expect(imageCard).toContainText("원본 모델 시각 문맥");
  await expect(imageCard).toContainText("도구 위치 근거 아님");
  await expect(imageCard).toContainText("마지막 수신");
  await expect(imageCard).toContainText("frame 없음", { timeout: 5_000 });
});

test("drops an oversized Multicam frame before creating an object URL", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One bounded payload run is sufficient for the image ingress guard.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  await page.addInitScript(() => {
    const audit = { created: [] as string[] };
    (window as unknown as { __taskplannerObjectUrlAudit: typeof audit }).__taskplannerObjectUrlAudit = audit;
    const createObjectURL = URL.createObjectURL.bind(URL);
    URL.createObjectURL = (blob: Blob | MediaSource) => {
      const value = createObjectURL(blob);
      audit.created.push(value);
      return value;
    };
  });
  await installRosbridgeStub(page, {
    publishCaptureStatus: true,
    publishObserverImages: true,
    observerImagePublishLimit: 1,
    observerImageMessage: {
      header: { frame_id: "oversized-camera" },
      format: "jpeg",
      data: "x".repeat(16 * 1024 * 1024 + 1),
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await openLegacyMulticamWorkspace(page);
  await expect(page.getByRole("heading", { name: "멀티캠 관제 콘솔" })).toBeVisible();
  await page.waitForTimeout(500);
  const created = await page.evaluate(() =>
    (window as unknown as { __taskplannerObjectUrlAudit: { created: string[] } })
      .__taskplannerObjectUrlAudit.created.length);
  expect(created).toBe(0);
});

test("bounds Multicam observer status collections before rendering", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One bounded observer-status run is sufficient for the ingress guard.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const oversizedCapture = {
    online_cameras: Array.from({ length: 200 }, (_, index) => `unexpected-${index}`),
    offline_cameras: Array.from({ length: 200 }, (_, index) => `offline-${index}`),
    all_cameras_online: true,
    cameras: Array.from({ length: 200 }, (_, index) => ({
      camera_name: `camera-${index}`,
      detect_rate_hz: 30,
      area_coverage: 0.5,
    })),
    capture_dir: "x".repeat(20_000),
  };
  const oversizedWorldTags = Object.fromEntries(Array.from({ length: 200 }, (_, index) => [
    `tag-${index}`,
    {
      role: "calibration",
      total: 3,
      per_camera: {
        [`unexpected-camera-${index}`]: { count: 2, fresh: true },
      },
    },
  ]));
  await installRosbridgeStub(page, {
    publishCaptureStatus: true,
    captureStatusPublishLimit: 1,
    observerCaptureStatusMessage: oversizedCapture,
    publishWorldStatus: true,
    observerWorldStatusMessage: {
      data: JSON.stringify({
        collecting: true,
        reference_frame: "map",
        world_frame: "world",
        message: "w".repeat(20_000),
        tags: oversizedWorldTags,
      }),
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await openLegacyMulticamWorkspace(page);
  await expect(page.getByRole("heading", { name: "멀티캠 관제 콘솔" })).toBeVisible();
  await expect(page.locator(".ops-kpi-grid .ops-metric").first().locator("strong")).toHaveText("0/5");
  await expect.poll(() => page.locator(".ops-world-tags article").count()).toBe(64);
  const capturePathLength = await page.locator(".ops-path code").textContent();
  expect(capturePathLength?.length ?? 0).toBeLessThanOrEqual(4_096);
  const worldMessage = await page.locator(".ops-world-message").textContent();
  expect(worldMessage?.length ?? 0).toBeLessThanOrEqual(4_096);
  const firstTagText = await page.locator(".ops-world-tags article").first().innerText();
  expect(firstTagText.length).toBeLessThan(1_000);
});

test("fails closed when Multicam CaptureStatus shape exceeds the UI bound", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One malformed observer-status run is sufficient for the ingress guard.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  const oversizedCapture = Object.fromEntries(
    Array.from({ length: 513 }, (_, index) => [`extra-${index}`, index]),
  );
  await installRosbridgeStub(page, {
    publishCaptureStatus: true,
    captureStatusPublishLimit: 1,
    observerCaptureStatusMessage: oversizedCapture,
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await openLegacyMulticamWorkspace(page);
  await expect(page.getByRole("heading", { name: "멀티캠 관제 콘솔" })).toBeVisible();
  await expect(page.getByText("CaptureStatus 대기")).toBeVisible();
  await expect(page.getByText(/CaptureStatus payload 무시/)).toBeVisible();
  await expect(page.locator('[data-slot="multicam-readiness-boundary"]'))
    .toHaveAttribute("role", "status");
  await expect(page.locator(".ops-disconnected-banner")).toContainText("fresh CaptureStatus 확인 전");
});

test("marks an old World Anchor heartbeat stale before showing its last state", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    publishWorldStatus: true,
    observerWorldStatusMessage: {
      data: JSON.stringify({
        collecting: true,
        reference_frame: "map",
        world_frame: "world",
        message: "anchor collection active",
        tags: {},
      }),
    },
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await openLegacyMulticamWorkspace(page);
  await expect(page.locator(".ops-world-card .ops-status-dot")).toHaveText("COLLECTING");
  await expect(page.locator(".ops-world-card .ops-status-dot")).toHaveText("STALE", { timeout: 5_000 });
  await expect(page.locator(".ops-world-message")).toContainText("마지막 상태");
  await expect(page.locator(".ops-world-message")).toHaveClass(/is-stale/);
  await expect(page.locator(".ops-world-tags")).toHaveClass(/is-stale/);
});

test("observes a running Replay without replacing its runtime", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "replay",
    requested_mode: "replay",
    retryable: false,
  };
  const requestedModes: LauncherMode[] = [];
  const observerServiceCalls: string[] = [];
  let shadowSubscribed = false;

  await installRosbridgeStub(page, {
    publishCaptureStatus: true,
    simulationState: { running: false, execution_state: "idle" },
    shadowReplayState: {
      state: "running",
      loaded: true,
      running: true,
      paused: false,
      completed: false,
    },
    onShadowSubscription: () => {
      shadowSubscribed = true;
    },
    onServiceCall: (service) => observerServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify(runtime) });
  });

  await page.goto("/");
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("shadow");
  await expect.poll(() => shadowSubscribed).toBe(true);
  observerServiceCalls.length = 0;
  await openLegacyMulticamWorkspace(page);

  await expect(page.getByRole("heading", { name: "멀티캠 관제 콘솔" })).toBeVisible();
  await expect(page.getByText(/멀티캠 observer ready · CaptureStatus fresh/)).toBeVisible();
  expect(requestedModes).toEqual([]);
  expect(observerServiceCalls.length).toBeGreaterThan(0);
  expect(observerServiceCalls.every(
    (service) => service === "/multicam_observer/rosapi/topics",
  )).toBe(true);
  expect(runtime.active_mode).toBe("replay");
});

test("keeps a stopped Replay while graph discovery waits for actual CaptureStatus", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "replay",
    requested_mode: "replay",
    retryable: false,
  };
  const requestedModes: LauncherMode[] = [];
  const observerServiceCalls: string[] = [];
  let shadowSubscribed = false;

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    shadowReplayState: {
      state: "stopped",
      loaded: true,
      running: false,
      paused: false,
      completed: false,
    },
    onShadowSubscription: () => {
      shadowSubscribed = true;
    },
    onServiceCall: (service) => observerServiceCalls.push(service),
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify(runtime) });
  });

  await page.goto("/");
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("shadow");
  await expect.poll(() => shadowSubscribed).toBe(true);
  observerServiceCalls.length = 0;
  await openLegacyMulticamWorkspace(page);

  await expect(page.getByRole("heading", { name: "멀티캠 관제 콘솔" })).toBeVisible({ timeout: 5_000 });
  await expect(page.getByText(/CaptureStatus 토픽 발견 · 실제 메시지 수신 대기/)).toBeVisible();
  await expect(page.getByText("CaptureStatus 대기")).toBeVisible();
  await expect(page.getByText(/전용 멀티캠 observer가 ready 상태가 아닙니다/)).toBeVisible();
  expect(requestedModes).toEqual([]);
  expect(observerServiceCalls.length).toBeGreaterThan(0);
  expect(observerServiceCalls.every(
    (service) => service === "/multicam_observer/rosapi/topics",
  )).toBe(true);
  expect(runtime.active_mode).toBe("replay");
});
