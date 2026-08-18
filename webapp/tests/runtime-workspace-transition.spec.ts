import { expect, test, type Page, type WebSocketRoute } from "playwright/test";

const localFrontendBaseUrl =
  process.env.PLAYWRIGHT_BASE_URL ??
  `http://127.0.0.1:${process.env.PLAYWRIGHT_WEB_PORT ?? "4173"}`;

type LauncherMode = "live" | "llm-surgeon" | "replay" | "debug";

type RuntimeStatus = {
  phase: "idle" | "starting" | "failed";
  active_mode: LauncherMode | null;
  requested_mode: LauncherMode | null;
  message?: string;
  retryable: boolean;
};

type RosbridgeStubOptions = {
  shadowReplayState?: {
    state: string;
    loaded: boolean;
    running: boolean;
    paused: boolean;
    completed: boolean;
  };
  onShadowSubscription?: () => void;
  simulationState?: {
    running: boolean;
    execution_state: string;
  };
  onSimulationSubscription?: () => void;
  observerAvailable?: boolean;
  publishCaptureStatus?: boolean;
  captureStatusPublishLimit?: number;
  publishObserverImages?: boolean;
  respondToObserverServices?: boolean;
  withholdMissionService?: string;
  onServiceCall?: (service: string) => void;
  onMissionServiceCall?: (service: string) => void;
};

function installRosbridgeStub(page: Page, options: RosbridgeStubOptions = {}) {
  let captureStatusPublishCount = 0;
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
          msg: {
            stamp: { sec: 1, nanosec: 0 },
            run_id: "test-run",
            case_id: "0704_6",
            procedure_id: "thyroidectomy",
            mode: "elastic_demo",
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
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            procedure_id: "test-procedure",
            active_bundle: "thyroidectomy_v1",
            filtered_phase: "P03",
            instrument_states: [{ instrument_id: "grasper" }],
            ...options.simulationState,
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
            online_cameras: ["cam_1", "cam_2", "cam_3", "cam_4", "flir"],
            offline_cameras: [],
            all_cameras_online: true,
            uptime_sec: 12,
            cameras: [],
          },
        }));
        return;
      }
      if (
        message.op === "subscribe" &&
        observerSocket &&
        options.publishObserverImages &&
        message.topic?.startsWith("/synced/")
      ) {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            header: { frame_id: "test-camera" },
            format: "png",
            data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
          },
        }));
        return;
      }
      if (message.op !== "call_service" || !message.id || !message.service) return;
      if (observerSocket) options.onServiceCall?.(message.service);
      else options.onMissionServiceCall?.(message.service);
      if (observerSocket && options.respondToObserverServices === false) return;
      if (!observerSocket && message.service === options.withholdMissionService) return;
      socket.send(JSON.stringify({
        op: "service_response",
        id: message.id,
        service: message.service,
        result: true,
        values: message.service === "/multicam_observer/rosapi/topics"
          ? options.observerAvailable === false
            ? { topics: [], types: [] }
            : {
                topics: ["/multicam_node/capture_status"],
                types: ["arpa_multicam_msgs/msg/CaptureStatus"],
              }
          : { success: true, message: "ok", model_ids: [] },
      }));
    });
  };
  return Promise.all([
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9090\/?$/, (socket) => handleSocket(socket)),
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9091(?:\/multicam)?\/?$/, (socket) => handleSocket(socket, true)),
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9099\/?$/, (socket) => handleSocket(socket)),
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

test("treats the same mode as a no-op after active-mode authority arrives", async ({ page }) => {
  const runtime: RuntimeStatus = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    retryable: false,
  };
  let statusRequests = 0;
  const requestedModes: LauncherMode[] = [];

  await installRosbridgeStub(page);
  await page.route("**/api/runtime/status", async (route) => {
    statusRequests += 1;
    if (statusRequests === 1) await new Promise((resolve) => setTimeout(resolve, 100));
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) });
  });
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify(runtime) });
  });

  await page.goto("/");
  const modeSelect = page.locator(".runtime-mode-select select");
  await expect(modeSelect).toHaveValue("llm");
  await page.waitForTimeout(250);
  const statusRequestsBeforeSelection = statusRequests;
  await modeSelect.selectOption("llm");

  await expect.poll(() => statusRequests).toBeGreaterThan(statusRequestsBeforeSelection);
  expect(requestedModes).toEqual([]);
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
  await installRosbridgeStub(page);
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));

  await page.goto("/");

  await expect(page.locator(".runtime-mode-select select")).toHaveValue("shadow");
  await expect.poll(() => page.evaluate(() =>
    Object.entries(window.localStorage).some(
      ([key, value]) => key.startsWith("taskplanner.runtimeMode.") && value === "shadow",
    ),
  )).toBe(true);
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
  const debugButton = page.getByRole("button", { name: "디버그 모드" });
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

  await installRosbridgeStub(page, {
    simulationState: { running: false, execution_state: "idle" },
    withholdMissionService: "/simulation/control",
  });
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/transition", async (route) => {
    requestedModes.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify(runtime) });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "시작", exact: true }).click();
  await expect(page.locator(".dock-action-message.pending")).toContainText("Starting simulation");
  const modeSelect = page.locator(".runtime-mode-select select");
  const debugButton = page.getByRole("button", { name: "디버그 모드" });
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

  const debugButton = page.getByRole("button", { name: "디버그 모드" });
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
            instrument_states: [{ instrument_id: "grasper" }],
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
  await expect(page.getByRole("button", { name: "시작", exact: true })).toBeEnabled();
  serviceCalls.length = 0;

  await page.locator(".runtime-mode-select select").selectOption("live");
  await expect(page.locator(".runtime-mode-select select")).toHaveValue("live", { timeout: 5_000 });
  await expect.poll(() => socketGeneration).toBeGreaterThanOrEqual(2);
  await expect.poll(() => releaseLiveState !== null).toBe(true);
  await expect(startButton).toBeDisabled();
  await page.waitForTimeout(250);
  expect(serviceCalls.filter((call) => call.generation >= 2)).toEqual([]);

  releaseLiveState?.();
  await expect(page.getByRole("button", { name: "시작", exact: true })).toBeEnabled();
});

test("locks mission commands when the simulation-state heartbeat expires", async ({ page }) => {
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

  await expect(pauseButton).toBeDisabled({ timeout: 5_500 });
  await expect(page.getByText("ROS 끊김")).toBeVisible();
  await pauseButton.evaluate((button) => {
    button.removeAttribute("disabled");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await page.waitForTimeout(100);
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

  await page.goto("/");
  await page.getByRole("button", { name: "멀티캠 관제" }).click();

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
  await page.getByRole("button", { name: "멀티캠 관제" }).click();

  await expect(page.getByRole("heading", { name: "멀티캠 관제 콘솔" })).toBeVisible({ timeout: 5_000 });
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
  await page.getByRole("button", { name: "멀티캠 observer 재연결" }).click();
  await expect(page.getByText("CaptureStatus 대기")).toBeVisible();
  await expect(page.getByText(/CaptureStatus 토픽 발견 · 실제 메시지 수신 대기/)).toBeVisible();

  await page.getByRole("button", { name: "미션 화면" }).click();
  await expect(page.getByRole("heading", { name: "수술실 디지털 트윈" })).toBeVisible({ timeout: 5_000 });
  expect(requestedModes).toEqual([]);
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

  await page.goto("/");
  await page.getByRole("button", { name: "멀티캠 관제" }).click();
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
  await page.goto("/");
  await page.getByRole("button", { name: "멀티캠 관제" }).click();
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
  await page.getByRole("button", { name: "멀티캠 관제" }).click();

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
  await page.getByRole("button", { name: "멀티캠 관제" }).click();

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
