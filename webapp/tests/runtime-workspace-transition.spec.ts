import { expect, test, type Page, type WebSocketRoute } from "playwright/test";

const localFrontendBaseUrl =
  process.env.PLAYWRIGHT_BASE_URL ??
  `http://127.0.0.1:${process.env.PLAYWRIGHT_WEB_PORT ?? "4173"}`;

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
  publishMissionImages?: boolean;
  missionImagePublishLimit?: number;
  missionImageTopic?: string;
  missionImageMessage?: unknown;
  publishVlmImages?: boolean;
  vlmImagePublishLimit?: number;
  vlmImageMessage?: unknown;
  publishPerceptionHealth?: boolean;
  perceptionHealthMessage?: unknown;
  publishVlmHealth?: boolean;
  vlmHealthPublishLimit?: number;
  vlmHealthMessage?: unknown;
  respondToObserverServices?: boolean;
  liveAsrStatus?: Record<string, unknown>;
  liveAsrStatusMessage?: unknown;
  withholdMissionService?: string;
  onServiceCall?: (service: string) => void;
  onMissionServiceCall?: (service: string) => void;
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

function installRosbridgeStub(page: Page, options: RosbridgeStubOptions = {}) {
  let captureStatusPublishCount = 0;
  let observerImagePublishCount = 0;
  let missionImagePublishCount = 0;
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
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            ...(options.liveAsrStatusMessage ?? {
              data: JSON.stringify({
                schema: "taskplanner.asr.status.v1",
                stamp_sec: 1,
                asr: options.liveAsrStatus,
              }),
            }),
          },
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/surgery/perception/rfdetr/health" &&
        !observerSocket &&
        options.publishPerceptionHealth
      ) {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: options.perceptionHealthMessage ?? {
            data: JSON.stringify({
              schema: "taskplanner.rfdetr_health.v1",
              enabled: true,
              connected: true,
              status: "ready",
              latency_ms: 40,
              last_error: "",
            }),
          },
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === (options.missionImageTopic ?? "/surgery/images/cam4/detected/compressed") &&
        !observerSocket &&
        options.publishMissionImages &&
        missionImagePublishCount < (options.missionImagePublishLimit ?? Number.POSITIVE_INFINITY)
      ) {
        missionImagePublishCount += 1;
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: options.missionImageMessage ?? {
            header: { frame_id: "test-perception-camera" },
            format: "png",
            data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
          },
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        message.topic === "/surgery/images/field/compressed" &&
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
      if (observerSocket && options.respondToObserverServices === false) return;
      if (!observerSocket && message.service === options.withholdMissionService) return;
      const values = observerSocket && message.service === "/integration/debug/command"
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
      socket.send(JSON.stringify({
        op: "service_response",
        id: message.id,
        service: message.service,
        result: true,
        values,
      }));
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
      requested_mode: "llm-surgeon",
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
    await expect(modeSelect).toHaveValue("llm");
    if (recoveryCase === "unavailable status service") {
      await expect(page.locator(".runtime-transition-feedback.error")).toBeVisible();
      await page.getByRole("button", { name: "다시 시도" }).click();
    } else {
      await expect(page.locator(".runtime-transition-feedback.error")).toContainText(
        "실행 중인 런타임이 없습니다.",
      );
      await page.getByRole("button", { name: "현재 모드 시작" }).click();
    }

    await expect.poll(() => requestedModes).toEqual(["llm-surgeon"]);
    await expect(modeSelect).toHaveValue("llm");
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
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("llm");
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
  await expect(modeSelect).toHaveValue("llm");
  const debugButton = page.getByRole("button", { name: "독립 Debug" });
  await expect(debugButton).toBeDisabled();
  await expect(debugButton).toHaveAttribute("title", /현재 런타임 상태를 확인/);
  // The bridge is intentionally held closed until runtime authority arrives;
  // surface that as a pending handshake instead of a false transport failure.
  await expect(page.getByText("런타임 확인 중")).toBeVisible();
  releaseStatus();
  await expect(modeSelect).toBeEnabled();
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
  const debugButton = page.getByRole("button", { name: "독립 Debug" });
  await expect(debugButton).toBeDisabled();
  await debugButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
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
  const debugButton = page.getByRole("button", { name: "독립 Debug" });
  await expect(modeSelect).toBeDisabled();
  await expect(debugButton).toBeDisabled();
  await expect(page.locator("#runtime-mode-lock-note")).toContainText("제어 요청의 결과");

  await debugButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await modeSelect.evaluate((select) => {
    select.removeAttribute("disabled");
    (select as HTMLSelectElement).value = "debug";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await page.waitForTimeout(150);

  await expect(page.locator(".mission-layout")).toBeVisible();
  expect(requestedModes).toEqual([]);
});

test("keeps bundle changes single-flight while the service admission is pending", async ({ page }) => {
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

  await bundleSelect.evaluate((element) => {
    element.removeAttribute("disabled");
    element.dispatchEvent(new Event("change", { bubbles: true }));
    element.setAttribute("disabled", "");
  });
  await expect.poll(() =>
    missionServiceCalls.filter((service) => service === "/simulation/select_bundle").length,
  ).toBe(1);
  await expect(page.locator(".dock-action-message.pending")).toContainText("Applying bundle");
  await bundleSelect.evaluate((element) => {
    element.removeAttribute("disabled");
    element.dispatchEvent(new Event("change", { bubbles: true }));
    element.setAttribute("disabled", "");
  });
  await page.waitForTimeout(100);

  expect(missionServiceCalls.filter((service) => service === "/simulation/select_bundle")).toHaveLength(1);
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
      devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16_000, default: true }],
      device_id: 7,
      device_name: "Test USB microphone",
      device_status: "READY",
      route_policy: "cloud",
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
  await expect(page.locator(".live-asr-message.error")).toContainText("ASR 상태 토픽을 기다리는 중입니다.");
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
      devices,
      device_id: 0,
      device_name: "Input 0",
      device_status: "READY",
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
      devices: [{ id: 7, name: "Test USB microphone", input_channels: 1, default_samplerate: 16000, default: true }],
      device_id: 7,
      device_name: "Test USB microphone",
      device_status: "READY",
      device_message: "ready",
      route_policy: "cloud",
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

  const debugButton = page.getByRole("button", { name: "독립 Debug" });
  await expect(debugButton).toBeDisabled();
  await debugButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
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
  await expect(page.getByText("ROS 제어 준비")).toBeVisible();
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
  await expect(page.getByText("ROS 제어 준비")).toBeVisible();
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
    "ROS 제어 준비",
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
  await openLegacyMulticamWorkspace(page);
  await expect.poll(createdCount).toBeGreaterThanOrEqual(5);
  await page.getByRole("tab", { name: "Depth" }).click();
  await expect.poll(createdCount).toBeGreaterThanOrEqual(9);
  await page.getByRole("tab", { name: "Color" }).click();
  await expect.poll(createdCount).toBeGreaterThanOrEqual(14);
  await page.getByRole("button", { name: "미션 화면" }).click();
  await page.waitForTimeout(500);

  const audit = await page.evaluate(() =>
    (window as unknown as { __taskplannerObjectUrlAudit: { created: string[]; revoked: string[] } })
      .__taskplannerObjectUrlAudit);
  expect(audit.created.length).toBeGreaterThanOrEqual(14);
  const revoked = new Set(audit.revoked);
  expect(audit.created.every((value) => revoked.has(value))).toBe(true);
});

test("expires a derived perception preview when the detector stops publishing", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One viewport is enough for the perception preview freshness guard.");
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "live",
    requested_mode: "live",
    retryable: false,
  };
  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    publishPerceptionHealth: true,
    publishMissionImages: true,
    missionImagePublishLimit: 1,
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await page.getByRole("button", { name: "CAM4", exact: true }).click();
  const camera = page.locator('[data-slot="stage-camera-toggle-viewport"][data-camera-id="cam4"]');
  await expect(camera).toHaveAttribute("data-camera-id", "cam4");
  await expect(camera.locator("img.stage-camera-frame")).toBeVisible();
  await expect(camera).toContainText("인식 결과");

  await expect.poll(
    () => camera.locator("img.stage-camera-frame").count(),
    { timeout: 6_000, intervals: [500] },
  ).toBe(0);
  await expect(camera).toContainText("인식 결과 대기");
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
    publishVlmImages: true,
    vlmImagePublishLimit: 1,
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");
  await page.getByRole("tab", { name: "VLM", exact: true }).click();
  const imageCard = page.locator(".detail-card").filter({ hasText: "VLM 입력 영상" });
  await expect(imageCard).toContainText("RF-DETR 분할 FLIR");
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
