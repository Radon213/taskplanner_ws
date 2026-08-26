import { useId } from "react";
import { useReducedMotion } from "framer-motion";
import {
  Box,
  CheckCircle2,
  CircleDashed,
  LoaderCircle,
  RotateCcw,
  ServerCog,
  ShieldAlert,
  WifiOff,
} from "lucide-react";

import type { Language } from "../../utils/display";
import "./execution-route-selector.css";

/**
 * Route identifiers deliberately contain no endpoint address. The server is
 * authoritative for resolving these IDs to its allow-listed Action/Service
 * contracts.
 */
export type ExecutionEndpointSource = "external" | "virtual";

export type ExecutionEndpointReadiness = "ready" | "unavailable" | "unknown";

/** Read-only Action/Service discovery for the candidate source. */
export type ExecutionEndpointServerHealth = {
  actionServerReady: boolean;
  retractionServiceReady: boolean;
};

/**
 * Parent-owned transition state. A route selection is not considered ready
 * until the server reports the requested configuration and reset outcome.
 */
export type ExecutionRouteTransitionState =
  | "idle"
  | "switching"
  | "resetting"
  | "ready"
  | "failed";

export type ExecutionRouteSelectorProps = Omit<
  React.ComponentPropsWithoutRef<"section">,
  "children"
> & {
  language: Language;
  /** Server-confirmed source currently used for tool-handover Action dispatch. */
  currentSource: ExecutionEndpointSource | null;
  /** Server-confirmed source currently used for retraction Service dispatch. */
  currentRetractionSource: ExecutionEndpointSource | null;
  /** UI/server-selected Action destination; falls back to `currentSource` when omitted. */
  selectedSource?: ExecutionEndpointSource | null;
  /** UI/server-selected Service destination; falls back to its current source. */
  selectedRetractionSource?: ExecutionEndpointSource | null;
  /** True only after the procedure and its active planner work have stopped. */
  procedureStopped: boolean;
  /** Bridge/control-plane readiness, not physical controller readiness. */
  connected: boolean;
  /** Per-source route-selection availability from the bounded server projection. */
  sourceReadiness?: Readonly<Partial<Record<ExecutionEndpointSource, ExecutionEndpointReadiness>>>;
  /** Supplemental graph health; it never substitutes for the post-switch preflight. */
  sourceHealth?: Readonly<
    Partial<Record<ExecutionEndpointSource, ExecutionEndpointServerHealth>>
  >;
  transitionState?: ExecutionRouteTransitionState;
  /** Safe, operator-facing detail returned by the parent/server; never an endpoint URL. */
  transitionMessage?: string;
  /** Additional parent authority lock, such as an outstanding Action or Service request. */
  disabledReason?: string;
  /** Called only after all browser-side stop/readiness gates pass. */
  onSourcesChange: (sources: {
    toolHandoverSource: ExecutionEndpointSource;
    retractionSource: ExecutionEndpointSource;
  }) => void;
};

type SourceOption = {
  id: ExecutionEndpointSource;
  icon: typeof ServerCog;
  label: readonly [string, string];
  detail: readonly [string, string];
};

const SOURCE_OPTIONS: readonly SourceOption[] = [
  {
    id: "external",
    icon: ServerCog,
    label: ["실제 통합 서버", "External integration"],
    detail: ["외부 Action·Service 계약 경로", "External Action/Service contract route"],
  },
  {
    id: "virtual",
    icon: Box,
    label: ["가상 실행 서버", "Virtual execution"],
    detail: ["DT 시연 경로 · 실물 제어 없음", "DT demonstration route · no physical control"],
  },
];

const MAX_OPERATOR_MESSAGE_CHARS = 360;

/** Keep route diagnostics useful without disclosing a raw server address in the UI. */
function safeOperatorMessage(value: string): string {
  return value
    .trim()
    .replace(/\b(?:https?|wss?):\/\/[^\s]+/gi, "[endpoint hidden]")
    .replace(/\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?:\/[^\s]*)?/g, "[endpoint hidden]")
    .slice(0, MAX_OPERATOR_MESSAGE_CHARS);
}

function localized<T>(value: readonly [T, T], language: Language): T {
  return value[language === "ko" ? 0 : 1];
}

function sourceLabel(source: ExecutionEndpointSource | null, language: Language): string {
  if (!source) return language === "ko" ? "상태 수신 대기" : "Awaiting route status";
  return localized(
    source === "virtual"
      ? ["가상 실행 서버", "Virtual execution"]
      : ["실제 통합 서버", "External integration"],
    language,
  );
}

function readinessLabel(readiness: ExecutionEndpointReadiness, language: Language): string {
  if (readiness === "ready") return language === "ko" ? "전환 가능" : "Selectable";
  if (readiness === "unavailable") return language === "ko" ? "전환 불가" : "Unavailable";
  return language === "ko" ? "상태 대기" : "Status pending";
}

function sourceHealthLabel(
  health: ExecutionEndpointServerHealth | undefined,
  language: Language,
): string {
  if (!health) {
    return language === "ko"
      ? "Action · Service 상태 대기"
      : "Action · Service status pending";
  }
  if (health.actionServerReady && health.retractionServiceReady) {
    return language === "ko"
      ? "Action · Service 발견됨"
      : "Action · Service discovered";
  }
  if (!health.actionServerReady && !health.retractionServiceReady) {
    return language === "ko"
      ? "Action · Service 모두 대기"
      : "Action · Service both pending";
  }
  return language === "ko"
    ? health.actionServerReady
      ? "Action 발견 · Service 대기"
      : "Action 대기 · Service 발견"
    : health.actionServerReady
      ? "Action discovered · Service pending"
      : "Action pending · Service discovered";
}

function transitionCopy(
  state: ExecutionRouteTransitionState,
  language: Language,
  message: string,
): string {
  if (message) return message;
  if (state === "switching") {
    return language === "ko"
      ? "선택한 실행 서버를 적용하는 중입니다."
      : "Applying the selected execution server.";
  }
  if (state === "resetting") {
    return language === "ko"
      ? "수술 실행 및 디지털 트윈 상태를 초기화하는 중입니다."
      : "Resetting procedure execution and digital-twin state.";
  }
  if (state === "ready") {
    return language === "ko"
      ? "경로 변경과 초기화가 완료되었습니다. 새 통합 시작 점검을 확인한 뒤 시작하세요."
      : "Route change and reset completed. Verify a fresh integration start check before starting.";
  }
  if (state === "failed") {
    return language === "ko"
      ? "경로 변경 또는 초기화가 완료되지 않았습니다. 현재 적용 경로를 확인하세요."
      : "The route change or reset did not finish. Verify the currently applied route.";
  }
  return language === "ko"
    ? "선택하면 현재 수술 실행과 디지털 트윈 진행 상태가 초기화됩니다."
    : "Selecting a route resets the current procedure execution and digital-twin progress.";
}

function lockReason(
  language: Language,
  procedureStopped: boolean,
  connected: boolean,
  transitionState: ExecutionRouteTransitionState,
  disabledReason: string,
): string {
  if (!procedureStopped) {
    return language === "ko"
      ? "수술 실행과 활성 작업이 완전히 정지된 뒤에만 서버를 바꿀 수 있습니다."
      : "Stop procedure execution and all active work before changing servers.";
  }
  if (!connected) {
    return language === "ko"
      ? "운영 연결 상태를 확인한 뒤 서버를 바꿀 수 있습니다."
      : "Verify the operational connection before changing servers.";
  }
  if (transitionState === "switching" || transitionState === "resetting") {
    return language === "ko"
      ? "현재 경로 변경과 초기화 결과를 확인하는 중입니다."
      : "Waiting for the current route change and reset result.";
  }
  return disabledReason;
}

/**
 * Controlled operating-screen selector for the allow-listed external/virtual
 * Action and Service routes. It owns no endpoint URLs and never performs a
 * route change itself; the parent must issue and confirm the server request.
 */
export function ExecutionRouteSelector({
  language,
  currentSource,
  currentRetractionSource,
  selectedSource,
  selectedRetractionSource,
  procedureStopped,
  connected,
  sourceReadiness = {},
  sourceHealth = {},
  transitionState = "idle",
  transitionMessage = "",
  disabledReason = "",
  onSourcesChange,
  className,
  ...sectionProps
}: ExecutionRouteSelectorProps) {
  const reducedMotion = useReducedMotion();
  const lockNoteId = useId();
  const selectionHelpId = useId();
  const selectedTool = selectedSource ?? currentSource;
  const selectedRetraction = selectedRetractionSource ?? currentRetractionSource;
  const isTransitioning = transitionState === "switching" || transitionState === "resetting";
  const normalizedDisabledReason = safeOperatorMessage(disabledReason);
  const selectionLockReason = lockReason(
    language,
    procedureStopped,
    connected,
    transitionState,
    normalizedDisabledReason,
  );
  const statusCopy = transitionCopy(
    transitionState,
    language,
    safeOperatorMessage(transitionMessage),
  );
  const noRouteReady = SOURCE_OPTIONS.every(
    (option) => (sourceReadiness[option.id] ?? "unknown") !== "ready",
  );
  const sourceOptions = (
    kind: "tool" | "retraction",
    selected: ExecutionEndpointSource | null,
  ) => (
    <div
      aria-describedby={selectionHelpId}
      aria-label={
        kind === "tool"
          ? language === "ko" ? "도구 전달 Action 서버 선택" : "Tool-handover Action server selection"
          : language === "ko" ? "리트랙션 Service 서버 선택" : "Retraction Service server selection"
      }
      className="execution-route-selector-operation"
      role="group"
    >
      <strong className="execution-route-selector-operation-title">
        {kind === "tool"
          ? language === "ko" ? "도구 전달 Action" : "Tool-handover Action"
          : language === "ko" ? "리트랙션 Service" : "Retraction Service"}
      </strong>
      <div className="execution-route-selector-options">
        {SOURCE_OPTIONS.map((option) => {
          const Icon = option.icon;
          const readiness = sourceReadiness[option.id] ?? "unknown";
          const health = sourceHealth[option.id];
          const optionSelected = selected === option.id;
          const optionUnavailable = readiness !== "ready";
          const optionDisabled = Boolean(selectionLockReason) || optionUnavailable || optionSelected;
          const readinessId = `${selectionHelpId}-${kind}-${option.id}`;
          const nextSources = kind === "tool"
            ? { toolHandoverSource: option.id, retractionSource: selectedRetraction ?? option.id }
            : { toolHandoverSource: selectedTool ?? option.id, retractionSource: option.id };
          return (
            <button
              aria-describedby={[selectionHelpId, readinessId, selectionLockReason ? lockNoteId : ""].filter(Boolean).join(" ")}
              aria-pressed={optionSelected}
              className={["execution-route-selector-option", optionSelected ? "selected" : ""].filter(Boolean).join(" ")}
              data-health={!health ? "unknown" : health.actionServerReady && health.retractionServiceReady ? "ready" : "partial"}
              data-readiness={readiness}
              data-source={option.id}
              disabled={optionDisabled}
              key={option.id}
              onClick={() => {
                if (!optionDisabled) onSourcesChange(nextSources);
              }}
              type="button"
            >
              <Icon aria-hidden="true" size={17} />
              <span>
                <strong>{localized(option.label, language)}</strong>
                <small>{kind === "tool"
                  ? language === "ko" ? "Action 서버" : "Action server"
                  : language === "ko" ? "Service 서버" : "Service server"}</small>
                <small className="execution-route-selector-health">
                  {sourceHealthLabel(health, language)}
                </small>
              </span>
              <em id={readinessId}>{readinessLabel(readiness, language)}</em>
            </button>
          );
        })}
      </div>
    </div>
  );

  return (
    <section
      {...sectionProps}
      aria-busy={isTransitioning}
      className={["execution-route-selector", className].filter(Boolean).join(" ")}
      data-motion={reducedMotion ? "reduced" : "full"}
      data-slot="execution-route-selector"
      data-transition-state={transitionState}
    >
      <div className="execution-route-selector-header">
        <div>
          <span>{language === "ko" ? "실행 서버" : "Execution server"}</span>
          <strong>{language === "ko" ? "Action · Service 독립 경로" : "Independent Action · Service routes"}</strong>
        </div>
        {isTransitioning ? (
          <LoaderCircle aria-hidden="true" className="execution-route-selector-spinner" size={18} />
        ) : transitionState === "ready" ? (
          <CheckCircle2 aria-hidden="true" size={18} />
        ) : transitionState === "failed" ? (
          <ShieldAlert aria-hidden="true" size={18} />
        ) : (
          <ServerCog aria-hidden="true" size={18} />
        )}
      </div>

      <dl className="execution-route-selector-summary">
        <div>
          <dt>{language === "ko" ? "Action 현재" : "Action applied"}</dt>
          <dd>{sourceLabel(currentSource, language)}</dd>
        </div>
        <div>
          <dt>{language === "ko" ? "Service 현재" : "Service applied"}</dt>
          <dd>{sourceLabel(currentRetractionSource, language)}</dd>
        </div>
        <div>
          <dt>{language === "ko" ? "Action 선택" : "Action selected"}</dt>
          <dd>{sourceLabel(selectedTool, language)}</dd>
        </div>
        <div>
          <dt>{language === "ko" ? "Service 선택" : "Service selected"}</dt>
          <dd>{sourceLabel(selectedRetraction, language)}</dd>
        </div>
      </dl>
      {sourceOptions("tool", selectedTool)}
      {sourceOptions("retraction", selectedRetraction)}

      <div className="execution-route-selector-reset-note" id={selectionHelpId} role="note">
        <RotateCcw aria-hidden="true" size={16} />
        <span>
          {language === "ko"
            ? "서버를 바꾸면 현재 수술 실행과 디지털 트윈 진행 상태가 초기화됩니다."
            : "Changing the server resets the current procedure execution and digital-twin progress."}
        </span>
      </div>

      {selectionLockReason ? (
        <div className="execution-route-selector-lock" id={lockNoteId} role="status">
          {!connected ? <WifiOff aria-hidden="true" size={16} /> : <ShieldAlert aria-hidden="true" size={16} />}
          <span>{selectionLockReason}</span>
        </div>
      ) : noRouteReady ? (
        <div className="execution-route-selector-lock" id={lockNoteId} role="status">
          <CircleDashed aria-hidden="true" size={16} />
          <span>
            {language === "ko"
              ? "선택 가능한 실행 서버의 준비 상태를 기다리는 중입니다."
              : "Waiting for an execution server to become ready."}
          </span>
        </div>
      ) : null}

      <div
        aria-atomic="true"
        aria-live={transitionState === "failed" ? "assertive" : "polite"}
        className={["execution-route-selector-feedback", transitionState].join(" ")}
        role={transitionState === "failed" ? "alert" : "status"}
      >
        {statusCopy}
      </div>
    </section>
  );
}
