/**
 * Bounded browser boundary for the manually controlled rosbag2 recorder.
 *
 * The recorder owns capture and storage.  The browser only observes its
 * retained status and asks it to start or stop through a typed ROS Service.
 */
export const ROSBAG_RECORDING_STATUS_TOPIC = "/recording/rosbag/status";
export const ROSBAG_RECORDING_CONTROL_SERVICE = "/recording/rosbag/set_enabled";
export const ROSBAG_RECORDING_CONTROL_SERVICE_TYPE = "std_srvs/srv/SetBool";
export const ROSBAG_RECORDING_UI_AUDIT_TOPIC = "/recording/rosbag/ui_audit";
export const ROSBAG_RECORDING_STATUS_SCHEMA = "taskplanner.rosbag_recording.status.v1";

const MAX_STATUS_JSON_CHARS = 16 * 1024;
const MAX_SESSION_ID_CHARS = 160;
const MAX_MESSAGE_CHARS = 1_024;
const MAX_OUTPUT_DIR_CHARS = 4_096;
const MAX_TIMESTAMP_CHARS = 64;

const RECORDING_STATES = new Set([
  "idle",
  "starting",
  "recording",
  "stopping",
  "saved",
  "failed",
] as const);

const STATUS_KEYS = new Set([
  "schema",
  "state",
  "recording_active",
  "session_id",
  "message",
  "output_dir",
  "started_at",
  "stopped_at",
]);

export type RosbagRecordingState =
  | "idle"
  | "starting"
  | "recording"
  | "stopping"
  | "saved"
  | "failed";

export type RosbagRecordingStatus = {
  schema: "taskplanner.rosbag_recording.status.v1";
  state: RosbagRecordingState;
  recordingActive: boolean;
  sessionId: string;
  message: string;
  outputDir: string;
  startedAt: string;
  stoppedAt: string;
};

type RosString = { data?: unknown };

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function boundedString(
  value: unknown,
  maximum: number,
  { required = false }: { required?: boolean } = {},
): string | null {
  if (typeof value !== "string" || value.length > maximum) return null;
  if (required && !value.trim()) return null;
  return value;
}

function isoTimestampOrEmpty(value: unknown): string | null {
  const timestamp = boundedString(value, MAX_TIMESTAMP_CHARS);
  if (timestamp === null || timestamp === "") return timestamp;
  return Number.isFinite(Date.parse(timestamp)) ? timestamp : null;
}

function hasExactStatusShape(value: Record<string, unknown>): boolean {
  const keys = Object.keys(value);
  return keys.length === STATUS_KEYS.size && keys.every((key) => STATUS_KEYS.has(key));
}

/**
 * Only accept the precise retained recorder status.  It is intentionally
 * separate from the generic ROS JSON normalizer because this topic is a small,
 * stable operational contract with no forward-compatible display fields.
 */
export function normalizeRosbagRecordingStatus(message: unknown): RosbagRecordingStatus | null {
  if (!isRecord(message)) return null;
  const raw = (message as RosString).data;
  if (typeof raw !== "string") return null;
  if (!raw || raw.length > MAX_STATUS_JSON_CHARS) return null;

  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(value) || !hasExactStatusShape(value)) return null;
  if (value.schema !== ROSBAG_RECORDING_STATUS_SCHEMA) return null;
  if (typeof value.state !== "string" || !RECORDING_STATES.has(value.state as RosbagRecordingState)) {
    return null;
  }

  const sessionId = boundedString(value.session_id, MAX_SESSION_ID_CHARS);
  const detail = boundedString(value.message, MAX_MESSAGE_CHARS);
  const outputDir = boundedString(value.output_dir, MAX_OUTPUT_DIR_CHARS);
  const startedAt = isoTimestampOrEmpty(value.started_at);
  const stoppedAt = isoTimestampOrEmpty(value.stopped_at);
  if (
    typeof value.recording_active !== "boolean"
    || sessionId === null
    || detail === null
    || outputDir === null
    || startedAt === null
    || stoppedAt === null
  ) {
    return null;
  }

  return {
    schema: ROSBAG_RECORDING_STATUS_SCHEMA,
    state: value.state as RosbagRecordingState,
    recordingActive: value.recording_active,
    sessionId,
    message: detail,
    outputDir,
    startedAt,
    stoppedAt,
  };
}

export function sameRosbagRecordingStatus(
  left: RosbagRecordingStatus | null,
  right: RosbagRecordingStatus | null,
): boolean {
  if (left === right) return true;
  if (!left || !right) return false;
  return left.schema === right.schema
    && left.state === right.state
    && left.recordingActive === right.recordingActive
    && left.sessionId === right.sessionId
    && left.message === right.message
    && left.outputDir === right.outputDir
    && left.startedAt === right.startedAt
    && left.stoppedAt === right.stoppedAt;
}
