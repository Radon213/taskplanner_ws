import { expect, test } from "playwright/test";

import {
  normalizeRosbagRecordingStatus,
  sameRosbagRecordingStatus,
} from "../src/ros/rosbagRecordingMessages";

function statusMessage(overrides: Record<string, unknown> = {}) {
  return {
    data: JSON.stringify({
      schema: "taskplanner.rosbag_recording.status.v1",
      state: "recording",
      recording_active: true,
      session_id: "20260903T091500Z--abc123",
      message: "Recording all discovered ROS topics to MCAP.",
      output_dir: "/home/arl/.local/state/taskplanner/rosbag2/20260903T091500Z--abc123",
      started_at: "2026-09-03T09:15:00Z",
      stopped_at: "",
      ...overrides,
    }),
  };
}

test("normalizes the exact retained manual recorder status", () => {
  const status = normalizeRosbagRecordingStatus(statusMessage());

  expect(status).toEqual({
    schema: "taskplanner.rosbag_recording.status.v1",
    state: "recording",
    recordingActive: true,
    sessionId: "20260903T091500Z--abc123",
    message: "Recording all discovered ROS topics to MCAP.",
    outputDir: "/home/arl/.local/state/taskplanner/rosbag2/20260903T091500Z--abc123",
    startedAt: "2026-09-03T09:15:00Z",
    stoppedAt: "",
  });
});

test("rejects malformed, expanded, and unsafe recorder statuses", () => {
  expect(normalizeRosbagRecordingStatus(statusMessage({ state: "unknown" }))).toBeNull();
  expect(normalizeRosbagRecordingStatus(statusMessage({ recording_active: "true" }))).toBeNull();
  expect(normalizeRosbagRecordingStatus(statusMessage({ started_at: "not-a-time" }))).toBeNull();
  expect(normalizeRosbagRecordingStatus(statusMessage({ unexpected: "field" }))).toBeNull();
  expect(normalizeRosbagRecordingStatus({ data: "x".repeat(16 * 1024 + 1) })).toBeNull();
});

test("deduplicates retained recorder status redelivery", () => {
  const initial = normalizeRosbagRecordingStatus(statusMessage());
  const repeated = normalizeRosbagRecordingStatus(statusMessage());
  const saved = normalizeRosbagRecordingStatus(statusMessage({
    state: "saved",
    recording_active: false,
    message: "Recording finalized.",
    stopped_at: "2026-09-03T09:16:00Z",
  }));

  expect(sameRosbagRecordingStatus(initial, repeated)).toBe(true);
  expect(sameRosbagRecordingStatus(initial, saved)).toBe(false);
});
