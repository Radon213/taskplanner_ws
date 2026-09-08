import type { SimulationState } from "../types";
import { parseBoundedJson } from "../utils/display";

export const TOOL_POLICY_STATUS_TOPIC = "/twin/tool_policy_status";
export const TOOL_POLICY_STATUS_SCHEMA = "taskplanner.tool_policy_status.v1";

export type ToolPolicyCandidate = {
  instrument_id: string;
  /** N-gram p(next tool), not VLM reuse demand or camera confidence. */
  probability: number;
  stability_sec: number;
};

type ToolPolicyCondition = {
  probability_threshold: number;
  comparison: "gte" | "lte";
  dwell_sec: number;
};

export type ToolPolicyStatus = {
  schema: typeof TOOL_POLICY_STATUS_SCHEMA;
  source: "handover_ngram_0704";
  procedure_id: string;
  procedure_run_id: string;
  running: boolean;
  execution_state: string;
  prepare: ToolPolicyCondition & { candidate: ToolPolicyCandidate | null };
  recovery: ToolPolicyCondition & {
    enabled_instrument_ids: string[];
    candidates: (ToolPolicyCandidate & { instance_id: string })[];
  };
};

function record(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function probability(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function condition(value: unknown): value is ToolPolicyCondition {
  return record(value)
    && probability(value.probability_threshold)
    && (value.comparison === "gte" || value.comparison === "lte")
    && typeof value.dwell_sec === "number"
    && Number.isFinite(value.dwell_sec) && value.dwell_sec > 0;
}

function candidate(value: unknown): value is ToolPolicyCandidate {
  return record(value)
    && typeof value.instrument_id === "string" && Boolean(value.instrument_id.trim())
    && probability(value.probability)
    && typeof value.stability_sec === "number"
    && Number.isFinite(value.stability_sec) && value.stability_sec >= 0;
}

/** Missing/invalid policy hides only its timer; never invent local defaults. */
export function normalizeToolPolicyStatus(message: unknown): ToolPolicyStatus | null {
  if (!record(message) || typeof message.data !== "string") return null;
  const value = parseBoundedJson(message.data);
  if (!record(value)
    || value.schema !== TOOL_POLICY_STATUS_SCHEMA || value.source !== "handover_ngram_0704"
    || typeof value.procedure_id !== "string" || typeof value.procedure_run_id !== "string"
    || typeof value.running !== "boolean" || typeof value.execution_state !== "string"
    || !condition(value.prepare) || !condition(value.recovery)) return null;
  const prepare = value.prepare as unknown as Record<string, unknown>;
  const recovery = value.recovery as unknown as Record<string, unknown>;
  if ((prepare.candidate !== null && !candidate(prepare.candidate))
    || !Array.isArray(recovery.enabled_instrument_ids)
    || !recovery.enabled_instrument_ids.every((id) => typeof id === "string")
    || !Array.isArray(recovery.candidates)
    || !recovery.candidates.every((row) => record(row)
      && typeof row.instance_id === "string" && Boolean(row.instance_id.trim())
      && candidate(row))) return null;
  return value as unknown as ToolPolicyStatus;
}

/** Retained data is valid only for the currently running procedure epoch. */
export function activeToolPolicyStatus(
  status: ToolPolicyStatus | null,
  scope: Pick<SimulationState, "procedure_id" | "procedure_run_id" | "running" | "execution_state">,
): ToolPolicyStatus | null {
  return status && scope.running && scope.execution_state === "running"
    && status.running && status.execution_state === "running"
    && Boolean(scope.procedure_run_id)
    && status.procedure_run_id === scope.procedure_run_id
    && status.procedure_id === scope.procedure_id
    ? status : null;
}

/** roslib 1.x needs the same QoS decoration used by existing preview topics. */
export function configureToolPolicySubscription(topic: {
  callForSubscribeAndAdvertise: (request: Record<string, unknown>) => void;
}): void {
  const send = topic.callForSubscribeAndAdvertise.bind(topic);
  topic.callForSubscribeAndAdvertise = (request) => send(request.op === "subscribe"
    ? { ...request, qos: {
        history: "keep_last", depth: 1,
        reliability: "reliable", durability: "transient_local",
      } }
    : request);
}
