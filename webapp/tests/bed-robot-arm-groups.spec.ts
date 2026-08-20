import { expect, test, type Page } from "playwright/test";

type RosbridgeMessage = {
  op?: string;
  id?: string;
  topic?: string;
  service?: string;
};

function simulationEvent(
  eventType: string,
  status: string,
  detail: Record<string, unknown>,
  instrumentId = "",
) {
  return {
    stamp: { sec: 0, nanosec: 0 },
    event_type: eventType,
    instrument_id: instrumentId,
    from_anchor: "",
    to_anchor: "",
    arm: "",
    status,
    detail: JSON.stringify(detail),
  };
}

const RETRACTION_REQUEST_ID = "req-retraction-adjust-001";

const FIXTURE_EVENTS = [
  simulationEvent("SurgeonRequestObserved", "observed", {
    requested_tool: "yankauer_suction",
    voice_text: "Yankauer suction please",
  }, "yankauer_suction"),
  simulationEvent("BedRobotArmGroupRequestObserved", "pending", {
    request_id: RETRACTION_REQUEST_ID,
    group_id: "retraction",
    arm_id: "arm_1",
    operation: "adjust_retraction",
    voice_text: "오른쪽으로 5 mm 조정해줘",
  }),
  simulationEvent("BedRobotArmGroupProposalObserved", "valid", {
    request_id: RETRACTION_REQUEST_ID,
    group_id: "retraction",
    arm_id: "arm_1",
    operation: "adjust_retraction",
    direction: "right",
    distance_mm: 5,
    valid: true,
  }),
  simulationEvent("BedRobotArmGroupCommandApproved", "approved", {
    request_id: RETRACTION_REQUEST_ID,
    command_id: "cmd-retraction-adjust-001",
    group_id: "retraction",
    arm_id: "arm_1",
    operation: "adjust_retraction",
  }),
  simulationEvent("BedRobotArmGroupStatusUpdated", "retracting", {
    request_id: RETRACTION_REQUEST_ID,
    command_id: "cmd-retraction-adjust-001",
    group_id: "retraction",
    arm_id: "arm_1",
    operation: "adjust_retraction",
    state: "retracting",
  }),
  // A stale legacy suction-arm event must never create a card or trace.
  simulationEvent("BedRobotArmGroupRequestObserved", "pending", {
    request_id: "req-legacy-suction-arm",
    group_id: "suction",
    operation: "suction_start",
    voice_text: "석션 시작",
  }),
];

const SIMULATION_STATE = {
  procedure_id: "nephrectomy",
  active_bundle: "nephrectomy",
  running: true,
  execution_state: "running",
  filtered_phase: "skin_incision",
  robot_state: "idle",
  surgeon_intent: "",
  surgeon_request_tool: "",
  surgeon_ready_for_handover: false,
  surgeon_ready_for_retrieval: false,
  cleaner_busy: false,
  cleaner_remaining_sec: 0,
  pending_transition_tools: [],
  active_recovery_tools: [],
  right_hand_tool: "",
  left_hand_tool: "",
  prepositioned_tool: "",
  active_robot_task_id: "",
  active_robot_task_type: "",
  active_robot_task_tool_id: "",
  active_robot_task_arm: "",
  active_robot_task_source_anchor: "",
  active_robot_task_target_anchor: "",
  active_robot_task_progress: 0,
  active_robot_task_remaining_sec: 0,
  // Compatibility input deliberately includes a removed suction group.
  bed_robot_arm_groups: [
    { group_id: "suction", state: "suctioning", operation: "suction_start" },
    { group_id: "retraction", state: "holding", end_effector_profile: "legacy_retractor" },
  ],
  instrument_states: [],
  recent_events: [],
  layout_json: "",
};

const BED_ROBOT_ARM_STATUS = {
  stamp: { sec: 1, nanosec: 0 },
  revision: 7,
  procedure_type: "nephrectomy",
  arms: [
    {
      arm_id: "arm_1",
      role: "retraction",
      role_instance_id: "left_malleable",
      state: "retracting",
      direct_teach_active: false,
      reason_code: "ok",
    },
    {
      arm_id: "arm_2",
      role: "retraction",
      role_instance_id: "right_malleable",
      state: "direct_teach",
      direct_teach_active: true,
      reason_code: "manual_control",
    },
  ],
};

type TimedMessage = { delayMs: number; message: unknown };

async function installRosbridgeFixture(
  page: Page,
  options: {
    simulationMessages?: TimedMessage[];
    bedRobotStatusMessages?: TimedMessage[];
  } = {},
) {
  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        phase: "idle",
        active_mode: "live",
        requested_mode: "live",
        message: "fixture runtime",
        retryable: false,
      }),
    }),
  );
  const simulationMessages = options.simulationMessages ?? [
    { delayMs: 0, message: SIMULATION_STATE },
  ];
  const bedRobotStatusMessages = options.bedRobotStatusMessages ?? [
    { delayMs: 0, message: BED_ROBOT_ARM_STATUS },
  ];
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9090\/?$/, (socket) => {
    const publishedTopics = new Set<string>();
    socket.onMessage((rawMessage) => {
      const serialized = typeof rawMessage === "string" ? rawMessage : rawMessage.toString();
      const message = JSON.parse(serialized) as RosbridgeMessage;
      if (message.op === "subscribe" && message.topic && !publishedTopics.has(message.topic)) {
        publishedTopics.add(message.topic);
        if (message.topic === "/simulation/state") {
          simulationMessages.forEach(({ delayMs, message: fixtureMessage }) => {
            setTimeout(
              () => socket.send(JSON.stringify({ op: "publish", topic: message.topic, msg: fixtureMessage })),
              delayMs,
            );
          });
        }
        if (message.topic === "/external/bed_robot_arms/status") {
          bedRobotStatusMessages.forEach(({ delayMs, message: fixtureMessage }) => {
            setTimeout(
              () => socket.send(JSON.stringify({ op: "publish", topic: message.topic, msg: fixtureMessage })),
              delayMs,
            );
          });
        }
        if (message.topic === "/simulation/event") {
          FIXTURE_EVENTS.forEach((event, index) => {
            setTimeout(() => socket.send(JSON.stringify({ op: "publish", topic: message.topic, msg: event })), 20 + index * 20);
          });
        }
      }
      if (message.op === "call_service" && message.id && message.service) {
        const values = message.service.endsWith("/list_models")
          ? { success: true, model_ids: ["mock-model"], active_model_id: "mock-model", message: "connected" }
          : { success: true, message: "ok", results: [] };
        socket.send(JSON.stringify({ op: "service_response", id: message.id, service: message.service, values, result: true }));
      }
    });
  });
}

test("shows only document-defined retraction arms while preserving clinical suction evidence", async ({ page }) => {
  await installRosbridgeFixture(page);
  await page.goto("/");

  const arms = page.locator("[data-bed-robot-arm-id]");
  await expect(arms).toHaveCount(2);
  await expect(page.locator('[data-bed-robot-arm-id="arm_1"]')).toContainText("left_malleable");
  await expect(page.locator('[data-bed-robot-arm-id="arm_1"]')).toContainText("견인 중");
  await expect(page.locator('[data-bed-robot-arm-id="arm_2"]')).toContainText("right_malleable");
  await expect(page.locator('[data-bed-robot-arm-id="arm_2"]')).toContainText("직접 교시");
  await expect(page.locator('[data-bed-robot-arm-id="arm_2"]')).toContainText("manual_control");

  await expect(page.getByText("석션 로봇암")).toHaveCount(0);
  await expect(page.getByText("suction_arm")).toHaveCount(0);
  // Landscape monitoring intentionally conceals the timeline, but the clinical
  // suction evidence must remain available in the mounted observability feed.
  await expect(page.locator(".timeline-area").getByText(/Yankauer suction/i).first()).toBeAttached();

  const traceRegion = page.getByRole("region", { name: "리트랙션 로봇암 요청 추적" });
  await expect(traceRegion.locator(`[data-bed-arm-request-id="${RETRACTION_REQUEST_ID}"]`)).toHaveCount(1);
  await expect(traceRegion.locator('[data-bed-arm-request-id="req-legacy-suction-arm"]')).toHaveCount(0);
  await expect(traceRegion.locator("[data-bed-arm-trace-step]")).toHaveCount(5);
});

test("keeps retraction arm cards and traces responsive", async ({ page }) => {
  await installRosbridgeFixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");

  const layout = await page.evaluate(() => ({
    innerWidth: window.innerWidth,
    documentWidth: document.documentElement.scrollWidth,
    cardsFit: [...document.querySelectorAll<HTMLElement>(".bed-robot-arm-card, .bed-arm-trace-card")].every(
      (card) => card.scrollWidth <= card.clientWidth,
    ),
  }));
  expect(layout.documentWidth).toBeLessThanOrEqual(layout.innerWidth);
  expect(layout.cardsFit).toBe(true);
});

test("does not invent robot arms from legacy digital-twin groups", async ({ page }) => {
  await installRosbridgeFixture(page, { bedRobotStatusMessages: [] });
  await page.goto("/");

  await expect(page.locator("[data-bed-robot-arm-id]")).toHaveCount(0);
  await expect(page.getByText("legacy_retractor")).toHaveCount(0);
});

test("ignores out-of-order controller status", async ({ page }) => {
  const currentStatus = {
    ...BED_ROBOT_ARM_STATUS,
    stamp: { sec: 2, nanosec: 0 },
    revision: 8,
  };
  const olderStatus = {
    ...BED_ROBOT_ARM_STATUS,
    stamp: { sec: 1, nanosec: 0 },
    revision: 99,
    arms: BED_ROBOT_ARM_STATUS.arms.map((arm) => ({
      ...arm,
      state: "standby",
      direct_teach_active: false,
      reason_code: "stale_payload",
    })),
  };
  await installRosbridgeFixture(page, {
    bedRobotStatusMessages: [
      { delayMs: 10, message: currentStatus },
      { delayMs: 80, message: olderStatus },
    ],
  });
  await page.goto("/");

  await expect(page.locator('[data-bed-robot-arm-id="arm_1"]')).toContainText("견인 중");
  await page.waitForTimeout(150);
  await expect(page.locator('[data-bed-robot-arm-id="arm_1"]')).toContainText("견인 중");
  await expect(page.getByText("stale_payload")).toHaveCount(0);
});

test("clears controller state after it becomes stale", async ({ page }) => {
  await installRosbridgeFixture(page);
  await page.goto("/");

  await expect(page.locator("[data-bed-robot-arm-id]")).toHaveCount(2);
  await expect(page.locator("[data-bed-robot-arm-id]")).toHaveCount(0, {
    timeout: 4500,
  });
});

test("clears controller state when the selected procedure changes", async ({ page }) => {
  await installRosbridgeFixture(page, {
    simulationMessages: [
      { delayMs: 0, message: SIMULATION_STATE },
      {
        delayMs: 700,
        message: {
          ...SIMULATION_STATE,
          procedure_id: "thyroidectomy",
          active_bundle: "thyroidectomy",
        },
      },
    ],
  });
  await page.goto("/");

  await expect(page.locator("[data-bed-robot-arm-id]")).toHaveCount(2);
  await expect(page.locator("[data-bed-robot-arm-id]")).toHaveCount(0);
});
