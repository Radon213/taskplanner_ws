import { expect, test, type Page, type WebSocketRoute } from "playwright/test";

type RosbagBridge = {
  serviceCalls: Array<Record<string, unknown>>;
  scenarioControlCalls: () => number;
  auditEvents: Array<Record<string, unknown>>;
};

function recorderStatus(overrides: Record<string, unknown> = {}) {
  return {
    schema: "taskplanner.rosbag_recording.status.v1",
    state: "idle",
    recording_active: false,
    session_id: "",
    message: "Manual recorder is ready.",
    output_dir: "",
    started_at: "",
    stopped_at: "",
    ...overrides,
  };
}

function simulationState() {
  return {
    procedure_run_id: "",
    procedure_id: "thyroidectomy_demo",
    active_bundle: "thyroidectomy_demo",
    running: false,
    execution_state: "idle",
    filtered_phase: "",
    instrument_states: [],
  };
}

async function installRosbagBridge(page: Page): Promise<RosbagBridge> {
  const serviceCalls: Array<Record<string, unknown>> = [];
  const auditEvents: Array<Record<string, unknown>> = [];
  let simulationControlCalls = 0;
  let socket: WebSocketRoute | null = null;

  await page.route("**/api/runtime/status", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      phase: "idle",
      active_mode: "live",
      requested_mode: "live",
      retryable: false,
    }),
  }));
  await page.route("**/api/runtime/transition", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      phase: "idle",
      active_mode: "live",
      requested_mode: "live",
      retryable: false,
    }),
  }));
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9090\/?$/, (nextSocket) => {
    socket = nextSocket;
    nextSocket.onMessage((raw) => {
      const message = JSON.parse(typeof raw === "string" ? raw : raw.toString()) as {
        id?: string;
        op?: string;
        service?: string;
        topic?: string;
        args?: Record<string, unknown>;
        msg?: { data?: string };
      };
      if (message.op === "subscribe" && message.topic === "/simulation/state") {
        nextSocket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: simulationState(),
        }));
        return;
      }
      if (message.op === "subscribe" && message.topic === "/recording/rosbag/status") {
        nextSocket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: { data: JSON.stringify(recorderStatus()) },
        }));
        return;
      }
      if (message.op === "publish" && message.topic === "/recording/rosbag/ui_audit") {
        try {
          const payload = message.msg?.data;
          if (payload) auditEvents.push(JSON.parse(payload) as Record<string, unknown>);
        } catch {
          // The test checks the valid snapshot emitted by the browser.
        }
        return;
      }
      if (message.op !== "call_service" || !message.id || !message.service) return;
      if (message.service === "/simulation/control") simulationControlCalls += 1;
      if (message.service === "/recording/rosbag/set_enabled") {
        serviceCalls.push(message.args ?? {});
        nextSocket.send(JSON.stringify({
          op: "service_response",
          id: message.id,
          service: message.service,
          result: true,
          values: { success: true, message: "Manual recording started." },
        }));
        nextSocket.send(JSON.stringify({
          op: "publish",
          topic: "/recording/rosbag/status",
          msg: {
            data: JSON.stringify(recorderStatus({
              state: "recording",
              recording_active: true,
              session_id: "recording-ui-42",
              message: "Recording all discovered ROS topics.",
              output_dir: "/home/arl/.local/state/taskplanner/rosbag2/recording-ui-42",
              started_at: "2026-09-03T09:15:00Z",
            })),
          },
        }));
        return;
      }
      nextSocket.send(JSON.stringify({
        op: "service_response",
        id: message.id,
        service: message.service,
        result: true,
        values: { success: true, message: "ok", model_ids: [] },
      }));
    });
  });

  return {
    serviceCalls,
    scenarioControlCalls: () => simulationControlCalls,
    auditEvents,
  };
}

test("manually starts recording without a scenario lifecycle request", async ({ page }) => {
  const bridge = await installRosbagBridge(page);
  await page.goto("/");

  const control = page.locator('[data-slot="rosbag-recording-control"]');
  await expect(control).toBeVisible();
  await expect(control.getByRole("button", { name: "녹화 시작" })).toBeEnabled();
  await control.getByRole("button", { name: "녹화 시작" }).click();

  await expect.poll(() => bridge.serviceCalls).toEqual([{ data: true }]);
  await expect(control.getByRole("button", { name: "녹화 종료" })).toHaveAttribute("aria-pressed", "true");
  await expect(control).toContainText("/home/arl/.local/state/taskplanner/rosbag2/recording-ui-42");
  await expect.poll(() => bridge.scenarioControlCalls()).toBe(0);
  await expect.poll(() => bridge.auditEvents.some((event) => event.event === "recording_ui_snapshot")).toBe(true);
  expect(socket).not.toBeNull();
});
