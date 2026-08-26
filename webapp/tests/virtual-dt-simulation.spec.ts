// This workspace ships the runner through `playwright` rather than the
// optional `@playwright/test` facade used by older specs.
import { expect, test } from "playwright/test";

import { deriveVirtualDtSimulation } from "../src/hooks/useVirtualEndpointDtSimulation";
import type { ExecutionTrace, SkillStatus } from "../src/types";

const RUN = {
  id: "virtual-run-1",
  startedAtMs: 1_000,
  endpointSourceAtStart: "virtual",
  active: true,
} as const;

function trace(overrides: Partial<ExecutionTrace> = {}): ExecutionTrace {
  return {
    sequence: 1,
    command_id: "virtual-action-1",
    route: "tool_transfer",
    transport: "action",
    endpoint: "/integration/virtual/surgery/tool_handover",
    stage: "accepted",
    dispatch_submitted: true,
    terminal: false,
    evidence: "goal_response",
    reason_code: "goal_accepted",
    retraction_command: 0,
    retraction_target_side: 0,
    retraction_distance_m: 0,
    receivedAt: 1_200,
    ...overrides,
  };
}

function toolStatus(overrides: Partial<SkillStatus> = {}): SkillStatus {
  return {
    command_id: "virtual-action-1",
    action: "tool_handover",
    instrument_id: "T02",
    instrument_instance_id: "T02#1",
    state: "accepted",
    success: true,
    message: "accepted",
    arm: "right",
    source_location_id: "main_tray_slot_2",
    source_location_type: "tray",
    target_location_id: "surgeon_receive_zone",
    target_location_type: "surgeon",
    target_owner: "surgeon",
    cleaning_required: false,
    mode: "virtual",
    progress: 0.3,
    elapsed_sec: 0,
    remaining_sec: 0,
    ...overrides,
  };
}

test("projects only a completed virtual Action with corroborating command status", () => {
  const simulation = deriveVirtualDtSimulation({
    run: RUN,
    executionTraces: [trace({
      stage: "completed",
      terminal: true,
      evidence: "controller_result",
      reason_code: "completed",
    })],
    skillStatusByCommand: {
      "virtual-action-1": toolStatus({ state: "completed", success: true, progress: 1 }),
    },
  });

  expect(simulation?.current?.state).toBe("completed");
  expect(simulation?.current?.progress).toBe(1);
  expect(simulation?.terminalProjections).toEqual([
    expect.objectContaining({
      commandId: "virtual-action-1",
      toolId: "T02",
      toolInstanceId: "T02#1",
      source: "virtual",
      kind: "tool_handover",
    }),
  ]);
});

test("never renders an external endpoint as a virtual run", () => {
  const simulation = deriveVirtualDtSimulation({
    run: RUN,
    executionTraces: [trace({ endpoint: "/surgery/tool_handover" })],
    skillStatusByCommand: { "virtual-action-1": toolStatus() },
  });

  expect(simulation?.current).toBeNull();
  expect(simulation?.terminalProjections).toEqual([]);
});

test("waits for the matching terminal SkillStatus before creating a display projection", () => {
  const simulation = deriveVirtualDtSimulation({
    run: RUN,
    executionTraces: [trace({
      stage: "completed",
      terminal: true,
      evidence: "controller_result",
      reason_code: "completed",
    })],
    skillStatusByCommand: {
      "virtual-action-1": toolStatus({ state: "accepted", success: true, progress: 0.3 }),
    },
  });

  expect(simulation?.current?.state).toBe("completed");
  expect(simulation?.current?.terminalProjection).toBeNull();
  expect(simulation?.terminalProjections).toEqual([]);
});

test("keeps virtual Service acceptance admission-only", () => {
  const simulation = deriveVirtualDtSimulation({
    run: RUN,
    executionTraces: [trace({
      command_id: "virtual-service-1",
      route: "retraction",
      transport: "service",
      endpoint: "/integration/virtual/surgery/retraction/command",
      stage: "accepted",
      terminal: true,
      evidence: "service_admission_only",
      reason_code: "request_accepted",
    })],
    skillStatusByCommand: {},
  });

  expect(simulation?.current).toEqual(expect.objectContaining({
    commandId: "virtual-service-1",
    admissionOnly: true,
    terminalProjection: null,
  }));
  expect(simulation?.terminalProjections).toEqual([]);
});

test("requires the source selected at the start boundary to be virtual", () => {
  const simulation = deriveVirtualDtSimulation({
    run: { ...RUN, endpointSourceAtStart: "external" },
    executionTraces: [trace()],
    skillStatusByCommand: { "virtual-action-1": toolStatus() },
  });

  expect(simulation).toBeNull();
});
