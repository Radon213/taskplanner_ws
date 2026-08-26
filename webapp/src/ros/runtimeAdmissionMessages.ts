import {
  isBoundedRosPayload,
  MAX_ROS_JSON_PAYLOAD_CHARS,
} from "./rosMessageBounds";

export type IntegrationReadinessChecklistStatus = "pass" | "fail" | "pending";

/**
 * A bounded, read-only explanation of one integration admission check. This
 * never supersedes the canonical boolean checks map that owns the gate.
 */
export type IntegrationReadinessChecklistItem = {
  id: string;
  required: boolean;
  status: IntegrationReadinessChecklistStatus;
  reason: string;
  detail: string;
};

export type IntegrationReadiness = {
  schema: "taskplanner.integration_readiness.v1";
  ready: boolean;
  checks: Record<string, boolean>;
  checklist: readonly IntegrationReadinessChecklistItem[];
  missing: string[];
  activeBundle: string;
  procedureType: string;
  /**
   * Older readiness publishers omit the independently selected retraction
   * source. Those messages deliberately inherit the Action endpoint.
   */
  retractionEndpointSource: string;
  robotEndpointSource: string;
  retractionStateMachineSuppressed: boolean;
  stampSec: number;
};

export type IntegrationReadinessBlockReason =
  | "missing"
  | "stale"
  | "bundle_mismatch"
  | "not_ready";

export type ExecutionEndpointSource = "external" | "virtual";

export type ExecutionRouteInitializationState =
  | "launch_default"
  | "initializing"
  | "initialized"
  | "running"
  | "stopped"
  | "reset";

/** Graph discovery only; a fresh preflight remains the start authority. */
export type ExecutionRouteSourceEndpointReadiness = {
  actionServerReady: boolean;
  retractionServiceReady: boolean;
};

/** Read-only, bounded route selection projection from the execution bridge. */
export type ExecutionRouteState = {
  schema: "taskplanner.execution_route_state.v1";
  stampSec: number;
  revision: number;
  initializationRevision: number;
  selectedSource: ExecutionEndpointSource;
  runEndpointSource: ExecutionEndpointSource | null;
  retractionSource: ExecutionEndpointSource;
  runRetractionSource: ExecutionEndpointSource | null;
  initializationState: ExecutionRouteInitializationState;
  actionServerReady: boolean;
  retractionServiceReady: boolean;
  sourceEndpointReadiness: Readonly<
    Partial<Record<ExecutionEndpointSource, ExecutionRouteSourceEndpointReadiness>>
  >;
  routeControlEnabled: boolean;
  activeRequestCount: number;
};

export type ExecutionRouteCommandResult = {
  state: ExecutionRouteState;
  /** The manager's stopped-state DT reset completed before the bridge switch. */
  digitalTwinReset: boolean;
};

type RosString = {
  data?: string;
};

const MAX_INTEGRATION_READINESS_CHECKS = 64;
const MAX_INTEGRATION_READINESS_MISSING = 64;
const MAX_INTEGRATION_READINESS_CHECKLIST_ITEMS = 64;
const MAX_INTEGRATION_READINESS_CHECK_ID_CHARS = 256;
const MAX_INTEGRATION_READINESS_CHECK_REASON_CHARS = 512;
const INTEGRATION_READINESS_CHECK_STATUSES = new Set<IntegrationReadinessChecklistStatus>([
  "pass",
  "fail",
  "pending",
]);

// The preflight publisher emits once per second. Do not let a retained or
// stopped readiness heartbeat keep the Live start gate open.
export const INTEGRATION_READINESS_MAX_AGE_MS = 4_000;
const INTEGRATION_READINESS_MAX_SOURCE_AGE_MS = 6_000;
const INTEGRATION_READINESS_MAX_FUTURE_SKEW_MS = 1_000;

const EXECUTION_ROUTE_INITIALIZATION_STATES = new Set<
  ExecutionRouteInitializationState
>(["launch_default", "initializing", "initialized", "running", "stopped", "reset"]);

function boundedIntegrationChecklistText(value: unknown, maxLength: number): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  if (!normalized || normalized.length > maxLength) return null;
  return normalized;
}

/**
 * The checklist is presentation-only. Require it to agree with the canonical
 * checks map so it cannot manufacture a pass or hide a failed requirement.
 */
function normalizeIntegrationReadinessChecklist(
  value: unknown,
  checks: Record<string, boolean>,
): readonly IntegrationReadinessChecklistItem[] | null {
  if (value === undefined) {
    return Object.entries(checks)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([id, passed]) => ({
        id,
        required: true,
        status: passed ? "pass" : "fail",
        reason: passed ? "passed" : "failed",
        detail: "",
      }));
  }
  if (
    !Array.isArray(value)
    || !value.length
    || value.length > MAX_INTEGRATION_READINESS_CHECKLIST_ITEMS
  ) {
    return null;
  }
  const ids = new Set<string>();
  const checklist: IntegrationReadinessChecklistItem[] = [];
  for (const row of value) {
    if (!row || typeof row !== "object" || Array.isArray(row)) return null;
    const candidate = row as Record<string, unknown>;
    const id = boundedIntegrationChecklistText(
      candidate.id,
      MAX_INTEGRATION_READINESS_CHECK_ID_CHARS,
    );
    const status = boundedIntegrationChecklistText(
      candidate.status,
      16,
    ) as IntegrationReadinessChecklistStatus | null;
    const reason = boundedIntegrationChecklistText(
      candidate.reason,
      MAX_INTEGRATION_READINESS_CHECK_REASON_CHARS,
    );
    const detailValue = candidate.detail;
    const detail = detailValue === undefined || detailValue === ""
      ? ""
      : boundedIntegrationChecklistText(
        detailValue,
        MAX_INTEGRATION_READINESS_CHECK_REASON_CHARS,
      );
    if (
      !id
      || ids.has(id)
      || typeof candidate.required !== "boolean"
      || !status
      || !INTEGRATION_READINESS_CHECK_STATUSES.has(status)
      || !reason
      || detail === null
      || !(id in checks)
      || (status === "pass" && checks[id] !== true)
      || (status !== "pass" && checks[id] !== false)
    ) {
      return null;
    }
    ids.add(id);
    checklist.push({
      id,
      required: candidate.required,
      status,
      reason,
      detail,
    });
  }
  return checklist;
}

export function normalizeIntegrationReadiness(
  message: unknown,
): IntegrationReadiness | null {
  const raw = String((message as RosString | null)?.data ?? "");
  if (!raw || raw.length > MAX_ROS_JSON_PAYLOAD_CHARS) return null;
  try {
    const payload = JSON.parse(raw) as Record<string, unknown>;
    if (!isBoundedRosPayload(payload)) return null;
    if (payload.schema !== "taskplanner.integration_readiness.v1") return null;
    if (typeof payload.ready !== "boolean") return null;
    const rawChecks = payload.checks;
    if (!rawChecks || typeof rawChecks !== "object" || Array.isArray(rawChecks)) {
      return null;
    }
    const checkEntries = Object.entries(rawChecks as Record<string, unknown>);
    if (
      checkEntries.length === 0
      || checkEntries.length > MAX_INTEGRATION_READINESS_CHECKS
      || checkEntries.some(([name, passed]) =>
        !name || name.length > 256 || typeof passed !== "boolean")
    ) {
      return null;
    }
    const checks = Object.fromEntries(checkEntries) as Record<string, boolean>;
    const checklist = normalizeIntegrationReadinessChecklist(payload.checklist, checks);
    if (checklist === null) return null;
    const missing = Array.isArray(payload.missing)
      ? payload.missing
        .slice(0, MAX_INTEGRATION_READINESS_MISSING)
        .map((value) => String(value).trim())
        .filter((value) => value.length > 0 && value.length <= 256)
      : [];
    if (missing.length > MAX_INTEGRATION_READINESS_MISSING) return null;
    const details = payload.details && typeof payload.details === "object"
      ? payload.details as Record<string, unknown>
      : {};
    const allChecksPass = checkEntries.every(([, passed]) => passed === true);
    const failedCheckNames = checkEntries
      .filter(([, passed]) => passed === false)
      .map(([name]) => name)
      .sort();
    const normalizedMissing = [...new Set(missing)].sort();
    if (
      payload.ready !== allChecksPass
      || normalizedMissing.join("\u0000") !== failedCheckNames.join("\u0000")
    ) {
      return null;
    }
    const stampSec = Number(payload.stamp_sec);
    if (!Number.isFinite(stampSec) || stampSec <= 0) return null;
    const activeBundle = String(details.active_bundle ?? "").trim();
    if (!activeBundle || activeBundle.length > 256) return null;
    const robotEndpointSource = String(details.robot_endpoint_source ?? "")
      .trim()
      .toLowerCase();
    if (robotEndpointSource !== "external" && robotEndpointSource !== "virtual") {
      return null;
    }
    const rawRetractionEndpointSource = details.retraction_endpoint_source;
    const retractionEndpointSource = rawRetractionEndpointSource === undefined
      ? robotEndpointSource
      : String(rawRetractionEndpointSource).trim().toLowerCase();
    if (
      retractionEndpointSource !== "external"
      && retractionEndpointSource !== "virtual"
    ) {
      return null;
    }
    if (typeof details.retraction_state_machine_suppressed !== "boolean") {
      return null;
    }
    const retractionStateMachineSuppressed =
      details.retraction_state_machine_suppressed;
    // Suppression is the server's authoritative projection of authored
    // scenario policy. Do not duplicate that policy with bundle-name or
    // external-route allowlists in the browser. The only route-level invariant
    // the public contract exposes is that a virtual retraction endpoint cannot
    // claim that a physical workflow state machine is enforced.
    if (retractionEndpointSource === "virtual" && !retractionStateMachineSuppressed) {
      return null;
    }
    return {
      schema: "taskplanner.integration_readiness.v1",
      ready: payload.ready,
      checks,
      checklist,
      missing: normalizedMissing,
      activeBundle,
      procedureType: String(details.procedure_type ?? "").trim().slice(0, 256),
      retractionEndpointSource,
      robotEndpointSource,
      retractionStateMachineSuppressed,
      stampSec,
    };
  } catch {
    return null;
  }
}

export function normalizeExecutionEndpointSource(
  value: unknown,
): ExecutionEndpointSource | null {
  const source = String(value ?? "").trim().toLowerCase();
  return source === "external" || source === "virtual" ? source : null;
}

function nonNegativeSafeInteger(value: unknown): number | null {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : null;
}

function normalizeExecutionRouteSourceEndpointReadiness(
  value: unknown,
): Readonly<
  Partial<Record<ExecutionEndpointSource, ExecutionRouteSourceEndpointReadiness>>
> | null {
  if (value === undefined || value === null) return {};
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const result: Partial<
    Record<ExecutionEndpointSource, ExecutionRouteSourceEndpointReadiness>
  > = {};
  for (const source of ["external", "virtual"] as const) {
    const row = (value as Record<string, unknown>)[source];
    if (row === undefined || row === null) continue;
    if (!row || typeof row !== "object" || Array.isArray(row)) return null;
    const payload = row as Record<string, unknown>;
    if (
      typeof payload.action_server_ready !== "boolean"
      || typeof payload.retraction_service_ready !== "boolean"
    ) {
      return null;
    }
    result[source] = {
      actionServerReady: payload.action_server_ready,
      retractionServiceReady: payload.retraction_service_ready,
    };
  }
  return result;
}

/**
 * Parse route status for presentation only. It never replaces the fresh
 * integration-readiness heartbeat required to admit a start.
 */
export function normalizeExecutionRouteState(
  message: unknown,
): ExecutionRouteState | null {
  const raw = String((message as RosString | null)?.data ?? "");
  if (!raw || raw.length > MAX_ROS_JSON_PAYLOAD_CHARS) return null;
  try {
    const payload = JSON.parse(raw) as Record<string, unknown>;
    if (!isBoundedRosPayload(payload)) return null;
    if (payload.schema !== "taskplanner.execution_route_state.v1") return null;
    const stampSec = Number(payload.stamp_sec);
    const revision = nonNegativeSafeInteger(payload.revision);
    const initializationRevision = nonNegativeSafeInteger(
      payload.initialization_revision,
    );
    const selectedSource = normalizeExecutionEndpointSource(payload.selected_source);
    const rawRunSource = String(payload.run_endpoint_source ?? "").trim();
    const runEndpointSource = rawRunSource
      ? normalizeExecutionEndpointSource(rawRunSource)
      : null;
    const retractionSource = normalizeExecutionEndpointSource(
      payload.retraction_source ?? selectedSource,
    );
    const rawRunRetractionSource = String(
      payload.run_retraction_source ?? "",
    ).trim();
    const runRetractionSource = rawRunRetractionSource
      ? normalizeExecutionEndpointSource(rawRunRetractionSource)
      : null;
    const initializationState = String(
      payload.initialization_state ?? "",
    ).trim() as ExecutionRouteInitializationState;
    const activeRequestCount = nonNegativeSafeInteger(payload.active_request_count);
    const sourceEndpointReadiness =
      normalizeExecutionRouteSourceEndpointReadiness(payload.source_readiness);
    if (
      !Number.isFinite(stampSec)
      || stampSec <= 0
      || revision === null
      || initializationRevision === null
      || !selectedSource
      || !retractionSource
      || (rawRunSource && !runEndpointSource)
      || (runEndpointSource && runEndpointSource !== selectedSource)
      || (rawRunRetractionSource && !runRetractionSource)
      || (runRetractionSource && runRetractionSource !== retractionSource)
      || !EXECUTION_ROUTE_INITIALIZATION_STATES.has(initializationState)
      || typeof payload.action_server_ready !== "boolean"
      || typeof payload.retraction_service_ready !== "boolean"
      || typeof payload.route_control_enabled !== "boolean"
      || !sourceEndpointReadiness
      || activeRequestCount === null
      || activeRequestCount > 4096
      || typeof payload.require_bed_robot_status !== "boolean"
      || typeof payload.require_physical_stop_confirmation !== "boolean"
      || typeof payload.retraction_state_machine_suppressed !== "boolean"
    ) {
      return null;
    }
    const requireBedRobotStatus = payload.require_bed_robot_status;
    const requirePhysicalStopConfirmation =
      payload.require_physical_stop_confirmation;
    const retractionStateMachineSuppressed =
      payload.retraction_state_machine_suppressed;
    if (
      retractionSource === "virtual"
      && (
        requireBedRobotStatus
        || requirePhysicalStopConfirmation
        || !retractionStateMachineSuppressed
      )
    ) {
      return null;
    }
    return {
      schema: "taskplanner.execution_route_state.v1",
      stampSec,
      revision,
      initializationRevision,
      selectedSource,
      runEndpointSource,
      retractionSource,
      runRetractionSource,
      initializationState,
      actionServerReady: payload.action_server_ready,
      retractionServiceReady: payload.retraction_service_ready,
      sourceEndpointReadiness,
      routeControlEnabled: payload.route_control_enabled,
      activeRequestCount,
    };
  } catch {
    return null;
  }
}

/**
 * Decode a route-command receipt. A route snapshot alone is not successful:
 * callers must still require `digitalTwinReset === true` before admitting it.
 */
export function normalizeExecutionRouteCommandResult(
  value: unknown,
): ExecutionRouteCommandResult | null {
  const raw = String(value ?? "").trim();
  if (!raw || raw.length > MAX_ROS_JSON_PAYLOAD_CHARS) return null;
  try {
    const payload = JSON.parse(raw) as Record<string, unknown>;
    if (
      !isBoundedRosPayload(payload)
      || typeof payload.digital_twin_reset !== "boolean"
    ) {
      return null;
    }
    const state = normalizeExecutionRouteState({ data: raw });
    return state
      ? { state, digitalTwinReset: payload.digital_twin_reset }
      : null;
  } catch {
    return null;
  }
}

export function integrationReadinessBlockReason(
  readiness: IntegrationReadiness | null,
  receivedAt: number | null,
  expectedBundle: string,
  now = Date.now(),
): IntegrationReadinessBlockReason | null {
  if (!readiness || !receivedAt) return "missing";
  const receivedAgeMs = now - receivedAt;
  const sourceAgeMs = now - readiness.stampSec * 1_000;
  if (
    receivedAgeMs < -INTEGRATION_READINESS_MAX_FUTURE_SKEW_MS
    || receivedAgeMs > INTEGRATION_READINESS_MAX_AGE_MS
    || sourceAgeMs < -INTEGRATION_READINESS_MAX_FUTURE_SKEW_MS
    || sourceAgeMs > INTEGRATION_READINESS_MAX_SOURCE_AGE_MS
  ) {
    return "stale";
  }
  const normalizedExpectedBundle = expectedBundle.trim();
  if (
    !normalizedExpectedBundle
    || readiness.activeBundle !== normalizedExpectedBundle
  ) {
    return "bundle_mismatch";
  }
  return readiness.ready ? null : "not_ready";
}
