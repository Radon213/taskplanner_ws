/**
 * Strict, bounded wire contract for client-only presentation state captured
 * alongside a manually controlled rosbag2 recording.
 *
 * It intentionally contains no clinical text, camera pixels, or controller
 * inputs. Those data continue to use their existing ROS topics. This lane is
 * only the small amount of browser-local state needed to reconstruct the same
 * 4173 presentation while a bag is replayed.
 */
export const ROSBAG_UI_AUDIT_SCHEMA = "taskplanner.rosbag_ui_audit.v1";
export const ROSBAG_UI_AUDIT_TOPIC = "/recording/rosbag/ui_audit";

const MAX_AUDIT_JSON_CHARS = 4 * 1024;
const MAX_AUDIT_VALUE_CHARS = 160;
const MAX_AUDIT_SELECTION_CHARS = 160;
const MAX_AUDIT_TIMESTAMP_CHARS = 64;

const UI_AUDIT_EVENTS = new Set([
  "planner_control_requested",
  "bundle_selected",
  "bundle_apply_requested",
  "start_phase_selected",
  "recording_ui_snapshot",
  "language_selected",
  "workspace_selected",
  "stage_camera_selected",
  "stage_camera_inspection_changed",
  "surgery_record_opened",
  "surgery_record_tab_selected",
  "surgery_record_closed",
] as const);

const WORKSPACES = new Set(["mission", "multicam", "debug"] as const);
const LANGUAGES = new Set(["ko", "en"] as const);
const SURGICAL_BED_CAMERAS = new Set(["cam2", "flir"] as const);
const INDEPENDENT_CAMERAS = new Set(["cam1", "cam4"] as const);
const RECEIPT_TABS = new Set(["record", "source", "response"] as const);

const UI_AUDIT_KEYS = new Set([
  "schema",
  "event",
  "value",
  "runtime_mode",
  "bundle",
  "start_phase",
  "transport_connected",
  "captured_at",
  "language",
  "workspace",
  "stage_surgical_bed_camera",
  "stage_surgical_bed_inspecting",
  "stage_independent_camera",
  "stage_independent_inspecting",
  "stage_cam3_inspecting",
  "surgery_record_visible",
  "surgery_record_tab",
]);

export type RosbagUiAuditEventName =
  | "planner_control_requested"
  | "bundle_selected"
  | "bundle_apply_requested"
  | "start_phase_selected"
  | "recording_ui_snapshot"
  | "language_selected"
  | "workspace_selected"
  | "stage_camera_selected"
  | "stage_camera_inspection_changed"
  | "surgery_record_opened"
  | "surgery_record_tab_selected"
  | "surgery_record_closed";

export type RosbagUiWorkspace = "mission" | "multicam" | "debug";
export type RosbagUiLanguage = "ko" | "en";
export type RosbagSurgicalBedCamera = "cam2" | "flir";
export type RosbagIndependentCamera = "cam1" | "cam4";
export type RosbagStageCameraSlot = "surgical_bed" | "independent" | "cam3";
export type RosbagStageCameraId = RosbagSurgicalBedCamera | RosbagIndependentCamera | "cam3";
export type RosbagSurgeryRecordTab = "record" | "source" | "response";

export type RosbagUiPresentation = {
  language: RosbagUiLanguage;
  workspace: RosbagUiWorkspace;
  stageSurgicalBedCamera: RosbagSurgicalBedCamera;
  stageSurgicalBedInspecting: boolean;
  stageIndependentCamera: RosbagIndependentCamera;
  stageIndependentInspecting: boolean;
  stageCam3Inspecting: boolean;
  surgeryRecordVisible: boolean;
  surgeryRecordTab: RosbagSurgeryRecordTab;
};

export type RosbagUiAuditMessage = {
  schema: "taskplanner.rosbag_ui_audit.v1";
  event: RosbagUiAuditEventName;
  value: string;
  runtimeMode: "live";
  bundle: string;
  startPhase: string;
  transportConnected: boolean;
  capturedAt: string;
  presentation: RosbagUiPresentation;
};

export type RosbagUiAuditInput = Omit<
  RosbagUiAuditMessage,
  "schema" | "runtimeMode" | "capturedAt"
> & {
  capturedAt?: string;
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

function timestampOrEmpty(value: unknown): string | null {
  const timestamp = boundedString(value, MAX_AUDIT_TIMESTAMP_CHARS);
  if (timestamp === null || timestamp === "") return timestamp;
  return Number.isFinite(Date.parse(timestamp)) ? timestamp : null;
}

function enumValue<T extends string>(value: unknown, values: Set<T>): T | null {
  return typeof value === "string" && values.has(value as T) ? value as T : null;
}

function exactShape(value: Record<string, unknown>, keys: Set<string>): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.size && actual.every((key) => keys.has(key));
}

function normalizePresentation(value: Record<string, unknown>): RosbagUiPresentation | null {
  const language = enumValue(value.language, LANGUAGES);
  const workspace = enumValue(value.workspace, WORKSPACES);
  const stageSurgicalBedCamera = enumValue(
    value.stage_surgical_bed_camera,
    SURGICAL_BED_CAMERAS,
  );
  const stageIndependentCamera = enumValue(
    value.stage_independent_camera,
    INDEPENDENT_CAMERAS,
  );
  const surgeryRecordTab = enumValue(value.surgery_record_tab, RECEIPT_TABS);
  if (
    !language ||
    !workspace ||
    !stageSurgicalBedCamera ||
    !stageIndependentCamera ||
    !surgeryRecordTab ||
    typeof value.stage_surgical_bed_inspecting !== "boolean" ||
    typeof value.stage_independent_inspecting !== "boolean" ||
    typeof value.stage_cam3_inspecting !== "boolean" ||
    typeof value.surgery_record_visible !== "boolean"
  ) {
    return null;
  }
  return {
    language,
    workspace,
    stageSurgicalBedCamera,
    stageSurgicalBedInspecting: value.stage_surgical_bed_inspecting,
    stageIndependentCamera,
    stageIndependentInspecting: value.stage_independent_inspecting,
    stageCam3Inspecting: value.stage_cam3_inspecting,
    surgeryRecordVisible: value.surgery_record_visible,
    surgeryRecordTab,
  };
}

/** Returns a bounded wire value, or null instead of sending an unsafe audit. */
export function createRosbagUiAuditMessage(
  input: RosbagUiAuditInput,
): RosbagUiAuditMessage | null {
  const event = enumValue(input.event, UI_AUDIT_EVENTS);
  const value = boundedString(input.value, MAX_AUDIT_VALUE_CHARS);
  const bundle = boundedString(input.bundle, MAX_AUDIT_SELECTION_CHARS);
  const startPhase = boundedString(input.startPhase, MAX_AUDIT_SELECTION_CHARS);
  const capturedAt = timestampOrEmpty(input.capturedAt ?? new Date().toISOString());
  const presentation = normalizePresentation({
    language: input.presentation.language,
    workspace: input.presentation.workspace,
    stage_surgical_bed_camera: input.presentation.stageSurgicalBedCamera,
    stage_surgical_bed_inspecting: input.presentation.stageSurgicalBedInspecting,
    stage_independent_camera: input.presentation.stageIndependentCamera,
    stage_independent_inspecting: input.presentation.stageIndependentInspecting,
    stage_cam3_inspecting: input.presentation.stageCam3Inspecting,
    surgery_record_visible: input.presentation.surgeryRecordVisible,
    surgery_record_tab: input.presentation.surgeryRecordTab,
  });
  if (
    !event ||
    value === null ||
    bundle === null ||
    startPhase === null ||
    capturedAt === null ||
    !presentation ||
    typeof input.transportConnected !== "boolean"
  ) {
    return null;
  }
  return {
    schema: ROSBAG_UI_AUDIT_SCHEMA,
    event,
    value,
    runtimeMode: "live",
    bundle,
    startPhase,
    transportConnected: input.transportConnected,
    capturedAt,
    presentation,
  };
}

export function serializeRosbagUiAuditMessage(message: RosbagUiAuditMessage): string | null {
  const payload = JSON.stringify({
    schema: message.schema,
    event: message.event,
    value: message.value,
    runtime_mode: message.runtimeMode,
    bundle: message.bundle,
    start_phase: message.startPhase,
    transport_connected: message.transportConnected,
    captured_at: message.capturedAt,
    language: message.presentation.language,
    workspace: message.presentation.workspace,
    stage_surgical_bed_camera: message.presentation.stageSurgicalBedCamera,
    stage_surgical_bed_inspecting: message.presentation.stageSurgicalBedInspecting,
    stage_independent_camera: message.presentation.stageIndependentCamera,
    stage_independent_inspecting: message.presentation.stageIndependentInspecting,
    stage_cam3_inspecting: message.presentation.stageCam3Inspecting,
    surgery_record_visible: message.presentation.surgeryRecordVisible,
    surgery_record_tab: message.presentation.surgeryRecordTab,
  });
  return payload.length <= MAX_AUDIT_JSON_CHARS ? payload : null;
}

/** Accept only the exact versioned replay presentation event. */
export function normalizeRosbagUiAuditMessage(message: unknown): RosbagUiAuditMessage | null {
  if (!isRecord(message)) return null;
  const raw = (message as RosString).data;
  if (typeof raw !== "string" || !raw || raw.length > MAX_AUDIT_JSON_CHARS) return null;
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(value) || !exactShape(value, UI_AUDIT_KEYS)) return null;
  if (value.schema !== ROSBAG_UI_AUDIT_SCHEMA || value.runtime_mode !== "live") return null;
  const event = enumValue(value.event, UI_AUDIT_EVENTS);
  const auditValue = boundedString(value.value, MAX_AUDIT_VALUE_CHARS);
  const bundle = boundedString(value.bundle, MAX_AUDIT_SELECTION_CHARS);
  const startPhase = boundedString(value.start_phase, MAX_AUDIT_SELECTION_CHARS);
  const capturedAt = timestampOrEmpty(value.captured_at);
  const presentation = normalizePresentation(value);
  if (
    !event ||
    auditValue === null ||
    bundle === null ||
    startPhase === null ||
    capturedAt === null ||
    !presentation ||
    typeof value.transport_connected !== "boolean"
  ) {
    return null;
  }
  return {
    schema: ROSBAG_UI_AUDIT_SCHEMA,
    event,
    value: auditValue,
    runtimeMode: "live",
    bundle,
    startPhase,
    transportConnected: value.transport_connected,
    capturedAt,
    presentation,
  };
}
