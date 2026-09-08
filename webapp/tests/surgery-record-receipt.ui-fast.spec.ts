import { expect, test, type Page, type WebSocketRoute } from "playwright/test";

type ReceiptRosbridge = {
  hasSimulationStateSubscription: () => boolean;
  hasPublishedNextRun: () => boolean;
};

function receiptEnvelope() {
  const now = new Date().toISOString();
  return {
    schema: "taskplanner.surgery_record.receipt.v1",
    request_id: "record-ui-42",
    procedure_run_id: "run-ui-42",
    surgery_code: "THY-UI-42",
    record_text: "수술기록 원문\n  들여쓰기는 그대로 유지됩니다.",
    submit_state: "SUCCEEDED",
    success: true,
    http_status: 201,
    receipt_id: "receipt-ui-42",
    received_at: now,
    completed_at: now,
    server_result: "success",
    response: JSON.stringify({
      result: "success",
      data: {
        noteTitle: "갑상선 절제 수술기록지",
        date: "2026-08-31",
        roomName: "Preclinical Center",
        surgeryCode: "THY-UI-42",
        id: "server-ui-42",
        receivedAt: now,
        summary: [
          "1.수술명: 갑상선 절제술",
          "2.수술부위 및 주요 소견: ",
          "3. 수술과정:",
          "1) 절개 부위를 노출했습니다.",
          "2) 병변을 박리했습니다.",
          "4.사용기구 및 재료: 메츠바움 가위, 거즈",
          "5.출혈 및 지혈:",
        ].join("\n"),
      },
    }, null, 2),
    response_truncated: false,
    error: "",
  };
}

function simulationState(procedureRunId: string, running = false) {
  return {
    procedure_run_id: procedureRunId,
    procedure_id: "thyroidectomy_demo",
    active_bundle: "thyroidectomy_demo",
    running,
    execution_state: running ? "running" : "idle",
    filtered_phase: "phase-1",
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
  };
}

async function installReceiptRosbridge(page: Page): Promise<ReceiptRosbridge> {
  const receipt = receiptEnvelope();
  let socket: WebSocketRoute | null = null;
  let simulationStateSubscribed = false;
  let nextRunPublished = false;
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
      };
      if (message.op === "subscribe" && message.topic === "/surgery/record/receipt") {
        nextSocket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: { data: JSON.stringify(receipt) },
        }));
        return;
      }
      if (message.op === "subscribe" && message.topic === "/simulation/state") {
        simulationStateSubscribed = true;
        nextSocket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: simulationState(""),
        }));
        return;
      }
      if (message.op === "call_service" && message.id && message.service) {
        if (message.service === "/simulation/control") {
          nextRunPublished = true;
          nextSocket.send(JSON.stringify({
            op: "publish",
            topic: "/simulation/state",
            msg: simulationState("run-next", true),
          }));
        }
        nextSocket.send(JSON.stringify({
          op: "service_response",
          id: message.id,
          service: message.service,
          result: true,
          values: { success: true, message: "ok", model_ids: [] },
        }));
      }
    });
  });
  return {
    hasSimulationStateSubscription: () => simulationStateSubscribed,
    hasPublishedNextRun: () => nextRunPublished,
  };
}

test("shows the same three receipt views on 4173 without backdrop dismissal", async ({ page }) => {
  await installReceiptRosbridge(page);
  await page.goto("/");

  const dialog = page.locator('[data-slot="surgery-record-receipt-dialog"]');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "수술기록지가 생성되었습니다!" })).toBeVisible();
  await expect(dialog.getByRole("tab", { name: "기록지" })).toHaveAttribute("aria-selected", "true");
  await expect(dialog.locator(".surgery-record-receipt-structured")).toBeVisible();
  await expect(dialog.locator(".surgery-record-receipt-plain-summary")).toHaveCount(0);
  await expect(dialog).toContainText("갑상선 절제술");

  const recordTab = dialog.getByRole("tab", { name: "기록지" });
  await recordTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(dialog.getByRole("tab", { name: "전송 원문" })).toHaveAttribute("aria-selected", "true");
  await expect(dialog.getByLabel("서버로 전송한 수술기록지 원문")).toContainText("들여쓰기는 그대로 유지됩니다.");

  await page.keyboard.press("End");
  await expect(dialog.getByRole("tab", { name: "서버 원문" })).toHaveAttribute("aria-selected", "true");
  await expect(dialog.getByRole("tab", { name: "서버 원문" })).toBeFocused();
  await expect(dialog.getByLabel("서버 응답 원문")).toContainText('"server-ui-42"');

  await dialog.getByRole("button", { name: "닫기", exact: true }).focus();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "수술기록지 닫기" })).toBeFocused();

  await page.locator('[data-slot="surgery-record-receipt-backdrop"]').click({ position: { x: 3, y: 3 } });
  await expect(dialog).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
});

test("automatically closes a receipt after 30 seconds", async ({ page }) => {
  await page.clock.install({ time: Date.now() });
  await page.clock.resume();
  await installReceiptRosbridge(page);
  await page.goto("/");

  const dialog = page.locator('[data-slot="surgery-record-receipt-dialog"]');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("timer")).toHaveText(/자동으로 닫힘/);

  await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 5_000);
  await page.clock.fastForward(30_000);
  await expect(dialog).toBeHidden();
});

test("closes an older receipt when a new authoritative scenario reaches running", async ({ page }) => {
  const bridge = await installReceiptRosbridge(page);
  await page.goto("/");

  const dialog = page.locator('[data-slot="surgery-record-receipt-dialog"]');
  await expect(dialog).toBeVisible();
  await expect.poll(() => bridge.hasSimulationStateSubscription()).toBe(true);

  await page.getByRole("button", { name: "수술 시작", exact: true }).evaluate((button) => {
    const control = button as HTMLButtonElement;
    control.disabled = false;
    control.click();
  });
  await expect.poll(() => bridge.hasPublishedNextRun()).toBe(true);
  await expect(dialog).toBeHidden();
});
