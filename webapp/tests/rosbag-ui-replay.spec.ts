import { expect, test } from "playwright/test";

import {
  createRosbagUiAuditMessage,
  normalizeRosbagUiAuditMessage,
  serializeRosbagUiAuditMessage,
} from "../src/ros/rosbagUiAuditMessages";
import {
  initialRosbagUiReplayState,
  isRosbagPresentationReplayEnabled,
  reduceRosbagUiReplayState,
} from "../src/presentation/rosbagUiReplay";

function auditMessage(overrides: Record<string, unknown> = {}) {
  return {
    data: JSON.stringify({
      schema: "taskplanner.rosbag_ui_audit.v1",
      event: "recording_ui_snapshot",
      value: "",
      runtime_mode: "live",
      bundle: "thyroidectomy_demo",
      start_phase: "exposure",
      transport_connected: true,
      captured_at: "2026-09-03T09:15:00Z",
      language: "en",
      workspace: "mission",
      stage_surgical_bed_camera: "cam2",
      stage_surgical_bed_inspecting: true,
      stage_independent_camera: "cam4",
      stage_independent_inspecting: false,
      stage_cam3_inspecting: true,
      surgery_record_visible: true,
      surgery_record_tab: "response",
      ...overrides,
    }),
  };
}

test("normalizes an exact bounded UI replay snapshot", () => {
  expect(normalizeRosbagUiAuditMessage(auditMessage())).toEqual({
    schema: "taskplanner.rosbag_ui_audit.v1",
    event: "recording_ui_snapshot",
    value: "",
    runtimeMode: "live",
    bundle: "thyroidectomy_demo",
    startPhase: "exposure",
    transportConnected: true,
    capturedAt: "2026-09-03T09:15:00Z",
    presentation: {
      language: "en",
      workspace: "mission",
      stageSurgicalBedCamera: "cam2",
      stageSurgicalBedInspecting: true,
      stageIndependentCamera: "cam4",
      stageIndependentInspecting: false,
      stageCam3Inspecting: true,
      surgeryRecordVisible: true,
      surgeryRecordTab: "response",
    },
  });
});

test("rejects unbounded and expanded replay audit data", () => {
  expect(normalizeRosbagUiAuditMessage(auditMessage({ workspace: "other" }))).toBeNull();
  expect(normalizeRosbagUiAuditMessage(auditMessage({ extra: "field" }))).toBeNull();
  expect(normalizeRosbagUiAuditMessage({ data: "x".repeat(4 * 1024 + 1) })).toBeNull();
});

test("reducer restores client-only selections without a transport side effect", () => {
  const event = normalizeRosbagUiAuditMessage(auditMessage());
  expect(event).not.toBeNull();
  const initial = initialRosbagUiReplayState({ language: "ko", workspace: "mission" });
  expect(reduceRosbagUiReplayState(initial, event!)).toEqual({
    bundle: "thyroidectomy_demo",
    startPhase: "exposure",
    presentation: {
      language: "en",
      workspace: "mission",
      stageSurgicalBedCamera: "cam2",
      stageSurgicalBedInspecting: true,
      stageIndependentCamera: "cam4",
      stageIndependentInspecting: false,
      stageCam3Inspecting: true,
      surgeryRecordVisible: true,
      surgeryRecordTab: "response",
    },
  });
});

test("serializer rejects oversized values and replay requires the explicit query flag", () => {
  const valid = createRosbagUiAuditMessage({
    event: "language_selected",
    value: "en",
    bundle: "thyroidectomy_demo",
    startPhase: "",
    transportConnected: true,
    presentation: initialRosbagUiReplayState({ language: "en", workspace: "mission" }).presentation,
    capturedAt: "2026-09-03T09:15:00Z",
  });
  expect(valid).not.toBeNull();
  expect(serializeRosbagUiAuditMessage(valid!)).toContain("language_selected");
  expect(createRosbagUiAuditMessage({
    event: "language_selected",
    value: "x".repeat(161),
    bundle: "",
    startPhase: "",
    transportConnected: true,
    presentation: initialRosbagUiReplayState({ language: "en", workspace: "mission" }).presentation,
  })).toBeNull();
  expect(isRosbagPresentationReplayEnabled("?rosbagReplay=1")).toBe(true);
  expect(isRosbagPresentationReplayEnabled("?rosbagReplay=true")).toBe(false);
});
