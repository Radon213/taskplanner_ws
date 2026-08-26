import { memo } from "react";
import {
  BrainCircuit,
  CheckCircle2,
  CircleAlert,
  CircleX,
  MessageSquareText,
  RadioTower,
  RotateCcw,
  Send,
} from "lucide-react";

import type { ExecutionTrace, VLMResult } from "../../types";
import { parseBoundedJson, type Language } from "../../utils/display";
import "./operation-vlm-observability.css";

/**
 * The visualized result is intentionally a VLM proposal, never an execution
 * decision. Consumers must supply independently observed dispatch evidence to
 * the dispatch components below.
 */
export type VlmToolProbability = {
  toolId: string;
  confidence: number;
};

export type VlmMayoDisposition = "recover" | "reuse";

export type VlmMayoDecision = {
  toolId: string;
  disposition: VlmMayoDisposition;
  confidence: number;
};

export type VlmOperationObservations = {
  nextToolProbabilities: readonly VlmToolProbability[];
  mayoDecisions: readonly VlmMayoDecision[];
  source: string;
  schemaVersion: string;
  uncertainty: number | null;
};

export type ToolVlmEvidence = {
  nextToolProbability: number | null;
  /** The VLM ranking for this candidate, bounded to the operator-visible top 3. */
  nextToolRank: number | null;
  mayoDecision: VlmMayoDecision | null;
};

/** Actual transport/endpoint evidence supplied by the bridge, not VLM output. */
export type ExecutionDispatchKind = "action" | "service";

/**
 * `proposed` is deliberately separate from `sent`: only the latter and later
 * states mean a client-side Action/Service request was actually emitted.
 */
export type ExecutionDispatchState =
  | "proposed"
  | "sent"
  | "accepted"
  | "completed"
  | "rejected"
  | "failed"
  | "unknown";

export type ExecutionDispatchEvent = {
  id: string;
  kind: ExecutionDispatchKind;
  name: string;
  state: ExecutionDispatchState;
  occurredAt: number;
  sequence: number;
  commandId?: string;
  /**
   * The instrument is correlated separately through the typed SkillStatus
   * command_id.  It is intentionally optional because an execution trace
   * alone never carries a tool identifier.
   */
  toolId?: string;
  toolLabel?: string;
  toolInstanceId?: string;
  /**
   * Tool-flow locations are independently reported in the matching
   * `SkillStatus`; they describe movement such as Mayo stand → surgeon, not
   * the software transport source/destination.
   */
  sourceLocationId?: string;
  sourceLocationType?: string;
  targetLocationId?: string;
  targetLocationType?: string;
  targetOwner?: string;
  route?: string;
  /** Exact typed payload copied from a dispatched retraction Service call. */
  retractionCommand?: number;
  retractionTargetSide?: number;
  retractionDistanceCm?: number;
  detail?: string;
};

const MAX_DISPLAYED_NEXT_TOOL_RANK = 3;
const MAX_OBSERVED_CANDIDATES = 24;
const MAX_DISPLAY_DISPATCHES = 8;

function normalizeId(value: unknown): string {
  if (typeof value !== "string") return "";
  return value.trim().replace(/[\s-]+/g, "_").slice(0, 96);
}

function normalizeProbability(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) {
    return null;
  }
  return value;
}

function normalizeVlmToken(value: string): string {
  return value.trim().toLocaleLowerCase().replace(/[\s-]+/g, "_");
}

function candidateRows(value: unknown): unknown[][] {
  if (!Array.isArray(value)) return [];
  // V2 sent a single [tool, confidence] pair; V3/V4 send a ranked list.
  if (
    value.length === 2
    && typeof value[0] === "string"
    && typeof value[1] === "number"
  ) {
    return [value];
  }
  return value.filter((row): row is unknown[] => Array.isArray(row));
}

function sortedToolProbabilities(rows: unknown[][]): VlmToolProbability[] {
  const candidates = new Map<string, number>();
  for (const row of rows.slice(0, MAX_OBSERVED_CANDIDATES)) {
    if (row.length !== 2) continue;
    const toolId = normalizeId(row[0]);
    const confidence = normalizeProbability(row[1]);
    if (!toolId || confidence === null) continue;
    const previous = candidates.get(toolId);
    if (previous === undefined || confidence > previous) candidates.set(toolId, confidence);
  }
  return [...candidates.entries()]
    .map(([toolId, confidence]) => ({ toolId, confidence }))
    .sort((left, right) => right.confidence - left.confidence || left.toolId.localeCompare(right.toolId));
}

function sortedMayoDecisions(value: unknown): VlmMayoDecision[] {
  if (!Array.isArray(value)) return [];
  const decisions = new Map<string, VlmMayoDecision>();
  for (const row of value.slice(0, MAX_OBSERVED_CANDIDATES)) {
    if (!Array.isArray(row) || row.length !== 3) continue;
    const toolId = normalizeId(row[0]);
    const disposition = normalizeVlmToken(typeof row[1] === "string" ? row[1] : "");
    const confidence = normalizeProbability(row[2]);
    if (!toolId || confidence === null || (disposition !== "recover" && disposition !== "reuse")) {
      continue;
    }
    const next: VlmMayoDecision = { toolId, disposition, confidence };
    const previous = decisions.get(toolId);
    // Match schema normalization: an equal-confidence reuse decision wins over
    // recovery so the browser never promotes an ambiguous recovery proposal.
    if (
      !previous
      || next.confidence > previous.confidence
      || (next.confidence === previous.confidence
        && next.disposition === "reuse"
        && previous.disposition === "recover")
    ) {
      decisions.set(toolId, next);
    }
  }
  return [...decisions.values()].sort((left, right) =>
    left.toolId.localeCompare(right.toolId),
  );
}

/**
 * Decodes only the compact, bounded VLM payload already accepted by the
 * bridge. Invalid fields are omitted rather than guessed or coerced.
 */
export function deriveVlmOperationObservations(vlmResult: VLMResult): VlmOperationObservations {
  const parsed = parseBoundedJson(vlmResult.raw_json);
  const payload = parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? parsed as Record<string, unknown>
    : null;
  const uncertainty = normalizeProbability(vlmResult.uncertainty);
  return {
    nextToolProbabilities: sortedToolProbabilities(candidateRows(payload?.tool)),
    mayoDecisions: sortedMayoDecisions(payload?.mayo),
    source: normalizeId(vlmResult.source),
    schemaVersion: normalizeId(vlmResult.schema_version),
    uncertainty,
  };
}

export function getVlmToolEvidence(
  observations: VlmOperationObservations,
  toolId: string,
): ToolVlmEvidence {
  const normalizedToolId = normalizeId(toolId);
  if (!normalizedToolId) {
    return { nextToolProbability: null, nextToolRank: null, mayoDecision: null };
  }
  const nextToolIndex = observations.nextToolProbabilities
    .slice(0, MAX_DISPLAYED_NEXT_TOOL_RANK)
    .findIndex((candidate) => candidate.toolId === normalizedToolId);
  return {
    nextToolProbability: nextToolIndex >= 0
      ? observations.nextToolProbabilities[nextToolIndex]?.confidence ?? null
      : null,
    nextToolRank: nextToolIndex >= 0 ? nextToolIndex + 1 : null,
    mayoDecision:
      observations.mayoDecisions.find((decision) => decision.toolId === normalizedToolId) ?? null,
  };
}

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

/** Compact VLM labels intended to live inside an existing stage tool box. */
export const VlmToolEvidenceBadges = memo(function VlmToolEvidenceBadges({
  evidence,
  observations,
  toolId,
  language,
  showMayoDecision = false,
  nextToolAuthority = "vlm",
  className = "",
}: {
  /** Pass this when the caller already resolved the tool's VLM evidence. */
  evidence?: ToolVlmEvidence;
  /** A single parsed VLM result can be reused for all visible tool boxes. */
  observations?: VlmOperationObservations;
  /** Required together with `observations` when `evidence` is omitted. */
  toolId?: string;
  language: Language;
  /** Set only after the current DT state confirms a canonical Mayo location. */
  showMayoDecision?: boolean;
  /** Stage cards pass reducer-accepted system-final ranks here. */
  nextToolAuthority?: "vlm" | "system";
  className?: string;
}) {
  const resolvedEvidence = evidence ?? (observations && toolId
    ? getVlmToolEvidence(observations, toolId)
    : { nextToolProbability: null, nextToolRank: null, mayoDecision: null });
  const mayoDecision = showMayoDecision ? resolvedEvidence.mayoDecision : null;
  if (resolvedEvidence.nextToolProbability === null && !mayoDecision) return null;
  const recoveryLabel = mayoDecision?.disposition === "recover"
    ? language === "ko" ? "회수 필요" : "Recover"
    : language === "ko" ? "재사용 판단" : "Reuse";
  return (
    <div
      className={`operation-vlm-tool-evidence ${className}`.trim()}
      data-slot="operation-vlm-tool-evidence"
      data-vlm-next-tool={resolvedEvidence.nextToolProbability === null ? "false" : "true"}
      data-vlm-next-tool-rank={resolvedEvidence.nextToolRank ?? ""}
      data-vlm-mayo={mayoDecision?.disposition ?? "none"}
      data-next-tool-authority={nextToolAuthority}
      aria-label={nextToolAuthority === "system"
        ? language === "ko" ? "시스템 최종 다음 도구" : "System-final next tool"
        : language === "ko" ? "VLM 관찰 결과" : "VLM observation"}
    >
      {resolvedEvidence.nextToolProbability !== null ? (
        <span
          className="operation-vlm-tool-badge next"
          data-vlm-evidence="next-tool"
          data-vlm-next-tool-rank={resolvedEvidence.nextToolRank ?? ""}
        >
          <BrainCircuit aria-hidden="true" size={11} strokeWidth={2.2} />
          <span>
            {language === "ko"
              ? `${nextToolAuthority === "system" ? "시스템 " : ""}다음 도구 ${resolvedEvidence.nextToolRank ?? ""}순위`
              : `${nextToolAuthority === "system" ? "System " : ""}next #${resolvedEvidence.nextToolRank ?? ""}`}
          </span>
          <strong>{percent(resolvedEvidence.nextToolProbability)}</strong>
        </span>
      ) : null}
      {mayoDecision ? (
        <span
          className={`operation-vlm-tool-badge mayo ${mayoDecision.disposition}`}
          data-vlm-evidence="mayo-decision"
        >
          <RotateCcw aria-hidden="true" size={11} strokeWidth={2.2} />
          <span>{recoveryLabel}</span>
          <strong>{percent(mayoDecision.confidence)}</strong>
        </span>
      ) : null}
    </div>
  );
});

/**
 * Observer-only notice for an ASR utterance that has reached the final
 * transcript boundary.  It deliberately carries no intent or dispatch claim:
 * a finalized sentence can still be rejected, ambiguous, or unrelated to a
 * control request.
 */
export function SurgeonFinalSentencePopup({
  eventKey,
  text,
  language,
  className = "",
}: {
  eventKey: string;
  text: string;
  language: Language;
  className?: string;
}) {
  const normalizedText = text.trim();
  if (!eventKey || !normalizedText) return null;
  return (
    <section
      className={`operation-surgeon-final-popup ${className}`.trim()}
      data-slot="surgeon-asr-final-popup"
      data-asr-final-key={eventKey}
      role="status"
      aria-live="polite"
      aria-label={language === "ko" ? "집도의 ASR 확정 문장" : "Surgeon ASR final sentence"}
    >
      <MessageSquareText aria-hidden="true" size={18} strokeWidth={2.1} />
      <div>
        <span>{language === "ko" ? "ASR 확정 문장 · 관찰" : "ASR final sentence · observed"}</span>
        <strong title={normalizedText}>{normalizedText}</strong>
        <small>
          {language === "ko"
            ? "이 문장 자체는 액션 또는 서비스 요청이 아닙니다"
            : "This sentence alone is not an Action or Service request"}
        </small>
      </div>
    </section>
  );
}

function dispatchStateCopy(state: ExecutionDispatchState, language: Language): string {
  const ko: Record<ExecutionDispatchState, string> = {
    proposed: "제안만 · 미전송",
    sent: "클라이언트 송신",
    accepted: "서버 수락",
    completed: "완료 확인",
    rejected: "서버 거부",
    failed: "송신 또는 실행 실패",
    unknown: "상태 미확인",
  };
  const en: Record<ExecutionDispatchState, string> = {
    proposed: "Proposed · not sent",
    sent: "Client sent",
    accepted: "Server accepted",
    completed: "Completion confirmed",
    rejected: "Server rejected",
    failed: "Send or execution failed",
    unknown: "State unverified",
  };
  return (language === "ko" ? ko : en)[state];
}

function dispatchIcon(state: ExecutionDispatchState) {
  if (state === "completed" || state === "accepted") return CheckCircle2;
  if (state === "rejected" || state === "failed") return CircleX;
  if (state === "unknown") return CircleAlert;
  return state === "sent" ? Send : RadioTower;
}

function executionTraceState(trace: Pick<ExecutionTrace, "dispatch_submitted" | "stage">): ExecutionDispatchState {
  // Do not use a server stage to imply local transport: a trace that says the
  // browser did not submit the request remains visibly unsent.
  if (!trace.dispatch_submitted) return "proposed";
  switch (trace.stage) {
    case "sent":
      return "sent";
    case "accepted":
      return "accepted";
    case "completed":
      return "completed";
    case "rejected":
      return "rejected";
    case "failed":
    case "canceled":
      return "failed";
    default:
      return "unknown";
  }
}

/**
 * Converts the typed, observer-only execution trace into presentation data.
 * In particular, a Service admission is not labelled a physical completion.
 */
export function executionDispatchEventFromTrace(
  trace: Pick<
    ExecutionTrace,
    | "sequence"
    | "command_id"
    | "route"
    | "transport"
    | "endpoint"
    | "stage"
    | "dispatch_submitted"
    | "evidence"
    | "reason_code"
    | "retraction_command"
    | "retraction_target_side"
    | "retraction_distance_m"
    | "receivedAt"
  >,
  language: Language,
  subject?: {
    toolId?: string;
    toolLabel?: string;
    toolInstanceId?: string;
    sourceLocationId?: string;
    sourceLocationType?: string;
    targetLocationId?: string;
    targetLocationType?: string;
    targetOwner?: string;
  },
): ExecutionDispatchEvent | null {
  const transport = normalizeVlmToken(trace.transport);
  if (transport !== "action" && transport !== "service") return null;
  const stage = normalizeVlmToken(trace.stage);
  const evidence = normalizeVlmToken(trace.evidence);
  const isServiceAdmission = transport === "service"
    && stage === "accepted"
    && evidence === "service_admission_only";
  const detail = isServiceAdmission
    ? language === "ko"
      ? "서비스 접수 확인 · 물리 동작 완료 아님"
      : "Service admission only · not physical completion"
    : normalizeId(trace.reason_code) || normalizeId(trace.evidence);
  // Keep the observed command identifier intact for operators. A normalized
  // copy is used only in the DOM key so a hyphenated ID is not silently
  // rewritten in the visible audit trail.
  const commandId = String(trace.command_id || "").trim().slice(0, 96);
  const commandKey = normalizeId(commandId);
  const toolId = normalizeId(subject?.toolId);
  const toolLabel = String(subject?.toolLabel ?? "").trim().slice(0, 128);
  const toolInstanceId = String(subject?.toolInstanceId ?? "").trim().slice(0, 96);
  const sourceLocationId = normalizeId(subject?.sourceLocationId);
  const sourceLocationType = normalizeId(subject?.sourceLocationType);
  const targetLocationId = normalizeId(subject?.targetLocationId);
  const targetLocationType = normalizeId(subject?.targetLocationType);
  const targetOwner = normalizeId(subject?.targetOwner);
  const endpoint = String(trace.endpoint || "").trim().slice(0, 192);
  const route = String(trace.route || "").trim().slice(0, 48);
  const retractionCommand = Number(trace.retraction_command);
  const retractionTargetSide = Number(trace.retraction_target_side);
  const retractionDistanceM = Number(trace.retraction_distance_m);
  const hasRetractionPayload = normalizeVlmToken(route) === "retraction"
    && Number.isInteger(retractionCommand)
    && retractionCommand >= 1
    && retractionCommand <= 6
    && Number.isInteger(retractionTargetSide)
    && retractionTargetSide >= 0
    && retractionTargetSide <= 2
    && Number.isFinite(retractionDistanceM)
    && retractionDistanceM >= 0;
  const occurredAt = Number.isFinite(trace.receivedAt) && trace.receivedAt > 0
    ? trace.receivedAt
    : Date.now();
  return {
    // One command can produce sent → accepted → completed trace rows; retain
    // the sequence in the visual key so those evidence stages never collapse.
    id: `${commandKey || transport}-${trace.sequence}`,
    kind: transport,
    name: endpoint || route || transport,
    state: executionTraceState({
      dispatch_submitted: Boolean(trace.dispatch_submitted),
      stage,
    }),
    occurredAt,
    sequence: trace.sequence,
    commandId: commandId || undefined,
    toolId: toolId || undefined,
    toolLabel: toolLabel || undefined,
    toolInstanceId: toolInstanceId || undefined,
    sourceLocationId: sourceLocationId || undefined,
    sourceLocationType: sourceLocationType || undefined,
    targetLocationId: targetLocationId || undefined,
    targetLocationType: targetLocationType || undefined,
    targetOwner: targetOwner || undefined,
    route: route || undefined,
    retractionCommand: hasRetractionPayload ? retractionCommand : undefined,
    retractionTargetSide: hasRetractionPayload ? retractionTargetSide : undefined,
    retractionDistanceCm: hasRetractionPayload ? retractionDistanceM * 100 : undefined,
    detail: detail || undefined,
  };
}

function retractionCommandCopy(command: number, language: Language): string {
  const ko: Record<number, string> = {
    1: "직접 교시 시작",
    2: "직접 교시 종료",
    3: "리트랙션 시작",
    4: "리트랙션 조정",
    5: "도구 교체",
    6: "리트랙션 중지",
  };
  const en: Record<number, string> = {
    1: "Start direct teach",
    2: "Finish direct teach",
    3: "Start retraction",
    4: "Adjust retraction",
    5: "Change tool",
    6: "Stop retraction",
  };
  return (language === "ko" ? ko : en)[command]
    ?? (language === "ko" ? "명령 미확인" : "Unknown command");
}

function retractionSideCopy(command: number, side: number, language: Language): string {
  if (side === 1) return language === "ko" ? "왼쪽" : "Left";
  if (side === 2) return language === "ko" ? "오른쪽" : "Right";
  // The V1 wire contract uses TARGET_NONE for two bilateral operations:
  // finishing direct teach on both arms and adjusting both retractors.
  if (side === 0 && (command === 2 || command === 4)) {
    return language === "ko" ? "양쪽" : "Both";
  }
  return language === "ko" ? "없음" : "None";
}

function distanceCmCopy(distanceCm: number): string {
  if (!Number.isFinite(distanceCm) || distanceCm < 0) return "-";
  return `${Number(distanceCm.toFixed(2))} cm`;
}

function RetractionPayload({
  event,
  language,
}: {
  event: ExecutionDispatchEvent;
  language: Language;
}) {
  if (
    event.retractionCommand === undefined
    || event.retractionTargetSide === undefined
    || event.retractionDistanceCm === undefined
  ) {
    return null;
  }
  return (
    <dl
      className="operation-dispatch-retraction-payload"
      data-slot="operation-dispatch-retraction-payload"
    >
      <div>
        <dt>{language === "ko" ? "명령 종류" : "Command"}</dt>
        <dd>{retractionCommandCopy(event.retractionCommand, language)}</dd>
      </div>
      <div>
        <dt>{language === "ko" ? "대상 쪽" : "Target side"}</dt>
        <dd>{retractionSideCopy(event.retractionCommand, event.retractionTargetSide, language)}</dd>
      </div>
      <div>
        <dt>{language === "ko" ? "이동 거리" : "Travel distance"}</dt>
        <dd>{distanceCmCopy(event.retractionDistanceCm)}</dd>
      </div>
    </dl>
  );
}

function dispatchSubjectCopy(event: ExecutionDispatchEvent, language: Language): string | null {
  if (!event.toolLabel) return null;
  const instanceMarker = toolInstanceMarker(event.toolInstanceId, event.toolId);
  const subject = instanceMarker ? `${event.toolLabel} ${instanceMarker}` : event.toolLabel;
  return language === "ko"
    ? `대상 도구 · ${subject}`
    : `Instrument · ${subject}`;
}

function toolInstanceMarker(instanceId: string | undefined, toolId: string | undefined): string {
  const normalized = instanceId?.trim() ?? "";
  if (!normalized || normalized === toolId?.trim()) return "";
  const markerIndex = normalized.lastIndexOf("#");
  if (markerIndex >= 0 && markerIndex < normalized.length - 1) {
    return normalized.slice(markerIndex, markerIndex + 17);
  }
  return normalized.slice(0, 16);
}

function locationTypeCopy(locationType: string | undefined, language: Language): string {
  const normalized = normalizeVlmToken(locationType || "");
  const ko: Record<string, string> = {
    rack: "도구 랙",
    tray: "트레이",
    mayo: "메이요 스탠드",
    mayo_stand: "메이요 스탠드",
    surgeon: "집도의",
    robot: "로봇",
    bed: "수술 베드",
    cleaner: "클리너",
  };
  const en: Record<string, string> = {
    rack: "Tool rack",
    tray: "Tray",
    mayo: "Mayo stand",
    mayo_stand: "Mayo stand",
    surgeon: "Surgeon",
    robot: "Robot",
    bed: "Surgical bed",
    cleaner: "Cleaner",
  };
  const translated = (language === "ko" ? ko : en)[normalized];
  if (translated) return translated;
  return normalized ? normalized.replace(/_/g, " ") : "";
}

function dispatchLocationCopy(
  locationType: string | undefined,
  locationId: string | undefined,
  language: Language,
): string {
  const type = locationTypeCopy(locationType, language);
  const id = normalizeId(locationId);
  // A value such as `surgeon` / `surgeon` carries no extra location detail.
  const sameAsType = normalizeVlmToken(locationType || "") === normalizeVlmToken(id);
  if (type && id && !sameAsType) return `${type} · ${id}`;
  if (type) return type;
  if (id) return id;
  return language === "ko" ? "상태 미수신" : "Status not received";
}

function dispatchTimeCopy(occurredAt: number, language: Language): string {
  if (!Number.isFinite(occurredAt) || occurredAt <= 0) return "--:--:--";
  return new Date(occurredAt).toLocaleTimeString(language === "ko" ? "ko-KR" : "en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function DispatchFlow({
  event,
  language,
}: {
  event: ExecutionDispatchEvent;
  language: Language;
}) {
  const source = dispatchLocationCopy(event.sourceLocationType, event.sourceLocationId, language);
  const target = dispatchLocationCopy(
    event.targetLocationType || event.targetOwner,
    event.targetLocationId,
    language,
  );
  return (
    <div className="operation-dispatch-flow" data-slot="operation-dispatch-tool-flow">
      <div>
        <span>{language === "ko" ? "출발지" : "From"}</span>
        <strong>{source}</strong>
      </div>
      <span className="operation-dispatch-flow-arrow" aria-hidden="true">→</span>
      <div>
        <span>{language === "ko" ? "목적지" : "To"}</span>
        <strong>{target}</strong>
      </div>
    </div>
  );
}

function DispatchMetadata({
  event,
  language,
}: {
  event: ExecutionDispatchEvent;
  language: Language;
}) {
  const route = event.route || (language === "ko" ? "경로 미수신" : "Route not received");
  const commandId = event.commandId || (language === "ko" ? "명령 ID 미수신" : "Command ID not received");
  return (
    <dl className="operation-dispatch-metadata">
      <div>
        <dt>{language === "ko" ? "작업 경로" : "Operation route"}</dt>
        <dd>{route}</dd>
      </div>
      <div>
        <dt>{language === "ko" ? "명령 ID" : "Command ID"}</dt>
        <dd className="operation-dispatch-mono">{commandId}</dd>
      </div>
      <div>
        <dt>{language === "ko" ? "시퀀스" : "Sequence"}</dt>
        <dd>{event.sequence}</dd>
      </div>
      <div>
        <dt>{language === "ko" ? "관측 시각" : "Observed"}</dt>
        <dd>{dispatchTimeCopy(event.occurredAt, language)}</dd>
      </div>
    </dl>
  );
}

/** Gives parents a safe way to select a popup-worthy, actually sent event. */
export function latestActualDispatch(
  events: readonly ExecutionDispatchEvent[],
): ExecutionDispatchEvent | null {
  return [...events]
    // An ``unknown`` result still records a submitted client request. Keep it
    // visible as an uncertainty popup rather than hiding an actual send.
    .filter((event) => event.state !== "proposed")
    .sort((left, right) => right.occurredAt - left.occurredAt)[0] ?? null;
}

function DispatchRow({
  event,
  language,
}: {
  event: ExecutionDispatchEvent;
  language: Language;
}) {
  const Icon = dispatchIcon(event.state);
  const kindLabel = event.kind === "action"
    ? language === "ko" ? "액션" : "Action"
    : language === "ko" ? "서비스" : "Service";
  const subject = dispatchSubjectCopy(event, language);
  return (
    <li
      className={`operation-dispatch-row state-${event.state}`}
      data-dispatch-id={event.id}
      data-dispatch-kind={event.kind}
      data-dispatch-state={event.state}
      data-command-id={event.commandId ?? ""}
      data-tool-instance-id={event.toolInstanceId ?? ""}
      data-retraction-command={event.retractionCommand ?? ""}
      data-retraction-target-side={event.retractionTargetSide ?? ""}
    >
      <Icon aria-hidden="true" size={16} strokeWidth={2.2} />
      <div>
        <span>{kindLabel}</span>
        {subject ? <strong className="operation-dispatch-subject">{subject}</strong> : null}
        {event.retractionCommand === undefined
          ? <DispatchFlow event={event} language={language} />
          : <RetractionPayload event={event} language={language} />}
        <small className="operation-dispatch-endpoint">{event.name}</small>
        {event.detail ? <small>{event.detail}</small> : null}
        <DispatchMetadata event={event} language={language} />
      </div>
      <em>{dispatchStateCopy(event.state, language)}</em>
    </li>
  );
}

/**
 * Observer-only runtime feed. It never creates a dispatch and labels a VLM
 * proposal as unsent until a separate bridge transport event says otherwise.
 */
export function OperationExecutionDispatchFeed({
  events,
  language,
  maxEntries = MAX_DISPLAY_DISPATCHES,
  className = "",
}: {
  events: readonly ExecutionDispatchEvent[];
  language: Language;
  maxEntries?: number;
  className?: string;
}) {
  const visibleEvents = [...events]
    .sort((left, right) => right.occurredAt - left.occurredAt)
    .slice(0, Math.max(1, Math.min(MAX_DISPLAY_DISPATCHES, maxEntries)));
  return (
    <section
      className={`operation-dispatch-feed ${className}`.trim()}
      data-slot="operation-execution-dispatch-feed"
      aria-label={language === "ko" ? "실제 액션과 서비스 송신 추적" : "Action and service dispatch trace"}
    >
      <div className="operation-dispatch-header">
        <div>
          <span>{language === "ko" ? "실제 송신 추적" : "Dispatch trace"}</span>
          <strong>{language === "ko" ? "Action · Service" : "Action · Service"}</strong>
        </div>
        <RadioTower aria-hidden="true" size={18} strokeWidth={2.1} />
      </div>
      {visibleEvents.length ? (
        <ol aria-live="polite">
          {visibleEvents.map((event) => <DispatchRow event={event} key={event.id} language={language} />)}
        </ol>
      ) : (
        <p className="operation-dispatch-empty">
          {language === "ko"
            ? "아직 실제로 송신된 액션 또는 서비스가 없습니다."
            : "No Action or Service has been sent yet."}
        </p>
      )}
    </section>
  );
}

/** A compact, overlay-ready view of the most recent actual dispatch. */
export function OperationExecutionDispatchPopup({
  event,
  language,
  className = "",
}: {
  event: ExecutionDispatchEvent | null;
  language: Language;
  className?: string;
}) {
  if (!event || event.state === "proposed") return null;
  const Icon = dispatchIcon(event.state);
  const kindLabel = event.kind === "action"
    ? language === "ko" ? "액션" : "Action"
    : language === "ko" ? "서비스" : "Service";
  const subject = dispatchSubjectCopy(event, language);
  return (
    <section
      className={`operation-dispatch-popup state-${event.state} ${className}`.trim()}
      data-slot="operation-execution-dispatch-popup"
      data-dispatch-id={event.id}
      data-dispatch-kind={event.kind}
      data-dispatch-state={event.state}
      role="status"
      aria-live="polite"
    >
      <Icon aria-hidden="true" size={19} strokeWidth={2.2} />
      <div>
        <span>{kindLabel} · {dispatchStateCopy(event.state, language)}</span>
        {subject ? <strong className="operation-dispatch-subject">{subject}</strong> : null}
        <small className="operation-dispatch-endpoint">{event.name}</small>
        {event.detail ? <small>{event.detail}</small> : null}
      </div>
    </section>
  );
}
