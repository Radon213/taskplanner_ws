import type { SimulationState } from "../types";

export const SELECT_BUNDLE_SERVICE = "/simulation/select_bundle";
export const SELECT_BUNDLE_SERVICE_TYPE = "surgical_msgs/srv/SelectSimulationBundle";

const MAX_BUNDLE_NAME_CHARS = 128;
const MAX_MESSAGE_CHARS = 1_024;
const MAX_REVISION_CHARS = 256;
const MAX_DISPOSITION_CHARS = 64;

export type ScenarioRevisionDisposition =
  | "preview_change_available"
  | "preview_unchanged"
  | "change_available"
  | "unchanged"
  | "applied"
  | "deferred_paused_or_stopped_required"
  | "deferred"
  | "blocked"
  | "rejected"
  | (string & {});

export type ScenarioRevisionResult = {
  success: boolean;
  message: string;
  activeBundle: string;
  activeRevision: string;
  candidateRevision: string;
  changed: boolean;
  applied: boolean;
  disposition: ScenarioRevisionDisposition;
};

export type ScenarioRevisionPhase =
  | "idle"
  | "previewing"
  | "previewed"
  | "applying"
  | "applied"
  | "failed";

export type ScenarioRevisionState = {
  phase: ScenarioRevisionPhase;
  bundleName: string;
  message: string;
  result: ScenarioRevisionResult | null;
};

export type ScenarioRevisionApplyAdmission = {
  allowed: boolean;
  reason: string;
  restartIfRunning: boolean;
  reloadIfChanged: boolean;
};

export const EMPTY_SCENARIO_REVISION_STATE: ScenarioRevisionState = {
  phase: "idle",
  bundleName: "",
  message: "",
  result: null,
};

function boundedString(
  value: unknown,
  field: string,
  maxChars: number,
  { allowEmpty = true }: { allowEmpty?: boolean } = {},
): string {
  if (typeof value !== "string") {
    throw new Error(`Scenario revision response field ${field} must be a string.`);
  }
  const normalized = value.trim();
  if ((!allowEmpty && !normalized) || normalized.length > maxChars) {
    throw new Error(`Scenario revision response field ${field} is invalid.`);
  }
  return normalized;
}

function booleanField(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`Scenario revision response field ${field} must be a boolean.`);
  }
  return value;
}

/**
 * Validate the additive SelectSimulationBundle result before it reaches the UI.
 * Unknown disposition tokens remain displayable so a newer server can add a
 * result without making the browser unsafe or unusable.
 */
export function parseScenarioRevisionResult(
  response: Record<string, unknown>,
): ScenarioRevisionResult {
  const disposition = boundedString(
    response.disposition,
    "disposition",
    MAX_DISPOSITION_CHARS,
    { allowEmpty: false },
  );
  if (!/^[a-z][a-z0-9_]*$/.test(disposition)) {
    throw new Error("Scenario revision response disposition is invalid.");
  }
  return {
    success: booleanField(response.success, "success"),
    message: boundedString(response.message, "message", MAX_MESSAGE_CHARS),
    activeBundle: boundedString(
      response.active_bundle,
      "active_bundle",
      MAX_BUNDLE_NAME_CHARS,
      { allowEmpty: false },
    ),
    activeRevision: boundedString(
      response.active_config_revision,
      "active_config_revision",
      MAX_REVISION_CHARS,
    ),
    candidateRevision: boundedString(
      response.candidate_config_revision,
      "candidate_config_revision",
      MAX_REVISION_CHARS,
    ),
    changed: booleanField(response.changed, "changed"),
    applied: booleanField(response.applied, "applied"),
    disposition,
  };
}

export function scenarioRevisionPreviewRequest(bundleName: string) {
  return {
    bundle_name: bundleName,
    restart_if_running: false,
    preview_only: true,
    reload_if_changed: false,
    expected_candidate_revision: "",
  };
}

function pausedOrStopped(state: SimulationState): boolean {
  const executionState = state.execution_state.trim().toLowerCase();
  if (executionState === "paused") return true;
  return (
    !state.running &&
    ["idle", "halted", "completed", "terminated"].includes(executionState)
  );
}

/**
 * Browser affordance derived only from the server-authored runtime frame and
 * the latest server preview. The SelectSimulationBundle server remains the
 * admission authority and rechecks the same transition at call time.
 */
export function scenarioRevisionApplyAdmission({
  state,
  selectedBundle,
  revision,
  stateFresh,
  commandPending,
}: {
  state: SimulationState;
  selectedBundle: string;
  revision: ScenarioRevisionState;
  stateFresh: boolean;
  commandPending: boolean;
}): ScenarioRevisionApplyAdmission {
  const blocked = (reason: string): ScenarioRevisionApplyAdmission => ({
    allowed: false,
    reason,
    restartIfRunning: false,
    reloadIfChanged: false,
  });
  if (!selectedBundle) return blocked("Select a procedure bundle first.");
  if (!stateFresh) return blocked("Wait for a fresh server runtime state before applying changes.");
  if (commandPending) return blocked("Wait for the current runtime request to finish.");
  if (
    revision.bundleName !== selectedBundle ||
    revision.phase !== "previewed" ||
    !revision.result
  ) {
    return blocked("Preview this bundle revision before applying it.");
  }
  if (!revision.result.success) {
    return blocked(revision.result.message || "The server rejected this revision preview.");
  }
  if (!revision.result.changed) return blocked("The selected bundle already matches the active revision.");
  const activeBundle = state.active_bundle.trim();
  if (
    revision.result.activeBundle !== activeBundle ||
    !revision.result.candidateRevision ||
    revision.result.applied ||
    revision.result.disposition !== "preview_change_available"
  ) {
    return blocked("Preview this bundle revision before applying it.");
  }
  if (!pausedOrStopped(state)) {
    return blocked("Pause or stop the scenario before changing bundles.");
  }
  return {
    allowed: true,
    reason: "",
    restartIfRunning: false,
    reloadIfChanged: selectedBundle === activeBundle,
  };
}

export function scenarioRevisionApplyRequest(
  bundleName: string,
  admission: ScenarioRevisionApplyAdmission,
  expectedCandidateRevision: string,
) {
  return {
    bundle_name: bundleName,
    restart_if_running: admission.restartIfRunning,
    preview_only: false,
    reload_if_changed: admission.reloadIfChanged,
    expected_candidate_revision: expectedCandidateRevision,
  };
}
