import { expect, test } from "playwright/test";

import {
  appendOperationTrace,
  deriveOperationPresentation,
  emptyOperationTraceFeed,
  resetOperationTraceFeed,
} from "../src/presentation/operationPresentation";
import type { ExecutionTrace, LiveAsrFinal, SkillStatus } from "../src/types";

function trace(overrides: Partial<ExecutionTrace>): ExecutionTrace {
  return {
    sequence: 1,
    command_id: "",
    route: "",
    transport: "action",
    endpoint: "/integration/virtual/surgery/tool_handover",
    endpoint_source: "virtual",
    stage: "accepted",
    dispatch_submitted: true,
    terminal: false,
    evidence: "goal_response",
    reason_code: "",
    retraction_command: 0,
    retraction_target_side: 0,
    retraction_distance_m: 0,
    receivedAt: 1,
    ...overrides,
  };
}

function status(overrides: Partial<SkillStatus>): SkillStatus {
  return {
    command_id: "handover-bipolar-1",
    action: "tool_handover",
    instrument_id: "T07",
    instrument_instance_id: "T07#1",
    state: "accepted",
    success: false,
    message: "accepted",
    arm: "right",
    source_location_id: "T07",
    source_location_type: "rack",
    target_location_id: "surgeon",
    target_location_type: "surgeon",
    target_owner: "surgeon",
    cleaning_required: false,
    mode: "virtual",
    progress: 0.1,
    elapsed_sec: 0.1,
    remaining_sec: 0.5,
    ...overrides,
  };
}

test("projects correlated Action, Service, and ASR facts once for the UI", () => {
  const commandId = "handover-bipolar-1";
  const finals: readonly LiveAsrFinal[] = [{
    stamp: "2026-08-28T03:00:00.000Z",
    text: "석션 시작",
    response_latency_ms: 42,
    latency_basis: "source",
    latency_correlated: true,
  }];
  const presentation = deriveOperationPresentation({
    executionTraces: [
      trace({ command_id: commandId, receivedAt: 100 }),
      trace({
        sequence: 2,
        command_id: "suction-1",
        route: "retraction",
        transport: "service",
        endpoint: "/integration/virtual/surgery/retraction/command",
        stage: "completed",
        terminal: true,
        evidence: "virtual_service_transaction_completed",
        reason_code: "virtual_service_completed",
        retraction_command: 7,
        retraction_target_side: 0,
        retraction_distance_m: 0,
        receivedAt: 200,
      }),
    ],
    skillStatusByCommand: { [commandId]: status({ command_id: commandId }) },
    asrFinals: finals,
    language: "ko",
    displayToolName: (toolId) => toolId === "T07" ? "바이폴라 전기소작기" : toolId,
  });

  const handover = presentation.dispatches[0];
  const suction = presentation.latestDispatch;

  expect(handover?.subject).toBe("대상 도구 · 바이폴라 전기소작기 #1");
  expect(handover?.toolFlow).toEqual({
    from: { label: "출발지", value: "도구 랙 · T07" },
    to: { label: "목적지", value: "집도의" },
  });
  expect(suction?.title).toBe("서비스 · 완료 확인");
  expect(suction?.detail).toBe("가상 Service 처리 완료");
  expect(suction?.retraction?.facts).toEqual([
    { label: "명령", value: "7 · 석션 준비" },
    { label: "target_side", value: "0" },
    { label: "거리", value: "0 cm" },
  ]);
  expect(presentation.asrFinal).toEqual({
    eventKey: "asr:2026-08-28T03:00:00.000Z:석션 시작",
    text: "석션 시작",
    heading: "ASR 확정 문장 · 관찰",
    ariaLabel: "집도의 ASR 확정 문장",
  });
});

test("shows suction removal and signed retraction adjustment semantics", () => {
  const presentation = deriveOperationPresentation({
    executionTraces: [
      trace({
        sequence: 1,
        command_id: "less-pull-1",
        route: "retraction",
        transport: "service",
        retraction_command: 4,
        retraction_target_side: 2,
        retraction_distance_m: -0.01,
      }),
      trace({
        sequence: 2,
        command_id: "suction-remove-1",
        route: "retraction",
        transport: "service",
        retraction_command: 8,
        retraction_target_side: 0,
        retraction_distance_m: 0,
      }),
    ],
    skillStatusByCommand: {},
    asrFinals: [],
    language: "ko",
    displayToolName: (toolId) => toolId,
  });

  expect(presentation.dispatches[0]?.retraction).toEqual({
    command: 4,
    targetSide: 2,
    distanceCm: -1,
    facts: [
      { label: "명령", value: "4 · 리트랙션 조정" },
      { label: "target_side", value: "2" },
      { label: "거리", value: "1 cm 덜 당기기" },
    ],
  });
  expect(presentation.dispatches[1]?.retraction?.facts).toEqual([
    { label: "명령", value: "8 · 석션 제거" },
    { label: "target_side", value: "0" },
    { label: "거리", value: "0 cm" },
  ]);
});

test("clears browser-local action traces across reconnect and bridge restart", () => {
  const first = trace({ command_id: "old-action", sequence: 1, receivedAt: 10 });
  const second = trace({ command_id: "old-action", sequence: 2, receivedAt: 20 });
  const newOwnerFirst = trace({ command_id: "new-action", sequence: 1, receivedAt: 30 });

  const original = appendOperationTrace(
    appendOperationTrace(emptyOperationTraceFeed(), first),
    second,
  );
  expect(original.traces.map((entry) => entry.command_id)).toEqual(["old-action", "old-action"]);

  const restarted = appendOperationTrace(original, newOwnerFirst);
  expect(restarted.epoch).toBe(original.epoch + 1);
  expect(restarted.traces).toEqual([newOwnerFirst]);

  const disconnected = resetOperationTraceFeed(restarted);
  expect(disconnected.epoch).toBe(restarted.epoch + 1);
  expect(disconnected.traces).toEqual([]);
});
