import { type CSSProperties, useEffect, useState } from "react";
import {
  CheckCircle2,
  CircleDotDashed,
  CircleHelp,
  Clock3,
  Eye,
  EyeOff,
  LoaderCircle,
  MapPin,
  Power,
} from "lucide-react";

import {
  isExchangeableToolBelief,
  type ToolBeliefRuntime,
  type ToolBeliefStatus,
  type TrackedToolBelief,
} from "../../ros/toolBeliefMessages";
import type { Language } from "../../utils/display";
import { aggregateToolBeliefPanelRows } from "./toolBeliefPanelRows";
import "./tool-belief-panel.css";

type ToolBeliefPanelProps = {
  runtime: ToolBeliefRuntime | null;
  language: Language;
};

type ControlFeedback = {
  tone: "success" | "error";
  text: string;
};

// The tracker normally publishes every 200 ms; this still allows ample jitter.
const TOOL_BELIEF_STALE_AFTER_MS = 1_500;

const STATUS_ICONS = {
  confirmed: CheckCircle2,
  probable: CircleDotDashed,
  uncertain: CircleHelp,
} satisfies Record<ToolBeliefStatus, typeof CheckCircle2>;

const LOCATION_LABELS: Record<string, { ko: string; en: string }> = {
  tray: { ko: "도구 트레이", en: "Instrument tray" },
  mayo: { ko: "메이요 스탠드", en: "Mayo stand" },
  field: { ko: "수술 필드", en: "Surgical field" },
  surgeon: { ko: "집도의", en: "Surgeon" },
  robot: { ko: "로봇 파지", en: "Robot grasp" },
  cleaner: { ko: "세척 구역", en: "Cleaner" },
  unknown: { ko: "위치 불명", en: "Unknown" },
};

const STATUS_LABELS: Record<ToolBeliefStatus, { ko: string; en: string }> = {
  confirmed: { ko: "확정", en: "Confirmed" },
  probable: { ko: "유력", en: "Probable" },
  uncertain: { ko: "불확실", en: "Uncertain" },
};

function locationLabel(locationId: string, language: Language): string {
  const label = LOCATION_LABELS[locationId];
  return label ? label[language] : locationId || (language === "ko" ? "위치 불명" : "Unknown");
}

function probabilityPercent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value * 100)));
}

function evidenceAgeLabel(ageSec: number | null, language: Language): string {
  if (ageSec === null) return language === "ko" ? "직접 관측 없음" : "No direct observation";
  if (ageSec < 0.5) return language === "ko" ? "방금 관측" : "Observed just now";
  if (ageSec < 10) {
    return language === "ko"
      ? `${ageSec.toFixed(1)}초 전`
      : `${ageSec.toFixed(1)}s ago`;
  }
  if (ageSec < 60) {
    return language === "ko"
      ? `${Math.round(ageSec)}초 전`
      : `${Math.round(ageSec)}s ago`;
  }
  const minutes = Math.floor(ageSec / 60);
  return language === "ko" ? `${minutes}분 전` : `${minutes}m ago`;
}

function trackerDelayLabel(ageSec: number, language: Language): string {
  const duration = ageSec < 10 ? ageSec.toFixed(1) : String(Math.round(ageSec));
  return language === "ko"
    ? `추적기 갱신 지연 · ${duration}초 전 자료`
    : `Tracker stale · data is ${duration}s old`;
}

function evidenceSourceLabel(source: string): string {
  const normalized = source.toLocaleLowerCase().replace(/-/g, "_");
  if (normalized.includes("cam_3") || normalized.includes("cam3")) return "CAM3";
  if (normalized.includes("cam_4") || normalized.includes("cam4")) return "CAM4";
  if (normalized.includes("action")) return "Action";
  if (normalized.includes("skill")) return "Skill";
  if (normalized.includes("scenario") || normalized.includes("initial")) return "Scenario";
  return source;
}

function ToolBeliefRow({
  tool,
  exchangeable,
  quantity,
  language,
  snapshotAgeSec,
}: {
  tool: TrackedToolBelief;
  exchangeable: boolean;
  quantity: number;
  language: Language;
  snapshotAgeSec: number;
}) {
  const StatusIcon = STATUS_ICONS[tool.status];
  const probability = probabilityPercent(tool.mostLikelyProbability);
  const alternativeLocations = tool.locations
    .filter(({ locationId }) => locationId !== tool.mostLikelyLocationId)
    .slice(0, 2);
  const sourceLabels = tool.evidenceSources
    .map(evidenceSourceLabel)
    .filter((source, index, sources) => sources.indexOf(source) === index)
    .slice(0, 3);
  const effectiveEvidenceAgeSec = tool.lastPositiveAgeSec === null
    ? null
    : tool.lastPositiveAgeSec + snapshotAgeSec;
  return (
    <tr
      data-belief-status={tool.status}
      data-tool-track-id={exchangeable ? undefined : tool.trackId}
    >
      <th scope="row">
        <div className="tool-belief-tool-identity">
          <strong>{tool.displayName}</strong>
          <span>
            {tool.instrumentId}
            {exchangeable
              ? quantity > 1 ? ` · ${language === "ko" ? `${quantity}개` : `×${quantity}`}` : ""
              : ` · ${tool.instanceId}`}
          </span>
        </div>
      </th>
      <td>
        <div className="tool-belief-primary-location">
          <span>
            <MapPin aria-hidden="true" size={13} strokeWidth={2.2} />
            {locationLabel(tool.mostLikelyLocationId, language)}
          </span>
          <strong>{probability}%</strong>
        </div>
        <div
          aria-label={
            language === "ko"
              ? `${locationLabel(tool.mostLikelyLocationId, language)} 확률 ${probability}%`
              : `${locationLabel(tool.mostLikelyLocationId, language)} probability ${probability}%`
          }
          aria-valuemax={100}
          aria-valuemin={0}
          aria-valuenow={probability}
          className="tool-belief-probability-track"
          role="progressbar"
          style={{ "--tool-belief-probability": `${probability}%` } as CSSProperties}
        >
          <span />
        </div>
      </td>
      <td>
        <span className={`tool-belief-status ${tool.status}`}>
          <StatusIcon aria-hidden="true" size={14} strokeWidth={2.2} />
          {STATUS_LABELS[tool.status][language]}
        </span>
      </td>
      <td>
        <div className="tool-belief-alternatives">
          {alternativeLocations.length ? alternativeLocations.map((location) => (
            <span key={location.locationId}>
              {locationLabel(location.locationId, language)}
              <strong>{probabilityPercent(location.probability)}%</strong>
            </span>
          )) : (
            <span className="empty">{language === "ko" ? "대안 없음" : "No alternatives"}</span>
          )}
        </div>
      </td>
      <td>
        <div className="tool-belief-evidence">
          <span className="tool-belief-freshness">
            <Clock3 aria-hidden="true" size={13} strokeWidth={2.2} />
            {evidenceAgeLabel(effectiveEvidenceAgeSec, language)}
          </span>
          <span className="tool-belief-source-list">
            {sourceLabels.length ? sourceLabels.join(" · ") : (language === "ko" ? "시나리오 초기값" : "Scenario prior")}
          </span>
        </div>
      </td>
    </tr>
  );
}

export function ToolBeliefPanel({ runtime, language }: ToolBeliefPanelProps) {
  const [nowMs, setNowMs] = useState(Date.now());
  const [requestedEnabled, setRequestedEnabled] = useState<boolean | null>(null);
  const [controlFeedback, setControlFeedback] = useState<ControlFeedback | null>(null);
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    if (controlFeedback?.tone !== "success") return;
    const timer = window.setTimeout(() => setControlFeedback(null), 3000);
    return () => window.clearTimeout(timer);
  }, [controlFeedback]);
  const enabled = runtime?.enabled ?? null;
  const snapshot = runtime?.snapshot ?? null;
  const snapshotAgeMs = snapshot ? Math.max(0, nowMs - snapshot.receivedAt) : 0;
  const snapshotAgeSec = snapshotAgeMs / 1000;
  const isStale = Boolean(enabled && snapshot && snapshotAgeMs > TOOL_BELIEF_STALE_AFTER_MS);
  const titleId = "tool-belief-panel-title";
  const staleId = "tool-belief-panel-stale";
  const capacityMode = snapshot?.tools.some(isExchangeableToolBelief) ?? false;
  const panelRows = aggregateToolBeliefPanelRows(snapshot?.tools ?? []);
  const toolCount = snapshot?.tools.length ?? 0;
  const activeToolCount = panelRows.reduce((total, row) => total + row.quantity, 0);
  const ignoredCount = snapshot?.ignoredOutOfInventoryCount ?? 0;
  const ignoredTitle = snapshot?.ignoredClassNames.join(", ") || undefined;
  const controlPending = requestedEnabled !== null;
  const setTrackerEnabled = async () => {
    if (!runtime || enabled === null || controlPending) return;
    const desired = !enabled;
    setRequestedEnabled(desired);
    setControlFeedback(null);
    const result = await runtime.setEnabled(desired);
    setRequestedEnabled(null);
    setControlFeedback({
      tone: result.accepted ? "success" : "error",
      text: result.accepted
        ? language === "ko"
          ? desired
            ? "도구 위치 기능을 켰습니다. Digital Twin 상태를 다시 동기화합니다."
            : "도구 위치 기능을 껐습니다."
          : desired
            ? "Tool location tracking is on. Resynchronizing Digital Twin state."
            : "Tool location tracking is off."
        : language === "ko"
          ? `도구 위치 기능 전환 실패: ${result.message}`
          : `Could not change tool location tracking: ${result.message}`,
    });
  };

  return (
    <section
      aria-describedby={isStale ? staleId : undefined}
      aria-labelledby={titleId}
      className="tool-belief-panel"
      data-control-surface="tool-belief-tracker"
      data-enabled={enabled === null ? "unknown" : String(enabled)}
      data-slot="tool-belief-panel"
      data-stale={isStale ? "true" : "false"}
    >
      <header className="tool-belief-panel-header">
        <div className="tool-belief-panel-title">
          <Eye aria-hidden="true" size={16} strokeWidth={2.2} />
          <div>
            <span>{language === "ko" ? "REAL-TO-SIM · 관측 전용" : "REAL-TO-SIM · OBSERVATION ONLY"}</span>
            <strong id={titleId}>{language === "ko" ? "도구 위치 신뢰도" : "Tool location belief"}</strong>
          </div>
        </div>
        <div className="tool-belief-panel-badges" aria-label={language === "ko" ? "추적기 상태" : "Tracker status"}>
          <span className={enabled ? "enabled" : enabled === false ? "disabled" : "unknown"}>
            {enabled === null
              ? language === "ko" ? "상태 확인 중" : "Checking status"
              : enabled
                ? language === "ko" ? "기능 켜짐" : "Tracking on"
                : language === "ko" ? "기능 꺼짐" : "Tracking off"}
          </span>
          {snapshot ? (
            <span className="fixed">
              {capacityMode
                ? language === "ko"
                  ? `활성 도구 ${activeToolCount}/${toolCount}`
                  : `${activeToolCount}/${toolCount} active tools`
                : language === "ko"
                  ? `고정 인벤토리 ${toolCount}개`
                  : `${toolCount} fixed tools`}
            </span>
          ) : null}
          {isStale ? (
            <span className="stale" id={staleId}>
              <Clock3 aria-hidden="true" size={14} strokeWidth={2.2} />
              {trackerDelayLabel(snapshotAgeSec, language)}
            </span>
          ) : null}
          {snapshot?.robotMotionActive ? (
            <span className="motion">
              {language === "ko"
                ? `로봇 작업 중 · 전역 비탐지 ${probabilityPercent(snapshot.robotMotionNegativeScale)}% 반영`
                : `Robot active · global negative evidence ${probabilityPercent(snapshot.robotMotionNegativeScale)}%`}
            </span>
          ) : null}
          {ignoredCount > 0 ? (
            <span className="ignored" title={ignoredTitle}>
              {language === "ko" ? `인벤토리 외 ${ignoredCount}건 제외` : `${ignoredCount} out-of-inventory ignored`}
            </span>
          ) : null}
        </div>
        <button
          aria-busy={controlPending}
          aria-label={
            enabled === null
              ? language === "ko" ? "도구 위치 기능 상태 확인 중" : "Checking tool location tracking status"
              : language === "ko"
                ? `도구 위치 기능 ${enabled ? "끄기" : "켜기"}`
                : `Turn tool location tracking ${enabled ? "off" : "on"}`
          }
          aria-pressed={enabled ?? undefined}
          className="tool-belief-toggle"
          disabled={!runtime || enabled === null || controlPending}
          onClick={() => void setTrackerEnabled()}
          type="button"
        >
          {controlPending ? (
            <LoaderCircle aria-hidden="true" size={16} strokeWidth={2.2} />
          ) : (
            <Power aria-hidden="true" size={16} strokeWidth={2.2} />
          )}
          <span>
            {controlPending
              ? language === "ko"
                ? `${requestedEnabled ? "켜는" : "끄는"} 중…`
                : `Turning ${requestedEnabled ? "on" : "off"}…`
              : enabled === null
                ? language === "ko" ? "확인 중" : "Checking"
                : language === "ko"
                  ? enabled ? "끄기" : "켜기"
                  : enabled ? "Turn off" : "Turn on"}
          </span>
        </button>
      </header>

      {controlFeedback ? (
        <div
          className={`tool-belief-control-feedback ${controlFeedback.tone}`}
          role={controlFeedback.tone === "error" ? "alert" : "status"}
        >
          {controlFeedback.tone === "success" ? (
            <CheckCircle2 aria-hidden="true" size={14} strokeWidth={2.2} />
          ) : (
            <CircleHelp aria-hidden="true" size={14} strokeWidth={2.2} />
          )}
          <span>{controlFeedback.text}</span>
        </div>
      ) : null}

      {enabled === true && snapshot && panelRows.length ? (
        <div
          aria-label={
            language === "ko"
              ? "도구 위치 확률 표 · 좌우로 스크롤 가능"
              : "Tool location probability table · horizontally scrollable"
          }
          className="tool-belief-table-scroll"
          role="region"
          tabIndex={0}
        >
          <table className="tool-belief-table">
            <caption>
              {language === "ko"
                ? capacityMode
                  ? `${snapshot.procedureId} 시나리오의 활성 도구 ${activeToolCount}/${toolCount} 위치 확률`
                  : `${snapshot.procedureId} 시나리오의 고정 도구 위치 확률`
                : capacityMode
                  ? `Location probabilities for ${activeToolCount}/${toolCount} active tools in ${snapshot.procedureId}`
                  : `Fixed tool location probabilities for ${snapshot.procedureId}`}
            </caption>
            <thead>
              <tr>
                <th scope="col">{language === "ko" ? "도구" : "Tool"}</th>
                <th scope="col">{language === "ko" ? "위치 신뢰도" : "Location confidence"}</th>
                <th scope="col">{language === "ko" ? "상태" : "State"}</th>
                <th scope="col">{language === "ko" ? "다른 가능성" : "Alternatives"}</th>
                <th scope="col">{language === "ko" ? "최근 근거" : "Latest evidence"}</th>
              </tr>
            </thead>
            <tbody>
              {panelRows.map((row) => (
                <ToolBeliefRow
                  key={row.key}
                  tool={row.tool}
                  exchangeable={row.exchangeable}
                  quantity={row.quantity}
                  language={language}
                  snapshotAgeSec={snapshotAgeSec}
                />
              ))}
            </tbody>
          </table>
        </div>
      ) : enabled === true && snapshot && capacityMode ? (
        <div className="tool-belief-empty" role="status">
          <EyeOff aria-hidden="true" size={18} strokeWidth={2.2} />
          <span>
            {language === "ko"
              ? `활성 도구가 없습니다. 시나리오 용량 ${toolCount}개 슬롯은 유지됩니다.`
              : `No tools are active. The scenario capacity of ${toolCount} slots is retained.`}
          </span>
        </div>
      ) : enabled === false ? (
        <div className="tool-belief-empty off" role="status">
          <EyeOff aria-hidden="true" size={18} strokeWidth={2.2} />
          <span>
            {language === "ko"
              ? "도구 위치 기능이 꺼져 있습니다. 켜면 대기 상태에서도 독립적으로 관측합니다."
              : "Tool location tracking is off. Turn it on to observe independently, including while idle."}
          </span>
        </div>
      ) : (
        <div className="tool-belief-empty" role="status">
          <Clock3 aria-hidden="true" size={16} strokeWidth={2.2} />
          <span>
            {enabled === null
              ? language === "ko"
                ? "도구 위치 기능의 현재 상태를 확인하고 있습니다."
                : "Checking the current tool location tracking status."
              : language === "ko"
                ? "Digital Twin 상태를 다시 동기화하고 있습니다. 잠시만 기다려 주세요."
                : "Resynchronizing Digital Twin state. This should only take a moment."}
          </span>
        </div>
      )}
    </section>
  );
}
