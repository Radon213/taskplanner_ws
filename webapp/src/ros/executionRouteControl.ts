import type { TaskplannerRuntimeMode } from "../runtimeModes";
import type { SimulationState } from "../types";
import type { ExecutionRouteState } from "./runtimeAdmissionMessages";

export type ExecutionRouteSwitchContext = {
  runtimeMode: TaskplannerRuntimeMode;
  connected: boolean;
  runtimeStateFresh: boolean;
  routeState: ExecutionRouteState | null;
  actionInFlight: boolean;
  controlInFlight: boolean;
  simulationState: SimulationState;
};

/**
 * Browser-side explanation for why the independently owned execution-route
 * service cannot be changed. The service remains the authority: this helper
 * only keeps the UI from issuing an obviously invalid request.
 */
export function executionRouteSwitchBlockReason(
  context: ExecutionRouteSwitchContext,
): string | null {
  if (context.runtimeMode !== "live") {
    return "Action/Service route selection is available only in live integration mode.";
  }
  if (!context.runtimeStateFresh || !context.connected) {
    return "Wait for a fresh live runtime state before changing the Action/Service route.";
  }
  const routeState = context.routeState;
  if (!routeState || !routeState.routeControlEnabled) {
    return "The live runtime has not confirmed stopped-state Action/Service route control.";
  }
  if (routeState.initializationState === "initializing") {
    return "Wait for the selected Action/Service route to be acknowledged by the integration start check.";
  }
  if (context.actionInFlight || context.controlInFlight) {
    return "Wait for the current runtime control request before changing the Action/Service route.";
  }
  const state = context.simulationState;
  const executionState = String(state.execution_state || "").trim().toLowerCase();
  if (
    state.running
    || !["idle", "halted", "completed", "terminated"].includes(executionState)
    || String(state.robot_state || "").trim().toLowerCase() !== "idle"
    || Boolean(state.active_robot_task_id)
    || state.cleaner_busy
    || state.pending_transition_tools.length > 0
    || state.active_recovery_tools.length > 0
  ) {
    return "Stop planner execution and all active work before changing the Action/Service route.";
  }
  return null;
}
