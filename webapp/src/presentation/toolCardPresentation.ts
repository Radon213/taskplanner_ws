import type { StageToolChipPlacement } from "../hooks/useDigitalTwinViewModel";
import type { RankedToolPrediction, VLMResult } from "../types";
import type { ToolPolicyStatus } from "../ros/toolPolicyMessages";
import { parseBoundedJson } from "../utils/display";

export type ToolVlmEvidence = {
  nextToolProbability: number | null;
  /** The reducer-accepted ranking, bounded to the operator-visible top 3. */
  nextToolRank: number | null;
  /**
   * Probability that the surgeon will need this Mayo tool again later in the
   * active procedure. This is intentionally independent of camera detection
   * and Mayo placement confidence.
   */
  demandForecastProbability: number | null;
  /** A current CAM/VLM Mayo observer row exists; it is never a probability. */
  mayoObserved: boolean;
  /** Reducer-owned continuity clock for a system-next tool request. */
  nextToolConfirmation: ToolConfirmationCountdown | null;
  /** DT n-gram continuity clock for Mayo recovery, independent of VLM votes. */
  mayoConfirmation: ToolConfirmationCountdown | null;
};

/**
 * Display-only projection of a continuity condition already maintained by the
 * Digital Twin. It never admits, dispatches, or changes an Action.
 */
export type ToolConfirmationCountdown = {
  kind: "request" | "recovery" | "reuse";
  confidence: number;
  confidenceThreshold: number;
  probabilityComparison: "gte" | "lte";
  stabilitySec: number;
  requiredStabilitySec: number;
  remainingSec: number;
  progress: number;
  ready: boolean;
};

export type SystemToolPrediction = {
  rank: number;
  confidence: number;
  stabilitySec: number;
};

export type StageToolCardPresentation = {
  evidence: ToolVlmEvidence;
  nextToolAuthority: "system";
};

const MAX_TOOL_DEMAND_FORECASTS = 24;
const MAX_MAYO_OBSERVATIONS = 24;

function normalizedId(value: unknown): string {
  return typeof value === "string"
    ? value.trim().replace(/[\s-]+/g, "_").slice(0, 96)
    : "";
}

function normalizedProbability(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1
    ? value
    : null;
}

function normalizedStability(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : 0;
}

function projectConfirmationCountdown({
  kind,
  confidence,
  confidenceThreshold,
  probabilityComparison,
  stabilitySec,
  requiredStabilitySec,
}: {
  kind: ToolConfirmationCountdown["kind"];
  confidence: number;
  confidenceThreshold: number;
  probabilityComparison: "gte" | "lte";
  stabilitySec: number;
  requiredStabilitySec: number;
}): ToolConfirmationCountdown | null {
  if (normalizedProbability(confidence) === null
    || normalizedProbability(confidenceThreshold) === null
    || !Number.isFinite(requiredStabilitySec) || requiredStabilitySec <= 0
    || (probabilityComparison === "gte"
      ? confidence < confidenceThreshold : confidence > confidenceThreshold)) return null;
  const normalizedDuration = normalizedStability(stabilitySec);
  const requiredDuration = requiredStabilitySec;
  const remainingSec = Math.max(0, requiredDuration - normalizedDuration);
  return {
    kind,
    confidence,
    confidenceThreshold,
    probabilityComparison,
    stabilitySec: normalizedDuration,
    requiredStabilitySec: requiredDuration,
    remainingSec,
    progress: Math.max(0, Math.min(1, normalizedDuration / requiredDuration)),
    ready: normalizedDuration >= requiredDuration,
  };
}

function rawVlmPayload(vlmResult: VLMResult): Record<string, unknown> | null {
  const parsed = parseBoundedJson(vlmResult.raw_json);
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? parsed as Record<string, unknown>
    : null;
}

/**
 * Decode the current VLM demand forecast sidecar.
 *
 * The contract is deliberately narrow: `tool_demand_forecast` is an array of
 * `[instrument_id, probability]` rows. It represents future surgeon demand,
 * not camera confidence, Mayo location, or a command admission decision.
 */
export function toolDemandForecastById(vlmResult: VLMResult): ReadonlyMap<string, number> {
  const payload = rawVlmPayload(vlmResult);
  const forecasts = new Map<string, number>();
  if (!payload || !Array.isArray(payload.tool_demand_forecast)) return forecasts;

  for (const row of payload.tool_demand_forecast.slice(0, MAX_TOOL_DEMAND_FORECASTS)) {
    if (!Array.isArray(row) || row.length !== 2) continue;
    const toolId = normalizedId(row[0]);
    const probability = normalizedProbability(row[1]);
    if (!toolId || probability === null) continue;
    // One VLM turn should emit one row per tool. Keep the strongest valid row
    // rather than manufacturing an average from malformed duplicates.
    const previous = forecasts.get(toolId);
    if (previous === undefined || probability > previous) forecasts.set(toolId, probability);
  }
  return forecasts;
}

/**
 * Return only the identity of current Mayo observations. Legacy rows happen
 * to carry a recover/reuse token and a confidence, but presentation ignores
 * both: neither is a future-demand estimate.
 */
export function mayoObservedToolIds(vlmResult: VLMResult): ReadonlySet<string> {
  const payload = rawVlmPayload(vlmResult);
  const observed = new Set<string>();
  if (!payload || !Array.isArray(payload.mayo_observation)) return observed;

  for (const row of payload.mayo_observation.slice(0, MAX_MAYO_OBSERVATIONS)) {
    if (!Array.isArray(row) || row.length !== 3) continue;
    const toolId = normalizedId(row[0]);
    // Retain the existing bounded-row validation without using the score.
    if (!toolId || normalizedProbability(row[2]) === null) continue;
    observed.add(toolId);
  }
  return observed;
}

export function systemToolPredictionsById(
  predictions: readonly RankedToolPrediction[],
): ReadonlyMap<string, SystemToolPrediction> {
  const rows = [...predictions]
    .filter((prediction) => {
      const rank = Number(prediction.rank);
      const confidence = Number(prediction.confidence);
      return (
        Number.isInteger(rank)
        && rank >= 1
        && rank <= 3
        && Boolean(prediction.instrument_id.trim())
        && Number.isFinite(confidence)
        && confidence >= 0
        && confidence <= 1
      );
    })
    .sort((left, right) => left.rank - right.rank)
    .slice(0, 3);
  const byId = new Map<string, SystemToolPrediction>();
  for (const prediction of rows) {
    if (byId.has(prediction.instrument_id)) continue;
    byId.set(prediction.instrument_id, {
      rank: prediction.rank,
      confidence: prediction.confidence,
      stabilitySec: normalizedStability(prediction.stability_sec),
    });
  }
  return byId;
}

/**
 * Owns every tool-card visibility choice derived from existing VLM and
 * real-to-sim facts. The final projected holder is authoritative: a card only
 * becomes a Mayo card after real-to-sim (when enabled) places it on Mayo.
 */
export function projectToolCardPresentation({
  chip,
  procedureRunning,
  demandForecastReady = false,
  demandForecastByToolId = new Map<string, number>(),
  mayoObservedToolIdSet = new Set<string>(),
  systemPrediction,
  toolPolicyStatus,
}: {
  chip: Pick<
    StageToolChipPlacement,
    "holderId" | "instrumentId" | "instanceIds" | "displayInstanceId" | "canonicalMayoDecision"
  >;
  procedureRunning: boolean;
  /** The raw VLM result itself is current enough to display its sidecars. */
  demandForecastReady?: boolean;
  demandForecastByToolId?: ReadonlyMap<string, number>;
  mayoObservedToolIdSet?: ReadonlySet<string>;
  systemPrediction?: SystemToolPrediction;
  /** Run-scoped, read-only snapshot of the actual DT n-gram policy. */
  toolPolicyStatus?: ToolPolicyStatus | null;
}): StageToolCardPresentation {
  const isMayo = chip.holderId === "mayo";
  const normalizedInstrumentId = normalizedId(chip.instrumentId);
  const showFreshMayoVlmEvidence = isMayo && procedureRunning && demandForecastReady;
  const policy = procedureRunning && toolPolicyStatus?.running
    && toolPolicyStatus.execution_state === "running" && toolPolicyStatus.procedure_run_id
    ? toolPolicyStatus : null;
  const preparation = policy?.prepare.candidate;
  const representedInstances = chip.displayInstanceId
    ? [chip.displayInstanceId] : chip.instanceIds ?? [];
  const recovery = policy?.recovery.candidates
    .filter((candidate) => candidate.instrument_id === chip.instrumentId
      && (!representedInstances.length || representedInstances.includes(candidate.instance_id)))
    .sort((left, right) => right.stability_sec - left.stability_sec)[0];
  const nextToolConfirmation = policy && preparation?.instrument_id === chip.instrumentId
    ? projectConfirmationCountdown({
        kind: "request",
        confidence: preparation.probability,
        confidenceThreshold: policy.prepare.probability_threshold,
        probabilityComparison: policy.prepare.comparison,
        stabilitySec: preparation.stability_sec,
        requiredStabilitySec: policy.prepare.dwell_sec,
      })
    : null;
  const mayoConfirmation = policy && isMayo && recovery
    ? projectConfirmationCountdown({
        kind: "recovery",
        confidence: recovery.probability,
        confidenceThreshold: policy.recovery.probability_threshold,
        probabilityComparison: policy.recovery.comparison,
        stabilitySec: recovery.stability_sec,
        requiredStabilitySec: policy.recovery.dwell_sec,
      })
    : null;

  return {
    evidence: {
      // A Mayo card has its own future-demand forecast. A different system
      // next-tool rank stays visible only for non-Mayo cards.
      nextToolProbability: procedureRunning && !isMayo
        ? systemPrediction?.confidence ?? null
        : null,
      nextToolRank: procedureRunning && !isMayo
        ? systemPrediction?.rank ?? null
        : null,
      demandForecastProbability: showFreshMayoVlmEvidence
        ? demandForecastByToolId.get(normalizedInstrumentId) ?? null
        : null,
      mayoObserved: showFreshMayoVlmEvidence
        && mayoObservedToolIdSet.has(normalizedInstrumentId),
      nextToolConfirmation,
      mayoConfirmation,
    },
    nextToolAuthority: "system",
  };
}
