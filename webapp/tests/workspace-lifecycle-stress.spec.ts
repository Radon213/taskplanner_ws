import { expect, test, type Page, type WebSocketRoute } from "playwright/test";

type LauncherMode = "live" | "llm-surgeon" | "replay" | "debug";
type SocketKind = "mission" | "debug" | "multicam";

type BrowserResourceSnapshot = {
  activeWebSockets: number;
  activeIntervals: number;
  activeTimeouts: number;
  activeAnimationFrames: number;
  activeObjectUrls: number;
  activeResizeObservers: number;
  activeGlobalListeners: number;
  createdWebSockets: number;
  closedWebSockets: number;
};

type BrowserResourceAudit = {
  snapshot: () => BrowserResourceSnapshot;
};

const onePixelPng =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

function debugStatus(connection: number): Record<string, unknown> {
  return {
    schema: "taskplanner.integration_debug.status.v1",
    stamp_sec: Date.now() / 1_000,
    session: {
      session_id: `stress-${connection}`,
      state: "MONITOR_ONLY",
      armed: false,
      fault_locked: false,
      last_error: "",
      event_log_path: "/tmp/stress-events.jsonl",
    },
    runtime: {
      ros_domain_id: "0",
      rmw_implementation: "rmw_fastrtps_cpp",
      discovery_range: "LOCALHOST",
      blocked_nodes: [],
      operational_runtime_stopped: true,
      manual_control_available: true,
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
    asr: { state: "STOPPED" },
    surgery_record: { state: "IDLE", history: [] },
    recent_events: [],
  };
}

async function installBrowserResourceAudit(page: Page) {
  await page.addInitScript(() => {
    const activeIntervals = new Set<number>();
    const activeTimeouts = new Set<number>();
    const activeAnimationFrames = new Set<number>();
    const activeObjectUrls = new Set<string>();
    const activeWebSockets = new Set<WebSocket>();
    const resizeObserverTargets = new Map<ResizeObserver, Set<Element>>();
    const windowListeners = new Map<string, Set<EventListenerOrEventListenerObject>>();
    const documentListeners = new Map<string, Set<EventListenerOrEventListenerObject>>();
    let createdWebSockets = 0;
    let closedWebSockets = 0;

    const nativeSetInterval = window.setInterval.bind(window);
    const nativeClearInterval = window.clearInterval.bind(window);
    window.setInterval = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
      const id = nativeSetInterval(handler, timeout, ...args);
      activeIntervals.add(id);
      return id;
    }) as typeof window.setInterval;
    window.clearInterval = ((id?: number) => {
      if (typeof id === "number") activeIntervals.delete(id);
      nativeClearInterval(id);
    }) as typeof window.clearInterval;

    const nativeSetTimeout = window.setTimeout.bind(window);
    const nativeClearTimeout = window.clearTimeout.bind(window);
    window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
      let id = 0;
      const wrapped = typeof handler === "function"
        ? () => {
            activeTimeouts.delete(id);
            handler(...args);
          }
        : handler;
      id = nativeSetTimeout(wrapped, timeout);
      activeTimeouts.add(id);
      return id;
    }) as typeof window.setTimeout;
    window.clearTimeout = ((id?: number) => {
      if (typeof id === "number") activeTimeouts.delete(id);
      nativeClearTimeout(id);
    }) as typeof window.clearTimeout;

    const nativeRequestAnimationFrame = window.requestAnimationFrame.bind(window);
    const nativeCancelAnimationFrame = window.cancelAnimationFrame.bind(window);
    window.requestAnimationFrame = ((callback: FrameRequestCallback) => {
      let id = 0;
      id = nativeRequestAnimationFrame((time) => {
        activeAnimationFrames.delete(id);
        callback(time);
      });
      activeAnimationFrames.add(id);
      return id;
    }) as typeof window.requestAnimationFrame;
    window.cancelAnimationFrame = ((id: number) => {
      activeAnimationFrames.delete(id);
      nativeCancelAnimationFrame(id);
    }) as typeof window.cancelAnimationFrame;

    const nativeCreateObjectURL = URL.createObjectURL.bind(URL);
    const nativeRevokeObjectURL = URL.revokeObjectURL.bind(URL);
    URL.createObjectURL = (value: Blob | MediaSource) => {
      const url = nativeCreateObjectURL(value);
      activeObjectUrls.add(url);
      return url;
    };
    URL.revokeObjectURL = (url: string) => {
      activeObjectUrls.delete(url);
      nativeRevokeObjectURL(url);
    };

    const NativeWebSocket = window.WebSocket;
    class AuditedWebSocket extends NativeWebSocket {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        createdWebSockets += 1;
        activeWebSockets.add(this);
        this.addEventListener("close", () => {
          if (!activeWebSockets.delete(this)) return;
          closedWebSockets += 1;
        }, { once: true });
      }
    }
    window.WebSocket = AuditedWebSocket;

    const NativeResizeObserver = window.ResizeObserver;
    class AuditedResizeObserver extends NativeResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        super(callback);
        resizeObserverTargets.set(this, new Set());
      }

      observe(target: Element, options?: ResizeObserverOptions) {
        resizeObserverTargets.get(this)?.add(target);
        super.observe(target, options);
      }

      unobserve(target: Element) {
        resizeObserverTargets.get(this)?.delete(target);
        super.unobserve(target);
      }

      disconnect() {
        resizeObserverTargets.delete(this);
        super.disconnect();
      }
    }
    window.ResizeObserver = AuditedResizeObserver;

    const trackListener = (
      registry: Map<string, Set<EventListenerOrEventListenerObject>>,
      type: string,
      listener: EventListenerOrEventListenerObject | null,
      add: boolean,
    ) => {
      if (!listener) return;
      const listeners = registry.get(type) ?? new Set<EventListenerOrEventListenerObject>();
      if (add) listeners.add(listener);
      else listeners.delete(listener);
      if (listeners.size) registry.set(type, listeners);
      else registry.delete(type);
    };
    const nativeWindowAdd = window.addEventListener.bind(window);
    const nativeWindowRemove = window.removeEventListener.bind(window);
    window.addEventListener = ((type: string, listener: EventListenerOrEventListenerObject | null, options?: boolean | AddEventListenerOptions) => {
      trackListener(windowListeners, type, listener, true);
      nativeWindowAdd(type, listener, options);
    }) as typeof window.addEventListener;
    window.removeEventListener = ((type: string, listener: EventListenerOrEventListenerObject | null, options?: boolean | EventListenerOptions) => {
      trackListener(windowListeners, type, listener, false);
      nativeWindowRemove(type, listener, options);
    }) as typeof window.removeEventListener;
    const nativeDocumentAdd = document.addEventListener.bind(document);
    const nativeDocumentRemove = document.removeEventListener.bind(document);
    document.addEventListener = ((type: string, listener: EventListenerOrEventListenerObject | null, options?: boolean | AddEventListenerOptions) => {
      trackListener(documentListeners, type, listener, true);
      nativeDocumentAdd(type, listener, options);
    }) as typeof document.addEventListener;
    document.removeEventListener = ((type: string, listener: EventListenerOrEventListenerObject | null, options?: boolean | EventListenerOptions) => {
      trackListener(documentListeners, type, listener, false);
      nativeDocumentRemove(type, listener, options);
    }) as typeof document.removeEventListener;

    const listenerCount = (registry: Map<string, Set<EventListenerOrEventListenerObject>>) =>
      Array.from(registry.values()).reduce((total, listeners) => total + listeners.size, 0);
    const audit: BrowserResourceAudit = {
      snapshot: () => ({
        activeWebSockets: activeWebSockets.size,
        activeIntervals: activeIntervals.size,
        activeTimeouts: activeTimeouts.size,
        activeAnimationFrames: activeAnimationFrames.size,
        activeObjectUrls: activeObjectUrls.size,
        activeResizeObservers: Array.from(resizeObserverTargets.values())
          .filter((targets) => targets.size > 0).length,
        activeGlobalListeners: listenerCount(windowListeners) + listenerCount(documentListeners),
        createdWebSockets,
        closedWebSockets,
      }),
    };
    (window as unknown as { __taskplannerResourceAudit: BrowserResourceAudit })
      .__taskplannerResourceAudit = audit;
  });
}

async function resourceSnapshot(page: Page): Promise<BrowserResourceSnapshot> {
  return page.evaluate(() =>
    (window as unknown as { __taskplannerResourceAudit: BrowserResourceAudit })
      .__taskplannerResourceAudit.snapshot());
}

async function installStressStubs(page: Page) {
  let runtime: {
    phase: "idle";
    active_mode: LauncherMode;
    requested_mode: LauncherMode;
    message: string;
    retryable: boolean;
  } = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    message: "Selected runtime is ready.",
    retryable: false,
  };
  const sockets: Record<SocketKind, WebSocketRoute[]> = {
    mission: [],
    debug: [],
    multicam: [],
  };
  const subscriptions = new Map<WebSocketRoute, Set<string>>();
  const socketKinds = new Map<WebSocketRoute, SocketKind>();
  const transitions: LauncherMode[] = [];

  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/transition", async (route) => {
    const mode = (route.request().postDataJSON() as { mode: LauncherMode }).mode;
    transitions.push(mode);
    runtime = {
      phase: "idle",
      active_mode: mode,
      requested_mode: mode,
      message: "Selected runtime is ready.",
      retryable: false,
    };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) });
  });

  const handleSocket = (socket: WebSocketRoute, kind: SocketKind) => {
    sockets[kind].push(socket);
    socketKinds.set(socket, kind);
    const activeSubscriptions = new Set<string>();
    subscriptions.set(socket, activeSubscriptions);
    const connection = sockets[kind].length;
    let closed = false;
    socket.onClose((code, reason) => {
      if (closed) return;
      closed = true;
      activeSubscriptions.clear();
      void socket.close({ code, reason });
    });
    socket.onMessage((raw) => {
      const message = JSON.parse(typeof raw === "string" ? raw : raw.toString()) as {
        op?: string;
        id?: string;
        service?: string;
        topic?: string;
      };
      if (message.op === "subscribe" && message.topic) {
        activeSubscriptions.add(message.id || message.topic);
        if (kind === "mission" && message.topic === "/simulation/state") {
          socket.send(JSON.stringify({
            op: "publish",
            topic: message.topic,
            msg: {
              procedure_id: "stress-procedure",
              active_bundle: "thyroidectomy_v1",
              filtered_phase: "P03",
              running: false,
              execution_state: "idle",
              instrument_states: [{ instrument_id: "grasper" }],
            },
          }));
        } else if (kind === "debug" && message.topic === "/integration/debug/status") {
          socket.send(JSON.stringify({
            op: "publish",
            topic: message.topic,
            msg: { data: JSON.stringify(debugStatus(connection)) },
          }));
        } else if (kind === "multicam" && message.topic === "/multicam_node/capture_status") {
          socket.send(JSON.stringify({
            op: "publish",
            topic: message.topic,
            msg: {
              online_cameras: ["cam_1", "cam_2", "cam_3", "cam_4", "flir"],
              offline_cameras: [],
              all_cameras_online: true,
              uptime_sec: connection,
              cameras: [],
            },
          }));
        } else if (kind === "multicam" && message.topic.startsWith("/synced/")) {
          socket.send(JSON.stringify({
            op: "publish",
            topic: message.topic,
            msg: {
              header: { frame_id: "stress-camera" },
              format: "png",
              data: onePixelPng,
            },
          }));
        }
        return;
      }
      if (message.op === "unsubscribe") {
        activeSubscriptions.delete(message.id || message.topic || "");
        return;
      }
      if (message.op !== "call_service" || !message.id || !message.service) return;
      socket.send(JSON.stringify({
        op: "service_response",
        id: message.id,
        service: message.service,
        result: true,
        values: message.service === "/multicam_observer/rosapi/topics"
          ? {
              topics: ["/multicam_node/capture_status"],
              types: ["arpa_multicam_msgs/msg/CaptureStatus"],
            }
          : { success: true, accepted: true, message: "ok", model_ids: [] },
      }));
    });
  };

  await Promise.all([
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9090\/?$/, (socket) => handleSocket(socket, "mission")),
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9091\/multicam\/?$/, (socket) => handleSocket(socket, "multicam")),
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9091\/?$/, (socket) => handleSocket(socket, "debug")),
  ]);

  return {
    connectionCount: (kind: SocketKind) => sockets[kind].length,
    activeSubscriptionCount: () => Array.from(subscriptions.values())
      .reduce((total, active) => total + active.size, 0),
    activeSubscriptions: () => Array.from(subscriptions.entries())
      .filter(([, active]) => active.size > 0)
      .map(([socket, active]) => ({
        kind: socketKinds.get(socket) ?? "mission",
        topics: Array.from(active).sort(),
      })),
    transitions,
    closeLatest: async (kind: SocketKind) => {
      const socket = sockets[kind].at(-1);
      if (!socket) throw new Error(`No ${kind} socket is available to close.`);
      subscriptions.get(socket)?.clear();
      await socket.close({ code: 1012, reason: "stress reconnect" });
    },
  };
}

test("returns browser and ROS resources to baseline after 30 workspace cycles", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "The 30-cycle lifecycle stress only needs one viewport.");
  test.setTimeout(180_000);
  await installBrowserResourceAudit(page);
  const bridge = await installStressStubs(page);
  await page.goto("/");

  const missionHeading = page.getByRole("heading", { name: "수술실 디지털 트윈" });
  const startButton = page.getByRole("button", { name: "시작", exact: true });
  await expect(missionHeading).toBeVisible();
  await expect(startButton).toBeEnabled();
  await page.waitForTimeout(650);
  const baseline = await resourceSnapshot(page);
  const baselineSubscriptions = bridge.activeSubscriptionCount();
  expect(baseline.activeObjectUrls).toBe(0);
  expect(baseline.activeResizeObservers).toBeGreaterThanOrEqual(1);

  for (let cycle = 1; cycle <= 30; cycle += 1) {
    await page.getByRole("button", { name: "디버그 모드" }).click();
    await expect(page.getByRole("heading", { name: "디버그 모드" })).toBeVisible();
    await expect(page.getByRole("button", { name: "수동 제어 활성화" })).toBeEnabled();
    if (cycle % 10 === 0) {
      const connectionCount = bridge.connectionCount("debug");
      await bridge.closeLatest("debug");
      await expect.poll(() => bridge.connectionCount("debug"), { timeout: 4_000 })
        .toBe(connectionCount + 1);
      await expect(page.getByRole("button", { name: "수동 제어 활성화" })).toBeEnabled();
    }
    await page.getByRole("button", { name: "운영 화면으로" }).click();
    await expect(missionHeading).toBeVisible();
    await expect(startButton).toBeEnabled();

    await page.getByRole("button", { name: "멀티캠 관제" }).click();
    await expect(page.getByRole("heading", { name: "멀티캠 관제 콘솔" })).toBeVisible();
    await expect(page.getByText(/멀티캠 observer ready · CaptureStatus fresh/)).toBeVisible();
    await page.getByRole("tab", { name: "Depth" }).click();
    await page.getByRole("tab", { name: "Color" }).click();
    if (cycle % 10 === 0) {
      const connectionCount = bridge.connectionCount("multicam");
      await bridge.closeLatest("multicam");
      await expect.poll(() => bridge.connectionCount("multicam"), { timeout: 4_500 })
        .toBe(connectionCount + 1);
      await expect(page.getByText(/멀티캠 observer ready · CaptureStatus fresh/)).toBeVisible();
    }
    await page.getByRole("button", { name: "미션 화면" }).click();
    await expect(missionHeading).toBeVisible();
    await expect(startButton).toBeEnabled();

    if (cycle % 10 === 0) {
      const connectionCount = bridge.connectionCount("mission");
      await bridge.closeLatest("mission");
      await expect.poll(() => bridge.connectionCount("mission"), { timeout: 4_500 })
        .toBe(connectionCount + 1);
      await expect(startButton).toBeEnabled();
    }

    await page.waitForTimeout(500);
    const current = await resourceSnapshot(page);
    expect(current.activeWebSockets).toBe(baseline.activeWebSockets);
    expect(current.activeIntervals).toBe(baseline.activeIntervals);
    expect(current.activeTimeouts).toBe(baseline.activeTimeouts);
    expect(current.activeAnimationFrames).toBe(baseline.activeAnimationFrames);
    expect(current.activeObjectUrls).toBe(baseline.activeObjectUrls);
    expect(current.activeResizeObservers).toBe(baseline.activeResizeObservers);
    expect(current.activeGlobalListeners).toBe(baseline.activeGlobalListeners);
    expect(
      bridge.activeSubscriptionCount(),
      JSON.stringify(bridge.activeSubscriptions(), null, 2),
    ).toBe(baselineSubscriptions);
  }

  const final = await resourceSnapshot(page);
  expect(final.createdWebSockets - final.closedWebSockets).toBe(final.activeWebSockets);
  expect(bridge.transitions).toHaveLength(60);
  expect(bridge.transitions.filter((mode) => mode === "debug")).toHaveLength(30);
  expect(bridge.transitions.filter((mode) => mode === "llm-surgeon")).toHaveLength(30);
});
