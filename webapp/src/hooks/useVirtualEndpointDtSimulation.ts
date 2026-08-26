import { useMemo } from "react";

import type { ExecutionTrace, SkillStatus } from "../types";

/**
 * The virtual controller endpoints are deliberately isolated from the public
 * robot-controller names.  Requiring this prefix in addition to the selected
 * source prevents a stale selector from making an external trace look like a
 * virtual visualisation.
 */
const VIRTUAL_ENDPOINT_PREFIX = "/integration/virtual/";
const MAX_TERMINAL_PROJECTIONS = 12;

export type VirtualDtRunSnapshot = {
  /** A fresh, browser-local id generated when the integration run is started. */
  id: string;
  /** Browser time captured before the start request is sent. */
  startedAtMs: number;
  /** Source latched at the run boundary, never the live selector value. */
  endpointSourceAtStart: string;
  /** The authoritative runtime is currently executing this run. */
  active: boolean;
};

export type VirtualDtSimulationState =
  | "dispatching"
  | "accepted"
  | "executing"
  | "completed"
  | "rejected"
  | "failed"
  | "canceled"
  | "unknown";

export type VirtualDtRoute = {
  route: string;
  sourceLocationId: string;
  sourceLocationType: string;
  targetLocationId: string;
  targetLocationType: string;
  targetOwner: string;
};

/**
 * A browser-only terminal projection.  This is not an authoritative DT
 * mutation and is never physical-motion evidence.  Consumers may use it only
 * to update the visual projection after the virtual Action terminal result is
 * corroborated by the matching SkillStatus.
 */
export type VirtualDtTerminalProjection = {
  commandId: string;
  toolId: string;
  toolInstanceId: string;
  action: string;
  route: VirtualDtRoute;
  completedAtMs: number;
  source: "virtual";
  kind: "tool_handover";
};

export type VirtualDtCommandSimulation = {
  commandId: string;
  transport: "action" | "service";
  endpoint: string;
  route: VirtualDtRoute;
  action: string;
  toolId: string;
  toolInstanceId: string;
  state: VirtualDtSimulationState;
  /** UI-only, bounded route progress. It is never robot-state telemetry. */
  progress: number;
  terminal: boolean;
  /** A Service admission does not represent robot execution or completion. */
  admissionOnly: boolean;
  reasonCode: string;
  observedAtMs: number;
  terminalProjection: VirtualDtTerminalProjection | null;
};

export type VirtualDtSimulation = {
  runId: string;
  source: "virtual";
  current: VirtualDtCommandSimulation | null;
  terminalProjections: readonly VirtualDtTerminalProjection[];
};

export type VirtualDtSimulationInput = {
  run: VirtualDtRunSnapshot | null;
  executionTraces: readonly ExecutionTrace[];
  skillStatusByCommand: Readonly<Record<string, SkillStatus>>;
};

function normalized(value: unknown): string {
  return String(value ?? "").trim();
}

function bounded(value: unknown, fallback = ""): string {
  const text = normalized(value).replace(/\s+/g, " ");
  return (text || fallback).slice(0, 128);
}

function isVirtualSource(value: string): boolean {
  return normalized(value).toLowerCase() === "virtual";
}

function isIsolatedVirtualTrace(trace: ExecutionTrace, run: VirtualDtRunSnapshot): boolean {
  if (!run.active || !run.id || !isVirtualSource(run.endpointSourceAtStart)) return false;
  const commandId = normalized(trace.command_id);
  const endpoint = normalized(trace.endpoint);
  const receivedAt = Number(trace.receivedAt);
  return Boolean(
    commandId &&
      trace.dispatch_submitted &&
      endpoint.startsWith(VIRTUAL_ENDPOINT_PREFIX) &&
      Number.isFinite(receivedAt) &&
      receivedAt >= run.startedAtMs,
  );
}

function compareTrace(left: ExecutionTrace, right: ExecutionTrace): number {
  const receivedAtDelta = Number(left.receivedAt) - Number(right.receivedAt);
  if (receivedAtDelta) return receivedAtDelta;
  return Number(left.sequence) - Number(right.sequence);
}

function boundedProgress(value: unknown): number | null {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return null;
  return Math.max(0, Math.min(1, numeric));
}

function matchingStatus(
  commandId: string,
  byCommand: Readonly<Record<string, SkillStatus>>,
): SkillStatus | null {
  const status = byCommand[commandId];
  return status && normalized(status.command_id) === commandId ? status : null;
}

function routeFor(trace: ExecutionTrace, status: SkillStatus | null): VirtualDtRoute {
  return {
    route: bounded(trace.route, "virtual_route"),
    sourceLocationId: bounded(status?.source_location_id, "source"),
    sourceLocationType: bounded(status?.source_location_type),
    targetLocationId: bounded(status?.target_location_id, "target"),
    targetLocationType: bounded(status?.target_location_type),
    targetOwner: bounded(status?.target_owner),
  };
}

function simulationStateFor(
  trace: ExecutionTrace,
  status: SkillStatus | null,
): VirtualDtSimulationState {
  if (trace.terminal) {
    if (trace.stage === "completed") return "completed";
    // A Service V1 response is terminal as an admission receipt.  It is not
    // execution or physical-completion evidence, but it should remain visibly
    // distinct from an unrecognised terminal state in the virtual DT panel.
    if (trace.transport === "service" && trace.stage === "accepted") {
      return "accepted";
    }
    if (trace.stage === "rejected") return "rejected";
    if (trace.stage === "failed") return "failed";
    if (trace.stage === "canceled") return "canceled";
    return "unknown";
  }

  const statusState = normalized(status?.state).toLowerCase();
  if (
    statusState &&
    statusState !== "dispatching" &&
    statusState !== "accepted" &&
    statusState !== "pending"
  ) {
    return "executing";
  }
  return trace.stage === "accepted" ? "accepted" : "dispatching";
}

function progressFor(
  trace: ExecutionTrace,
  status: SkillStatus | null,
  state: VirtualDtSimulationState,
): number {
  const statusProgress = boundedProgress(status?.progress);
  if (state === "completed") return 1;
  if (trace.transport === "service" && trace.terminal && trace.stage === "accepted") {
    // This is the end of the virtual Service *receipt* path, not motion.
    return 1;
  }
  if (state === "rejected" || state === "failed" || state === "canceled" || state === "unknown") {
    return Math.max(0.08, statusProgress ?? 0);
  }
  const baseline = state === "executing" ? 0.52 : state === "accepted" ? 0.32 : 0.14;
  return Math.max(baseline, statusProgress ?? 0);
}

function hasTerminalVirtualActionSuccess(
  trace: ExecutionTrace,
  status: SkillStatus | null,
): boolean {
  // A virtual terminal projection is intentionally stricter than a visual
  // "completed" label: both the transport's completed result and the matching
  // SkillStatus success fact must agree.
  return Boolean(
    trace.transport === "action" &&
      trace.route === "tool_transfer" &&
      trace.terminal &&
      trace.stage === "completed" &&
      trace.evidence === "controller_result" &&
      status?.success === true &&
      normalized(status.state).toLowerCase() === "completed",
  );
}

function terminalProjectionFor(
  trace: ExecutionTrace,
  status: SkillStatus | null,
): VirtualDtTerminalProjection | null {
  if (!status || !hasTerminalVirtualActionSuccess(trace, status)) return null;
  const commandId = normalized(trace.command_id);
  const toolId = bounded(status.instrument_id);
  if (!commandId || !toolId) return null;
  return {
    commandId,
    toolId,
    toolInstanceId: bounded(status.instrument_instance_id),
    action: bounded(status.action, "tool_handover"),
    route: routeFor(trace, status),
    completedAtMs: Number(trace.receivedAt),
    source: "virtual",
    kind: "tool_handover",
  };
}

function commandSimulationFor(
  trace: ExecutionTrace,
  byCommand: Readonly<Record<string, SkillStatus>>,
): VirtualDtCommandSimulation {
  const commandId = normalized(trace.command_id);
  const status = matchingStatus(commandId, byCommand);
  const state = simulationStateFor(trace, status);
  const projection = terminalProjectionFor(trace, status);
  return {
    commandId,
    transport: trace.transport === "action" ? "action" : "service",
    endpoint: bounded(trace.endpoint),
    route: routeFor(trace, status),
    action: bounded(status?.action, bounded(trace.route, "virtual_request")),
    toolId: bounded(status?.instrument_id),
    toolInstanceId: bounded(status?.instrument_instance_id),
    state,
    progress: progressFor(trace, status, state),
    terminal: trace.terminal,
    admissionOnly: trace.transport === "service",
    reasonCode: bounded(trace.reason_code),
    observedAtMs: Number(trace.receivedAt),
    terminalProjection: projection,
  };
}

/**
 * Derive a virtual-only browser presentation from observer topics.  This
 * function is pure so the boundary can be tested without dispatching ROS
 * work. It never changes the authoritative Digital Twin.
 */
export function deriveVirtualDtSimulation({
  run,
  executionTraces,
  skillStatusByCommand,
}: VirtualDtSimulationInput): VirtualDtSimulation | null {
  if (!run || !run.active || !run.id || !isVirtualSource(run.endpointSourceAtStart)) return null;
  if (!Number.isFinite(run.startedAtMs) || run.startedAtMs < 0) return null;

  const latestByCommand = new Map<string, ExecutionTrace>();
  for (const trace of executionTraces) {
    if (!isIsolatedVirtualTrace(trace, run)) continue;
    const commandId = normalized(trace.command_id);
    const previous = latestByCommand.get(commandId);
    if (!previous || compareTrace(previous, trace) < 0) {
      latestByCommand.set(commandId, trace);
    }
  }

  const commands = [...latestByCommand.values()]
    .sort(compareTrace)
    .map((trace) => commandSimulationFor(trace, skillStatusByCommand));
  const current = commands.length ? commands[commands.length - 1] : null;
  const terminalProjections = commands
    .map((command) => command.terminalProjection)
    .filter((projection): projection is VirtualDtTerminalProjection => projection !== null)
    .slice(-MAX_TERMINAL_PROJECTIONS);

  return {
    runId: run.id,
    source: "virtual",
    current,
    terminalProjections,
  };
}

/**
 * Observer-only hook for the virtual Action/Service route.
 *
 * `run.endpointSourceAtStart` must be latched at the Start boundary by the
 * caller. A selector value read after a run begins must not be used here.
 */
export function useVirtualEndpointDtSimulation(
  input: VirtualDtSimulationInput,
): VirtualDtSimulation | null {
  return useMemo(
    () => deriveVirtualDtSimulation(input),
    [input.executionTraces, input.run, input.skillStatusByCommand],
  );
}
