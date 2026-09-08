import type {
  ExecutionTrace,
  LiveAsrFinal,
  SkillStatus,
} from "../types";
import type { Language } from "../utils/display";

/**
 * The operation presentation owner converts already-normalized transport and
 * ASR observations into compact UI facts.  It does not admit, dispatch, or
 * mutate any ROS state.
 */

export type OperationDispatchKind = "action" | "service";

export type OperationDispatchState =
  | "proposed"
  | "sent"
  | "accepted"
  | "completed"
  | "rejected"
  | "failed"
  | "unknown";

export type OperationPresentationFact = {
  label: string;
  value: string;
  valueStyle?: "mono";
};

export type OperationToolFlowPresentation = {
  from: OperationPresentationFact;
  to: OperationPresentationFact;
};

/**
 * Presentation-only copy for a tool-handover Action.  The transport bridge
 * remains authoritative for its state; this simply gives the observer a
 * stable, localized way to render the three visible legs of that one Action.
 */
export type OperationToolHandoverPresentation = {
  title: string;
  pipelineLabel: string;
  carrier: OperationPresentationFact;
};

export type OperationRetractionPresentation = {
  command: number;
  targetSide: number;
  distanceCm: number;
  facts: readonly OperationPresentationFact[];
};

export type OperationDispatchPresentation = {
  id: string;
  kind: OperationDispatchKind;
  state: OperationDispatchState;
  kindLabel: string;
  title: string;
  stateLabel: string;
  occurredAt: number;
  sequence: number;
  commandId?: string;
  toolId?: string;
  toolInstanceId?: string;
  subject?: string;
  endpoint: string;
  detail?: string;
  toolFlow: OperationToolFlowPresentation | null;
  toolHandover: OperationToolHandoverPresentation | null;
  retraction: OperationRetractionPresentation | null;
  metadata: readonly OperationPresentationFact[];
};

export type OperationAsrFinalPresentation = {
  eventKey: string;
  text: string;
  heading: string;
  ariaLabel: string;
};

export type OperationPresentation = {
  dispatches: readonly OperationDispatchPresentation[];
  latestDispatch: OperationDispatchPresentation | null;
  asrFinal: OperationAsrFinalPresentation | null;
};

export type OperationPresentationInput = {
  executionTraces: readonly ExecutionTrace[];
  skillStatusByCommand: Readonly<Record<string, SkillStatus>>;
  asrFinals: readonly LiveAsrFinal[];
  language: Language;
  displayToolName: (toolId: string) => string;
};

export type OperationDispatchFeedCopy = {
  ariaLabel: string;
  heading: string;
  title: string;
  empty: string;
};

/**
 * A browser-local view of the non-retained execution-trace stream.
 *
 * The public trace topic deliberately has no retained history. Keeping an
 * explicit feed epoch gives the transport hook one small, testable way to
 * discard presentation-only history when its connection or owner changes.
 */
export type OperationTraceFeed = {
  epoch: number;
  highestSequence: number;
  traces: readonly ExecutionTrace[];
};

const MAX_OPERATION_TRACE_HISTORY = 32;

export function emptyOperationTraceFeed(): OperationTraceFeed {
  return {
    epoch: 0,
    highestSequence: 0,
    traces: [],
  };
}

/** Start a fresh display epoch without changing any ROS/controller state. */
export function resetOperationTraceFeed(
  feed: OperationTraceFeed,
): OperationTraceFeed {
  return {
    epoch: feed.epoch + 1,
    highestSequence: 0,
    traces: [],
  };
}

/**
 * Keep the trace projection bounded and monotonic within one producer epoch.
 * A bridge owner starts its public sequence at one, so seeing that boundary
 * after an established stream is the browser-side restart signal the
 * non-retained contract exposes. It replaces, rather than mixes with, the
 * previous owner's Action/Service notices.
 */
export function appendOperationTrace(
  feed: OperationTraceFeed,
  trace: ExecutionTrace,
): OperationTraceFeed {
  const sequence = trace.sequence;
  if (!Number.isSafeInteger(sequence) || sequence < 1) return feed;

  const isDuplicate = feed.traces.some((entry) =>
    entry.sequence === sequence
    && entry.command_id === trace.command_id
    && entry.stage === trace.stage
    && entry.transport === trace.transport,
  );
  if (isDuplicate) return feed;

  // A running bridge emits sequence 1 only once.  If a different sequence-1
  // notice reaches an already populated feed, it is the new bridge owner
  // after an execution restart, even when the previous owner emitted only one
  // trace.  Do not mix its current action with the old browser-local card.
  if (sequence === 1 && feed.highestSequence >= 1) {
    return {
      epoch: feed.epoch + 1,
      highestSequence: 1,
      traces: [trace],
    };
  }

  // The execution bridge owns a strictly increasing, single-producer
  // sequence. An older/doubled websocket delivery must not resurrect an old
  // operation card in the current display epoch.
  if (sequence <= feed.highestSequence) return feed;

  return {
    ...feed,
    highestSequence: sequence,
    traces: [trace, ...feed.traces].slice(0, MAX_OPERATION_TRACE_HISTORY),
  };
}

const RETRACTION_COMMAND_NAMES = {
  ko: {
    1: "직접 교시 시작",
    2: "직접 교시 종료",
    3: "리트랙션 시작",
    4: "리트랙션 조정",
    5: "도구 교체",
    6: "리트랙션 중지",
    7: "석션 준비",
    8: "석션 제거",
  },
  en: {
    1: "Start direct teach",
    2: "Finish direct teach",
    3: "Start retraction",
    4: "Adjust retraction",
    5: "Change tool",
    6: "Stop retraction",
    7: "Prepare suction",
    8: "Remove suction",
  },
} as const;

function normalizedToken(value: unknown): string {
  return String(value ?? "").trim().toLocaleLowerCase().replace(/[\s-]+/g, "_");
}

function normalizedId(value: unknown, maxLength = 96): string {
  return typeof value === "string"
    ? value.trim().replace(/[\s-]+/g, "_").slice(0, maxLength)
    : "";
}

function boundedText(value: unknown, maxLength: number): string {
  return typeof value === "string" ? value.trim().slice(0, maxLength) : "";
}

function dispatchStateFor(
  trace: Pick<ExecutionTrace, "dispatch_submitted" | "stage">,
): OperationDispatchState {
  // A transport observer may receive a terminal server stage without the
  // local client submitting it. Keep that distinction visible to the operator.
  if (!trace.dispatch_submitted) return "proposed";
  switch (normalizedToken(trace.stage)) {
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
    case "cancelled":
      return "failed";
    default:
      return "unknown";
  }
}

function dispatchStateLabel(state: OperationDispatchState, language: Language): string {
  const ko: Record<OperationDispatchState, string> = {
    proposed: "제안만 · 미전송",
    sent: "클라이언트 송신",
    accepted: "서버 수락",
    completed: "완료 확인",
    rejected: "서버 거부",
    failed: "송신 또는 실행 실패",
    unknown: "상태 미확인",
  };
  const en: Record<OperationDispatchState, string> = {
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

function kindLabel(kind: OperationDispatchKind, language: Language): string {
  if (kind === "action") return language === "ko" ? "액션" : "Action";
  return language === "ko" ? "서비스" : "Service";
}

function commandName(command: number, language: Language): string {
  const names = language === "ko" ? RETRACTION_COMMAND_NAMES.ko : RETRACTION_COMMAND_NAMES.en;
  return names[command as keyof typeof names]
    ?? (language === "ko" ? "명령 미확인" : "Unknown command");
}

function distanceCmText(
  command: number,
  distanceCm: number,
  language: Language,
): string {
  const magnitude = `${Number(Math.abs(distanceCm).toFixed(2))} cm`;
  if (command !== 4 || distanceCm === 0) return magnitude;
  if (language === "ko") {
    return distanceCm < 0 ? `${magnitude} 덜 당기기` : `${magnitude} 더 당기기`;
  }
  return distanceCm < 0 ? `${magnitude} less pull` : `${magnitude} more pull`;
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

function locationTypeLabel(locationType: string | undefined, language: Language): string {
  const normalized = normalizedToken(locationType);
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
  return translated ?? (normalized ? normalized.replace(/_/g, " ") : "");
}

function locationLabel(
  locationType: string | undefined,
  locationId: string | undefined,
  language: Language,
): string {
  const type = locationTypeLabel(locationType, language);
  const id = normalizedId(locationId);
  const sameAsType = normalizedToken(locationType) === normalizedToken(id);
  if (type && id && !sameAsType) return `${type} · ${id}`;
  if (type) return type;
  if (id) return id;
  return language === "ko" ? "상태 미수신" : "Status not received";
}

function observedAtLabel(occurredAt: number, language: Language): string {
  if (!Number.isFinite(occurredAt) || occurredAt <= 0) return "--:--:--";
  return new Date(occurredAt).toLocaleTimeString(language === "ko" ? "ko-KR" : "en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function retractionFor(
  trace: Pick<
    ExecutionTrace,
    "route" | "retraction_command" | "retraction_target_side" | "retraction_distance_m"
  >,
  language: Language,
): OperationRetractionPresentation | null {
  const command = Number(trace.retraction_command);
  const targetSide = Number(trace.retraction_target_side);
  const distanceM = Number(trace.retraction_distance_m);
  const valid = normalizedToken(trace.route) === "retraction"
    && Number.isInteger(command)
    && command >= 1
    && command <= 8
    && Number.isInteger(targetSide)
    && targetSide >= 0
    && targetSide <= 3
    && Number.isFinite(distanceM)
    && Math.abs(distanceM) <= 1;
  if (!valid) return null;
  const distanceCm = distanceM * 100;
  return {
    command,
    targetSide,
    distanceCm,
    facts: [
      { label: language === "ko" ? "명령" : "Command", value: `${command} · ${commandName(command, language)}` },
      { label: "target_side", value: String(targetSide) },
      { label: language === "ko" ? "거리" : "Distance", value: distanceCmText(command, distanceCm, language) },
    ],
  };
}

function detailFor(
  trace: Pick<ExecutionTrace, "transport" | "stage" | "endpoint" | "endpoint_source" | "evidence" | "reason_code">,
  language: Language,
): string {
  const transport = normalizedToken(trace.transport);
  const stage = normalizedToken(trace.stage);
  const endpoint = boundedText(trace.endpoint, 192);
  const endpointSource = normalizedToken(trace.endpoint_source);
  const evidence = normalizedToken(trace.evidence);
  const reasonCode = normalizedId(trace.reason_code, 128);
  const virtualCompletion = transport === "service"
    && stage === "completed"
    && (
      endpointSource === "virtual"
      || endpoint.startsWith("/integration/virtual/")
      || evidence === "virtual_service_transaction_completed"
      || reasonCode === "virtual_service_completed"
    );
  if (virtualCompletion) return language === "ko" ? "가상 Service 처리 완료" : "Virtual Service completed";
  if (transport === "service" && stage === "accepted") {
    return language === "ko" ? "서비스 접수 확인" : "Service receipt confirmed";
  }
  return reasonCode || normalizedId(trace.evidence, 128);
}

function subjectFor(
  toolId: string,
  toolLabel: string,
  toolInstanceId: string,
  language: Language,
): string | undefined {
  if (!toolLabel) return undefined;
  const instanceMarker = toolInstanceMarker(toolInstanceId, toolId);
  const subject = instanceMarker ? `${toolLabel} ${instanceMarker}` : toolLabel;
  return language === "ko" ? `대상 도구 · ${subject}` : `Instrument · ${subject}`;
}

function flowFor(
  kind: OperationDispatchKind,
  subject: Pick<
    SkillStatus,
    | "source_location_id"
    | "source_location_type"
    | "target_location_id"
    | "target_location_type"
    | "target_owner"
  > | undefined,
  language: Language,
): OperationToolFlowPresentation | null {
  if (
    kind !== "action"
    || !subject
    || !(
      subject.source_location_id
      || subject.source_location_type
      || subject.target_location_id
      || subject.target_location_type
      || subject.target_owner
    )
  ) {
    return null;
  }
  return {
    from: {
      label: language === "ko" ? "출발지" : "From",
      value: locationLabel(subject.source_location_type, subject.source_location_id, language),
    },
    to: {
      label: language === "ko" ? "목적지" : "To",
      value: locationLabel(
        subject.target_location_type || subject.target_owner,
        subject.target_location_id,
        language,
      ),
    },
  };
}

function toolHandoverFor(
  kind: OperationDispatchKind,
  trace: Pick<ExecutionTrace, "route">,
  status: Pick<SkillStatus, "action"> | undefined,
  language: Language,
): OperationToolHandoverPresentation | null {
  const route = normalizedToken(trace.route);
  const action = normalizedToken(status?.action);
  // A route is the normal production signal. The status action also covers
  // endpoint implementations that expose a more specific handover verb.
  if (
    kind !== "action"
    || (route !== "tool_transfer" && route !== "tool_handover" && !action.includes("handover"))
  ) {
    return null;
  }
  return language === "ko"
    ? {
      title: "도구 전달",
      pipelineLabel: "전체 전달 파이프라인",
      carrier: { label: "이송", value: "휴머노이드" },
    }
    : {
      title: "Tool handover",
      pipelineLabel: "Full delivery pipeline",
      carrier: { label: "Transit", value: "Humanoid" },
    };
}

function dispatchFromTrace(
  trace: ExecutionTrace,
  skillStatusByCommand: Readonly<Record<string, SkillStatus>>,
  language: Language,
  displayToolName: (toolId: string) => string,
): OperationDispatchPresentation | null {
  const transport = normalizedToken(trace.transport);
  if (transport !== "action" && transport !== "service") return null;
  const kind: OperationDispatchKind = transport;
  const commandId = boundedText(trace.command_id, 96);
  const status = commandId ? skillStatusByCommand[commandId] : undefined;
  const toolId = boundedText(status?.instrument_id, 96);
  const toolLabel = toolId ? boundedText(displayToolName(toolId), 128) : "";
  const toolInstanceId = boundedText(status?.instrument_instance_id, 96);
  const state = dispatchStateFor(trace);
  const endpoint = boundedText(trace.endpoint, 192)
    || boundedText(trace.route, 48)
    || kind;
  const route = boundedText(trace.route, 48);
  const occurredAt = Number.isFinite(trace.receivedAt) && trace.receivedAt > 0
    ? trace.receivedAt
    : Date.now();
  const retraction = retractionFor(trace, language);
  const detail = detailFor(trace, language);
  const toolHandover = toolHandoverFor(kind, trace, status, language);
  return {
    id: `${normalizedId(commandId) || kind}-${trace.sequence}`,
    kind,
    state,
    kindLabel: kindLabel(kind, language),
    title: `${kindLabel(kind, language)} · ${dispatchStateLabel(state, language)}`,
    stateLabel: dispatchStateLabel(state, language),
    occurredAt,
    sequence: trace.sequence,
    commandId: commandId || undefined,
    toolId: toolId || undefined,
    toolInstanceId: toolInstanceId || undefined,
    subject: subjectFor(toolId, toolLabel, toolInstanceId, language),
    endpoint,
    detail: detail || undefined,
    toolFlow: flowFor(kind, status, language),
    toolHandover,
    retraction,
    metadata: [
      {
        label: language === "ko" ? "작업 경로" : "Operation route",
        value: route || (language === "ko" ? "경로 미수신" : "Route not received"),
      },
      {
        label: language === "ko" ? "명령 ID" : "Command ID",
        value: commandId || (language === "ko" ? "명령 ID 미수신" : "Command ID not received"),
        valueStyle: "mono",
      },
      { label: language === "ko" ? "시퀀스" : "Sequence", value: String(trace.sequence) },
      { label: language === "ko" ? "관측 시각" : "Observed", value: observedAtLabel(occurredAt, language) },
    ],
  };
}

export function latestActualOperationDispatch(
  dispatches: readonly OperationDispatchPresentation[],
): OperationDispatchPresentation | null {
  return [...dispatches]
    .filter((dispatch) => dispatch.state !== "proposed")
    .sort((left, right) => right.occurredAt - left.occurredAt)[0] ?? null;
}

export function latestAsrFinalPresentation(
  finals: readonly LiveAsrFinal[],
  language: Language,
): OperationAsrFinalPresentation | null {
  const final = finals[finals.length - 1];
  const text = final?.text.trim() ?? "";
  if (!text) return null;
  const stamp = final?.stamp.trim() ?? "";
  return {
    eventKey: stamp ? `asr:${stamp}:${text}` : `asr-text:${text}`,
    text,
    heading: language === "ko" ? "ASR 확정 문장 · 관찰" : "ASR final sentence · observed",
    ariaLabel: language === "ko" ? "집도의 ASR 확정 문장" : "Surgeon ASR final sentence",
  };
}

export function operationDispatchFeedCopy(language: Language): OperationDispatchFeedCopy {
  return language === "ko"
    ? {
      ariaLabel: "실제 액션과 서비스 송신 추적",
      heading: "실제 송신 추적",
      title: "Action · Service",
      empty: "아직 실제로 송신된 액션 또는 서비스가 없습니다.",
    }
    : {
      ariaLabel: "Action and service dispatch trace",
      heading: "Dispatch trace",
      title: "Action · Service",
      empty: "No Action or Service has been sent yet.",
    };
}

export function deriveOperationPresentation({
  executionTraces,
  skillStatusByCommand,
  asrFinals,
  language,
  displayToolName,
}: OperationPresentationInput): OperationPresentation {
  const dispatches = executionTraces.flatMap((trace) => {
    const dispatch = dispatchFromTrace(
      trace,
      skillStatusByCommand,
      language,
      displayToolName,
    );
    return dispatch ? [dispatch] : [];
  });
  return {
    dispatches,
    latestDispatch: latestActualOperationDispatch(dispatches),
    asrFinal: latestAsrFinalPresentation(asrFinals, language),
  };
}
