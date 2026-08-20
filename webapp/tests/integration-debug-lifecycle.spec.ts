import { expect, test, type Page, type WebSocketRoute } from "playwright/test";

type DebugSocketOptions = {
  respondToCommands?: boolean;
  refreshStatusOnCommand?: boolean;
  statusForConnection?: (connection: number) => Record<string, unknown>;
  onCommand?: (operation: string, payload: Record<string, unknown>) => void;
};

type RetractionVoiceStatusOptions = {
  mode?: "buttons_only" | "voice_and_buttons";
  internalState?: string;
  allowedCommands?: string[];
  serviceReady?: boolean;
  inFlight?: boolean;
  transcript?: string;
  command?: string | null;
  targetSide?: string;
  distanceM?: number;
  confidence?: number;
  reason?: string;
  interpreterSource?: string;
  vlmInvoked?: boolean;
  interpreterMode?: "deterministic" | "vlm_with_fallback";
  interpreterPending?: boolean;
  detail?: string;
  lastRejectionReason?: string;
};

function retractionVoiceStatus({
  mode = "buttons_only",
  internalState = "idle",
  allowedCommands = ["start_direct_teach"],
  serviceReady = false,
  inFlight = false,
  transcript = "",
  command = null,
  targetSide = "none",
  distanceM = 0,
  confidence = 0,
  reason = "empty_transcript",
  interpreterSource = "shared_deterministic",
  vlmInvoked = false,
  interpreterMode = "deterministic",
  interpreterPending = false,
  detail = "deterministic_normalizer",
  lastRejectionReason = "",
}: RetractionVoiceStatusOptions = {}): Record<string, unknown> {
  return {
    mode,
    internal_state: internalState,
    interpreter_mode: interpreterMode,
    interpreter_pending: interpreterPending,
    allowed_commands: allowedCommands,
    service_ready: serviceReady,
    in_flight: inFlight,
    last_interpretation: {
      transcript,
      command,
      target_side: targetSide,
      distance_m: distanceM,
      confidence,
      reason,
      interpreter_source: interpreterSource,
      vlm_invoked: vlmInvoked,
      detail,
    },
    last_rejection_reason: lastRejectionReason,
  };
}

function debugAsrStatus(): Record<string, unknown> {
  return {
    available: true,
    dependency_error: "",
    state: "STOPPED",
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
    voice: {
      auto_execute: false,
      last_sentence: "",
      last_parse: {},
      retraction: retractionVoiceStatus(),
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
      allowedCommands: ["start_direct_teach"],
      serviceReady: true,
    }),
  };
  return status;
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
    let status: Record<string, unknown> | null = null;
    sockets.push(socket);
    socket.onMessage((raw) => {
      const message = JSON.parse(typeof raw === "string" ? raw : raw.toString()) as {
        op?: string;
        id?: string;
        service?: string;
        topic?: string;
        args?: { operation?: string; payload_json?: string };
      };
      if (message.op === "subscribe" && message.topic === "/integration/debug/status") {
        status = options.statusForConnection?.(connection) ?? debugStatus(`session-${connection}`);
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: { data: JSON.stringify(status) },
        }));
        return;
      }
      if (message.op !== "call_service" || !message.id || !message.service) return;
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
      if (options.refreshStatusOnCommand && status) {
        socket.send(JSON.stringify({
          op: "publish",
          topic: "/integration/debug/status",
          msg: { data: JSON.stringify(status) },
        }));
      }
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

test("unlocks discovered Action and Service controls immediately after manual arm admission", async ({ page }) => {
  const commands: string[] = [];
  await openDebugWorkspace(page, {
    statusForConnection: () => manualControlsReadyStatus("manual-arm-session"),
    onCommand: (operation) => commands.push(operation),
  });

  await page.getByRole("tab", { name: /조그·수동 실행/ }).click();
  const actionButton = page.getByRole("button", { name: "도구 전달 요청" });
  const serviceButton = page.getByRole("button", { name: "직접 교시 시작" });
  await expect(actionButton).toBeDisabled();
  await expect(serviceButton).toBeDisabled();

  await page.getByRole("button", { name: "수동 제어 활성화" }).click();
  await expect.poll(() => commands).toEqual(["arm"]);
  await expect(page.getByRole("button", { name: "수동 제어 해제" })).toBeVisible();
  await expect(actionButton).toBeEnabled();
  await expect(serviceButton).toBeEnabled();
  expect(commands).toEqual(["arm"]);
});

test("uses the single retraction Service contract without legacy jog fields", async ({ page }) => {
  const commands: Array<{ operation: string; payload: Record<string, unknown> }> = [];
  await openDebugWorkspace(page, {
    refreshStatusOnCommand: true,
    statusForConnection: () => retractionServiceStatus("retraction-service-session"),
    onCommand: (operation, payload) => commands.push({ operation, payload }),
  });

  await page.getByRole("tab", { name: /조그·수동 실행/ }).click();
  await expect(page.getByRole("heading", { name: "리트랙터 명령" })).toBeVisible();
  await expect(page.getByText("/surgery/retraction/command")).toBeVisible();
  await expect(page.getByText("양측 동시")).toHaveCount(0);
  await expect(page.getByText(/방향·축·양측 조정과 arm_id·target_tool_id/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "요청 접수 결과" })).toBeVisible();
  await expect(page.getByRole("progressbar", { name: "Action 진행률" })).toHaveCount(0);
  await expect(page.getByText(/실제 물리 동작의 진행·완료·상태/)).toBeVisible();
  await expect(page.getByText("Debug 내부 상태")).toBeVisible();
  await expect(page.getByText("교시 완료")).toBeVisible();
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

test("keeps retraction voice routing as a final-transcript gate without starting ASR", async ({ page }) => {
  const commands: Array<{ operation: string; payload: Record<string, unknown> }> = [];
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = retractionServiceStatus("retraction-voice-mode-session");
      status.voice = {
        auto_execute: false,
        last_sentence: "오른쪽 5cm 더",
        last_parse: {},
        retraction: retractionVoiceStatus({
          mode: "buttons_only",
          internalState: "retraction_active",
          allowedCommands: ["adjust_retraction", "change_tool", "stop_retraction"],
          serviceReady: true,
          transcript: "오른쪽 5cm 더",
          command: "adjust_retraction",
          targetSide: "right",
          distanceM: 0.05,
          confidence: 0.96,
          reason: "normalized_adjust_retraction_explicit_adjustment_distance",
          detail: "deterministic_normalizer",
          lastRejectionReason: "voice_mode_buttons_only",
        }),
      };
      return status;
    },
    onCommand: (operation, payload) => commands.push({ operation, payload }),
  });

  await page.getByRole("tab", { name: /조그·수동 실행/ }).click();
  await expect(page.locator('[data-slot="debug-retraction-voice-mode"]')).toContainText("버튼만");
  await expect(page.locator('[data-slot="debug-retraction-voice-status"]')).toContainText("리트랙션 요청 접수");
  await expect(page.getByText("오른쪽 5cm 더")).toBeVisible();
  await expect(page.getByText("voice_mode_buttons_only")).toBeVisible();
  await expect(page.getByText("공용 결정론 정규화기 · VLM 미호출")).toBeVisible();
  await expect(page.getByText("공용 정규화기를 직접 사용했습니다.")).toBeVisible();
  await expect(page.locator('[data-slot="debug-retraction-voice-ownership"]')).toContainText("마이크 캡처는 USB 음성·로그 탭 하나만 사용합니다");
  await expect(page.getByRole("button", { name: "왼쪽 5 cm 더" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "직접 교시 시작" })).toBeDisabled();
  await expect(page.getByText(/별도 마이크나 ASR 세션을 시작·중지하지 않습니다/)).toBeVisible();

  await page.locator('[aria-label="리트랙터 음성 처리 모드"]').getByRole("button", { name: /^음성 \+ 버튼/ }).click();
  await expect.poll(() => commands).toContainEqual({
    operation: "configure_retraction_voice",
    payload: { enabled: true },
  });
  expect(commands.map(({ operation }) => operation)).not.toContain("asr_start");
  expect(commands.map(({ operation }) => operation)).not.toContain("asr_stop");

  await page.getByRole("tab", { name: /USB 음성·로그/ }).click();
  await expect(page.locator('[data-slot="debug-asr-sole-owner"]')).toContainText("이 탭이 Debug 마이크 캡처를 단독 소유합니다");
  await expect(page.getByText(/두 번째 오디오 스트림을 열지 않습니다/)).toBeVisible();
});

test("shows Text VLM pending provenance before any retraction Service request", async ({ page }) => {
  const commands: string[] = [];
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = retractionServiceStatus("retraction-vlm-pending-session");
      status.voice = {
        auto_execute: false,
        last_sentence: "리트랙션 시작해",
        last_parse: {},
        retraction: retractionVoiceStatus({
          mode: "voice_and_buttons",
          internalState: "taught_ready",
          allowedCommands: ["start_direct_teach", "start_retraction"],
          serviceReady: true,
          transcript: "리트랙션 시작해",
          command: "start_retraction",
          confidence: 0.8,
          reason: "normalized_start_retraction",
          interpreterSource: "text_vlm_pending",
          interpreterMode: "vlm_with_fallback",
          interpreterPending: true,
          detail: "text_vlm_request_submitted",
        }),
      };
      return status;
    },
    onCommand: (operation) => commands.push(operation),
  });

  await page.getByRole("tab", { name: /조그·수동 실행/ }).click();
  const status = page.locator('[data-slot="debug-retraction-voice-status"]');
  await expect(status).toContainText("Text VLM 해석 중");
  await expect(status).toContainText("Text VLM 요청 제출 · 응답 대기");
  await expect(status).toContainText("Text VLM 요청을 제출하고 비동기 응답을 기다립니다.");
  await expect(status).toContainText("text_vlm_request_submitted");
  await expect(page.getByRole("button", { name: "Retraction 시작" })).toBeDisabled();
  await expect(page.locator('[aria-label="리트랙터 음성 처리 모드"]').getByRole("button", { name: /^버튼만/ })).toBeEnabled();
  expect(commands).toEqual([]);
});

test("shows grounded deterministic fallback after a Text VLM transport attempt", async ({ page }) => {
  await openDebugWorkspace(page, {
    statusForConnection: () => {
      const status = retractionServiceStatus("retraction-vlm-fallback-session");
      status.voice = {
        auto_execute: false,
        last_sentence: "오른쪽 5cm 더",
        last_parse: {},
        retraction: retractionVoiceStatus({
          mode: "voice_and_buttons",
          internalState: "retraction_active",
          allowedCommands: ["adjust_retraction", "change_tool", "stop_retraction"],
          serviceReady: true,
          transcript: "오른쪽 5cm 더",
          command: "adjust_retraction",
          targetSide: "right",
          distanceM: 0.05,
          confidence: 0.96,
          reason: "normalized_adjust_retraction_explicit_adjustment_distance",
          interpreterSource: "deterministic_fallback",
          vlmInvoked: true,
          interpreterMode: "vlm_with_fallback",
          detail: "text_vlm_unavailable:TimeoutError",
        }),
      };
      return status;
    },
  });

  await page.getByRole("tab", { name: /조그·수동 실행/ }).click();
  const status = page.locator('[data-slot="debug-retraction-voice-status"]');
  await expect(status).toContainText("Text VLM 호출 후 공용 정규화기로 폴백");
  await expect(status).toContainText("Text VLM 연결 또는 응답 실패로 공용 정규화기를 사용했습니다. (TimeoutError)");
  await expect(status).toContainText("text_vlm_unavailable:TimeoutError");
});

test("holds retraction buttons and voice enable while a Service admission response is pending", async ({ page }) => {
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
          mode: "buttons_only",
          internalState: "retraction_active",
          allowedCommands: ["adjust_retraction", "change_tool", "stop_retraction"],
          serviceReady: true,
          inFlight: true,
        }),
      };
      return status;
    },
  });

  await page.getByRole("tab", { name: /조그·수동 실행/ }).click();
  await expect(page.locator('[data-slot="debug-retraction-voice-status"]')).toContainText("접수 응답 대기");
  await expect(page.getByRole("button", { name: "왼쪽 5 cm 더" })).toBeDisabled();
  await expect(page.locator('[aria-label="리트랙터 음성 처리 모드"]').getByRole("button", { name: /^음성 \+ 버튼/ })).toBeDisabled();
});
