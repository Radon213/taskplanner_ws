import { expect, test, type Page, type WebSocketRoute } from "playwright/test";

type DebugSocketOptions = {
  respondToCommands?: boolean;
  statusForConnection?: (connection: number) => Record<string, unknown>;
  onCommand?: (operation: string) => void;
};

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

async function openDebugWorkspace(page: Page, options: DebugSocketOptions = {}) {
  let connectionCount = 0;
  const sockets: WebSocketRoute[] = [];
  await page.addInitScript(() => {
    window.localStorage.setItem("taskplanner.runtimeMode.llm", "debug");
  });
  await page.route("**/api/runtime/status", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      phase: "idle",
      active_mode: "debug",
      requested_mode: "debug",
      message: "Selected runtime is ready.",
      retryable: false,
    }),
  }));
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9091\/?$/, (socket) => {
    connectionCount += 1;
    const connection = connectionCount;
    sockets.push(socket);
    socket.onMessage((raw) => {
      const message = JSON.parse(typeof raw === "string" ? raw : raw.toString()) as {
        op?: string;
        id?: string;
        service?: string;
        topic?: string;
        args?: { operation?: string };
      };
      if (message.op === "subscribe" && message.topic === "/integration/debug/status") {
        const status = options.statusForConnection?.(connection) ?? debugStatus(`session-${connection}`);
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: { data: JSON.stringify(status) },
        }));
        return;
      }
      if (message.op !== "call_service" || !message.id || !message.service) return;
      options.onCommand?.(String(message.args?.operation ?? ""));
      if (options.respondToCommands === false) return;
      socket.send(JSON.stringify({
        op: "service_response",
        id: message.id,
        service: message.service,
        result: true,
        values: {
          accepted: true,
          command_id: `command-${connection}`,
          message: "accepted",
          result_json: "{}",
        },
      }));
    });
  });
  await page.goto("/");
  return {
    connectionCount: () => connectionCount,
    sockets,
  };
}

test("locks Debug writes and cancels a pending command when status becomes stale", async ({ page }) => {
  const commands: string[] = [];
  await openDebugWorkspace(page, {
    respondToCommands: false,
    onCommand: (operation) => commands.push(operation),
  });

  const manualButton = page.getByRole("button", { name: "수동 제어 활성화" });
  await expect(manualButton).toBeEnabled();
  await manualButton.click();
  await expect.poll(() => commands).toEqual(["arm"]);

  await expect(page.getByText(/디버그 상태 heartbeat가 만료되었습니다/)).toBeVisible({ timeout: 4_500 });
  await expect(manualButton).toBeDisabled();
  await expect(page.getByRole("alert")).toContainText("heartbeat가 만료");
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
