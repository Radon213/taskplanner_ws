import { expect, test, type Page } from "playwright/test";

type RosbridgeMessage = {
  op?: string;
  id?: string;
  topic?: string;
  service?: string;
};

function simulationEvent(eventType: string, status: string, detail: Record<string, unknown>) {
  return {
    stamp: { sec: 0, nanosec: 0 },
    event_type: eventType,
    instrument_id: "",
    from_anchor: "",
    to_anchor: "",
    arm: "",
    status,
    detail: JSON.stringify(detail),
  };
}

function groupState({
  groupId,
  state,
  operation,
  profile,
  progress,
  requestId = "",
  commandId = "",
  direction = "",
  distanceMm = 0,
  distanceOrigin = "",
  rawDistanceText = "",
  errorCode = "",
  errorMessage = "",
  rejectionReason = "",
}: {
  groupId: "suction" | "retraction";
  state: string;
  operation: string;
  profile: string;
  progress: number;
  requestId?: string;
  commandId?: string;
  direction?: string;
  distanceMm?: number;
  distanceOrigin?: string;
  rawDistanceText?: string;
  errorCode?: string;
  errorMessage?: string;
  rejectionReason?: string;
}) {
  return {
    stamp: { sec: 0, nanosec: 0 },
    group_id: groupId,
    connected: true,
    state,
    operation,
    direction,
    distance_mm: distanceMm,
    distance_origin: distanceOrigin,
    raw_distance_text: rawDistanceText,
    end_effector_profile: profile,
    active_request_id: requestId,
    active_command_id: commandId,
    progress,
    error_code: errorCode,
    error_message: errorMessage,
    rejection_reason: rejectionReason,
  };
}

const RETRACTION_REQUEST_ID = "req-retraction-limit-001";
const SUCTION_REQUEST_ID = "req-suction-active-002";

const FIXTURE_EVENTS = [
  simulationEvent("BedRobotArmGroupStatusUpdated", "standby", {
    request_id: "health-suction",
    command_id: "health-suction",
    group_id: "suction",
    operation: "",
    state: "standby",
    outcome: "available",
    terminal: false,
    success: true,
  }),
  simulationEvent("BedRobotArmGroupRequestObserved", "pending", {
    request_id: RETRACTION_REQUEST_ID,
    group_id: "retraction",
    operation: "retraction",
    voice_text: "오른쪽으로 5 cm 당겨줘",
    end_effector_profile: "army",
  }),
  simulationEvent("BedRobotArmGroupProposalObserved", "valid", {
    request_id: RETRACTION_REQUEST_ID,
    command_id: "cmd-retraction-limit-001",
    group_id: "retraction",
    operation: "retraction",
    direction: "RIGHT",
    distance_mm: 50,
    distance_origin: "explicit_with_unit",
    raw_distance_text: "5 cm",
    end_effector_profile: "army",
    rationale: "explicit distance normalized without planner clamp",
    confidence: 0.97,
    valid: true,
  }),
  simulationEvent("BedRobotArmGroupCommandApproved", "approved", {
    request_id: RETRACTION_REQUEST_ID,
    command_id: "cmd-retraction-limit-001",
    group_id: "retraction",
    operation: "retraction",
    direction: "RIGHT",
    distance_mm: 50,
    distance_origin: "explicit_with_unit",
    raw_distance_text: "5 cm",
    end_effector_profile: "army",
    confidence: 0.97,
  }),
  simulationEvent("BedRobotArmGroupCommandRejected", "fault", {
    request_id: RETRACTION_REQUEST_ID,
    command_id: "cmd-retraction-limit-001",
    group_id: "retraction",
    operation: "retraction",
    state: "fault",
    outcome: "distance_limit_exceeded",
    terminal: true,
    success: false,
    direction: "RIGHT",
    distance_mm: 50,
    distance_origin: "explicit_with_unit",
    raw_distance_text: "5 cm",
    end_effector_profile: "army",
    error_code: "distance_limit_exceeded",
    rejection_reason: "controller safety limit",
  }),
  simulationEvent("BedRobotArmGroupRequestObserved", "pending", {
    request_id: SUCTION_REQUEST_ID,
    group_id: "suction",
    operation: "suction_start",
    voice_text: "석션 시작",
    end_effector_profile: "suction",
  }),
  simulationEvent("BedRobotArmGroupCommandApproved", "approved", {
    request_id: SUCTION_REQUEST_ID,
    command_id: "cmd-suction-active-002",
    group_id: "suction",
    operation: "suction_start",
    end_effector_profile: "suction",
    confidence: 1,
  }),
  simulationEvent("BedRobotArmGroupStatusUpdated", "suctioning", {
    request_id: SUCTION_REQUEST_ID,
    command_id: "cmd-suction-active-002",
    group_id: "suction",
    operation: "suction_start",
    state: "suctioning",
    outcome: "executing",
    terminal: false,
    success: false,
    end_effector_profile: "suction",
    progress: 0.4,
  }),
];

const SIMULATION_STATE = {
  procedure_id: "thyroidectomy",
  active_bundle: "thyroidectomy",
  running: true,
  execution_state: "running",
  filtered_phase: "skin_incision",
  robot_state: "idle",
  surgeon_intent: "request_bed_robot_arm_group",
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
  bed_robot_arm_groups: [
    groupState({
      groupId: "suction",
      state: "suctioning",
      operation: "suction_start",
      profile: "suction",
      progress: 0.4,
      requestId: SUCTION_REQUEST_ID,
      commandId: "cmd-suction-active-002",
    }),
    groupState({
      groupId: "retraction",
      state: "fault",
      operation: "retraction",
      profile: "army",
      progress: 0.6,
      direction: "RIGHT",
      distanceMm: 50,
      distanceOrigin: "explicit_with_unit",
      rawDistanceText: "5 cm",
      errorCode: "distance_limit_exceeded",
      errorMessage: "Requested 50 mm exceeds the controller safety limit",
      rejectionReason: "controller safety limit",
    }),
  ],
  instrument_states: [],
  recent_events: [],
  layout_json: "",
};

const INPUT_SOURCE_STATUS = {
  flir: { state: "READY", healthy: true, age_sec: 0.1, dropped_count: 0 },
  cam4: { state: "STALE", healthy: false, age_sec: 1.8, dropped_count: 3 },
  vlm: { state: "RECOVERING", healthy: false, age_sec: 0.2, dropped_count: 1 },
  speech: { state: "READY", healthy: true, age_sec: 0.0, dropped_count: 0 },
} as const;

async function installRosbridgeFixture(page: Page) {
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9090\/?$/, (socket) => {
    const publishedTopics = new Set<string>();
    socket.onMessage((rawMessage) => {
      const serialized = typeof rawMessage === "string" ? rawMessage : rawMessage.toString();
      const message = JSON.parse(serialized) as RosbridgeMessage;

      if (message.op === "subscribe" && message.topic && !publishedTopics.has(message.topic)) {
        publishedTopics.add(message.topic);
        if (message.topic === "/simulation/state") {
          socket.send(JSON.stringify({ op: "publish", topic: message.topic, msg: SIMULATION_STATE }));
        }
        if (message.topic === "/simulation/event") {
          FIXTURE_EVENTS.forEach((event, index) => {
            setTimeout(() => {
              socket.send(JSON.stringify({ op: "publish", topic: message.topic, msg: event }));
            }, 20 + index * 20);
          });
        }
        const sourceMatch = message.topic.match(/^\/input\/(flir|cam4|vlm|speech)\/status$/);
        if (sourceMatch) {
          const sourceId = sourceMatch[1] as keyof typeof INPUT_SOURCE_STATUS;
          socket.send(
            JSON.stringify({
              op: "publish",
              topic: message.topic,
              msg: {
                stamp: { sec: 1, nanosec: 0 },
                source_id: sourceId,
                modality: sourceId === "speech" ? "speech" : "image",
                last_observation_stamp: { sec: 1, nanosec: 0 },
                received_count: 10,
                accepted_count: 9,
                rejected_count: 1,
                epoch: 2,
                error_code: "",
                detail: "fixture",
                ...INPUT_SOURCE_STATUS[sourceId],
              },
            }),
          );
        }
      }

      if (message.op === "call_service" && message.id && message.service) {
        const values = message.service.endsWith("/list_models")
          ? { success: true, model_ids: ["mock-model"], active_model_id: "mock-model", message: "connected" }
          : { success: true, message: "ok", results: [] };
        socket.send(
          JSON.stringify({
            op: "service_response",
            id: message.id,
            service: message.service,
            values,
            result: true,
          }),
        );
      }
    });
  });
}

test.beforeEach(async ({ page }) => {
  await installRosbridgeFixture(page);
});

test("renders two logical groups and links request, VLM, BT, Action, and status by request ID", async ({ page }) => {
  await page.goto("/");

  const suctionCard = page.locator('[data-bed-robot-group-id="suction"]');
  const retractionCard = page.locator('[data-bed-robot-group-id="retraction"]');
  await expect(page.locator("[data-bed-robot-group-id]")).toHaveCount(2);
  await expect(suctionCard).toContainText("석션 로봇암");
  await expect(suctionCard).toContainText("석션중");
  await expect(retractionCard).toContainText("리트랙션 로봇암");
  await expect(retractionCard).toContainText("50mm");
  await expect(retractionCard.locator(".bed-robot-group-error")).toContainText("50 mm");
  await expect(retractionCard.locator(".bed-robot-group-error")).toHaveAttribute("role", "status");
  await expect(page.getByRole("progressbar", { name: /석션 로봇암 진행률 40%/ })).toBeVisible();
  await expect(page.getByRole("progressbar", { name: /리트랙션 로봇암 진행률 60%/ })).toBeVisible();

  const groupRailText = await page.locator(".bed-robot-group-rail").innerText();
  expect(groupRailText).not.toMatch(/베드 로봇암\s*[12]/);

  const traceRegion = page.getByRole("region", { name: "베드 로봇암 요청 추적" });
  const suctionTrace = traceRegion.locator(`[data-bed-group-request-id="${SUCTION_REQUEST_ID}"]`);
  const retractionTrace = traceRegion.locator(`[data-bed-group-request-id="${RETRACTION_REQUEST_ID}"]`);
  await expect(traceRegion).toContainText("같은 요청 ID");
  await expect(traceRegion.locator('[data-bed-group-request-id="health-suction"]')).toHaveCount(0);
  await expect(suctionTrace).toContainText(SUCTION_REQUEST_ID);
  await expect(suctionTrace.locator("[data-bed-group-trace-step]")).toHaveCount(5);
  await expect(suctionTrace.locator('[data-bed-group-trace-step="vlm"]')).toHaveAttribute(
    "aria-label",
    /우회.*VLM 없이 결정적 라우팅/,
  );
  await retractionTrace.scrollIntoViewIfNeeded();
  await expect(retractionTrace).toContainText(RETRACTION_REQUEST_ID);
  await expect(retractionTrace).toContainText("리트랙션 · 우 · 50 mm");
  await expect(retractionTrace.locator('[data-bed-group-trace-step="status"]')).toHaveAttribute(
    "aria-label",
    /거부.*distance_limit_exceeded/,
  );
  await expect(retractionTrace).toHaveClass(/tone-danger/);

  await expect(
    page.getByLabel("집도의 evidence").locator(".holder-embedded-bubble"),
  ).toContainText("석션 시작");
  const explicitDistanceEvent = page
    .locator(`.timeline-item[data-bed-group-request-id="${RETRACTION_REQUEST_ID}"]`)
    .filter({ hasText: "거리: 50 mm" })
    .first();
  await expect(explicitDistanceEvent).toContainText("거리 근거: 명시 단위");
  await expect(explicitDistanceEvent).toContainText(`요청 ID: ${RETRACTION_REQUEST_ID}`);
  await expect(page.locator('[data-timeline-filter="error"]')).toContainText("1");

  const btTab = page.getByRole("tab", { name: "BT" });
  const vlmTab = page.getByRole("tab", { name: "VLM" });
  await btTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(vlmTab).toHaveAttribute("aria-selected", "true");
  await expect(vlmTab).toBeFocused();
  await expect(page.getByRole("tabpanel", { name: "VLM" })).toBeAttached();
  const sourceHealth = page.getByRole("region", { name: "입력원 상태" });
  await expect(sourceHealth).toContainText("FLIR");
  await expect(sourceHealth).toContainText("CAM4");
  await expect(sourceHealth.locator(".state-stale")).toContainText("지연");
  await expect(sourceHealth.locator(".state-recovering")).toContainText("복구 중");
  await expect(sourceHealth).toContainText("누락 3");
});

test("keeps group cards and request traces responsive with accessible touch targets", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");

  const traceRegion = page.getByRole("region", { name: "베드 로봇암 요청 추적" });
  await expect(traceRegion.locator("[data-slot='bed-robot-arm-group-trace']")).toHaveCount(2);

  const layout = await page.evaluate(() => ({
    innerWidth: window.innerWidth,
    documentWidth: document.documentElement.scrollWidth,
    traceCardsFit: [...document.querySelectorAll<HTMLElement>(".bed-group-trace-card")].every(
      (card) => card.scrollWidth <= card.clientWidth,
    ),
    traceRowsFit: [...document.querySelectorAll<HTMLElement>(".bed-group-trace-card li")].every(
      (row) => row.scrollWidth <= row.clientWidth,
    ),
    tabTouchTargets: [...document.querySelectorAll<HTMLElement>(".tab-switch button")].map(
      (button) => button.getBoundingClientRect().height,
    ),
    filterTouchTargets: [...document.querySelectorAll<HTMLElement>(".timeline-filter button")].map(
      (button) => button.getBoundingClientRect().height,
    ),
  }));

  expect(layout.documentWidth).toBeLessThanOrEqual(layout.innerWidth);
  expect(layout.traceCardsFit).toBe(true);
  expect(layout.traceRowsFit).toBe(true);
  expect(layout.tabTouchTargets.every((height) => height >= 44)).toBe(true);
  expect(layout.filterTouchTargets.every((height) => height >= 44)).toBe(true);

  const traceSteps = traceRegion.locator("[data-bed-group-trace-step]");
  await expect(traceSteps).toHaveCount(10);
  expect(
    await traceSteps.evaluateAll((steps) =>
      steps.every((step) => Boolean(step.getAttribute("aria-label")) && Boolean(step.getAttribute("title"))),
    ),
  ).toBe(true);
});
