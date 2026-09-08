import type { CSSProperties } from "react";
import * as m from "framer-motion/m";
import { BrainCircuit, CheckCircle2, Clock3 } from "lucide-react";

import type { StageToolChipBadge } from "../../hooks/useDigitalTwinViewModel";
import { MOTION_DURATION, SILK_EASE } from "../../motion-system";
import type {
  StageToolCardPresentation,
  ToolConfirmationCountdown,
  ToolVlmEvidence,
} from "../../presentation/toolCardPresentation";
import type { Language } from "../../utils/display";
import type { DisplayToolChipPlacement } from "./toolBeliefStageProjection";
import "./stage-tool-card.css";

function quantityStackSpread(quantity: number): number {
  return Math.min(10, Math.max(0, quantity - 1) * 4);
}

function quantityStackLayerOffset(layerDepth: number, quantity: number): number {
  const layerCount = Math.max(1, quantity - 1);
  return (quantityStackSpread(quantity) * layerDepth) / layerCount;
}

function toolInstanceMarker(instanceId: string | undefined, instrumentId: string): string {
  const normalized = instanceId?.trim() ?? "";
  if (!normalized || normalized === instrumentId.trim()) return "";
  const markerIndex = normalized.lastIndexOf("#");
  if (markerIndex >= 0 && markerIndex < normalized.length - 1) {
    return normalized.slice(markerIndex, markerIndex + 17);
  }
  return normalized.slice(0, 16);
}

function visibleInstanceMarker(chip: DisplayToolChipPlacement): string {
  const instanceId = chip.displayInstanceId?.trim() ?? "";
  if (!instanceId || !chip.instanceIds.includes(instanceId)) return "";
  const shouldShow = (
    chip.active
    || chip.highlight !== "normal"
    // Leaving the rack is the spatial transition operators need to
    // disambiguate even before catalog presentation catches up.
    || chip.holderId !== "rack"
  );
  return shouldShow ? toolInstanceMarker(instanceId, chip.instrumentId) : "";
}

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

/** A dense Mayo grid has room for the recognizable instrument family, not a
 * clipped acronym such as "바전". The full label remains the card title. */
function mayoCompactLabel(label: string): string {
  const normalized = label.replace(/\([^)]*\)/g, "").trim();
  return normalized.split(/\s+/).find(Boolean) ?? label;
}

function confirmationCopy(
  confirmation: ToolConfirmationCountdown,
  language: Language,
): { full: string; compact: string; ready: string } {
  if (confirmation.kind === "recovery") {
    return language === "ko"
      ? { full: "회수 확정", compact: "회수", ready: "확정" }
      : { full: "Recovery", compact: "Recover", ready: "Ready" };
  }
  if (confirmation.kind === "reuse") {
    return language === "ko"
      ? { full: "재사용 유지", compact: "재사용", ready: "유지" }
      : { full: "Keep for reuse", compact: "Reuse", ready: "Ready" };
  }
  return language === "ko"
    ? { full: "요청 확정", compact: "요청", ready: "확정" }
    : { full: "Request", compact: "Request", ready: "Ready" };
}

function remainingTimeLabel(value: number, language: Language): string {
  const seconds = Math.max(0, value).toFixed(1);
  return language === "ko" ? `${seconds}초` : `${seconds}s`;
}

function ToolConfirmationTimer({
  confirmation,
  language,
  reduceMotion,
}: {
  confirmation: ToolConfirmationCountdown;
  language: Language;
  reduceMotion: boolean;
}) {
  const copy = confirmationCopy(confirmation, language);
  const remainingRatio = Math.max(0, Math.min(1, 1 - confirmation.progress));
  const status = confirmation.ready
    ? copy.ready
    : remainingTimeLabel(confirmation.remainingSec, language);
  const comparison = confirmation.probabilityComparison === "lte"
    ? language === "ko" ? "이하" : "at or below"
    : language === "ko" ? "이상" : "at or above";
  const description = language === "ko"
    ? `${copy.full}: n-gram 다음 도구 확률 ${percent(confirmation.confidence)}가 ${percent(confirmation.confidenceThreshold)} ${comparison}로 ${confirmation.stabilitySec.toFixed(1)}초 유지됨. ${confirmation.ready ? "확률 유지 조건 충족; 실제 실행은 DT가 결정" : `${remainingTimeLabel(confirmation.remainingSec, language)} 남음`}`
    : `${copy.full}: n-gram next-tool probability ${percent(confirmation.confidence)} held ${comparison} ${percent(confirmation.confidenceThreshold)} for ${confirmation.stabilitySec.toFixed(1)}s. ${confirmation.ready ? "Dwell met; execution remains DT-owned" : `${remainingTimeLabel(confirmation.remainingSec, language)} remaining`}`;
  const TimerIcon = confirmation.ready ? CheckCircle2 : Clock3;

  return (
    <span
      className={`stage-tool-confirmation ${confirmation.kind} ${confirmation.ready ? "ready" : "counting"}`}
      data-slot="stage-tool-confirmation"
      data-confirmation-kind={confirmation.kind}
      data-confirmation-ready={confirmation.ready ? "true" : "false"}
      data-confirmation-progress={confirmation.progress.toFixed(3)}
      title={description}
      aria-label={description}
    >
      <span className="stage-tool-confirmation-copy">
        <TimerIcon aria-hidden="true" size={10} strokeWidth={2.2} />
        <span className="stage-tool-confirmation-label">
          <span className="stage-tool-confirmation-label-full">{copy.full}</span>
          <span className="stage-tool-confirmation-label-compact">{copy.compact}</span>
        </span>
        <strong>{status}</strong>
      </span>
      <span className="stage-tool-confirmation-track" aria-hidden="true">
        <m.span
          className="stage-tool-confirmation-remaining"
          initial={false}
          animate={{ scaleX: remainingRatio }}
          transition={{
            duration: reduceMotion ? 0.01 : MOTION_DURATION.fast,
            ease: SILK_EASE,
          }}
        />
      </span>
    </span>
  );
}

function StageToolEvidenceBadges({
  evidence,
  language,
  reduceMotion,
}: {
  evidence: ToolVlmEvidence;
  language: Language;
  reduceMotion: boolean;
}) {
  const confirmation = evidence.mayoConfirmation ?? evidence.nextToolConfirmation;
  if (
    evidence.nextToolProbability === null
    && evidence.demandForecastProbability === null
    && confirmation === null
  ) return null;
  const demandForecastDescription = evidence.demandForecastProbability !== null
    ? language === "ko"
      ? `집도의 도구 재사용 예측 ${percent(evidence.demandForecastProbability)}`
      : `Surgeon tool reuse forecast ${percent(evidence.demandForecastProbability)}`
    : "";
  const nextToolDescription = evidence.nextToolProbability !== null
    ? language === "ko"
      ? `시스템 최종 다음 도구 ${evidence.nextToolRank ?? "?"}순위 ${percent(evidence.nextToolProbability)}`
      : `System-final next #${evidence.nextToolRank ?? "?"} ${percent(evidence.nextToolProbability)}`
    : "";
  return (
    <div
      className="operation-vlm-tool-evidence stage-tool-vlm-evidence"
      data-slot="operation-vlm-tool-evidence"
      data-vlm-next-tool={evidence.nextToolProbability === null ? "false" : "true"}
      data-vlm-next-tool-rank={evidence.nextToolRank ?? ""}
      data-vlm-tool-demand={evidence.demandForecastProbability === null ? "false" : "true"}
      data-mayo-observed={evidence.mayoObserved ? "true" : "false"}
      data-next-tool-authority="system"
      aria-label={language === "ko" ? "도구 예측" : "Tool forecasts"}
    >
      {evidence.nextToolProbability !== null ? (
        <span
          className="operation-vlm-tool-badge next"
          data-vlm-evidence="next-tool"
          data-vlm-next-tool-rank={evidence.nextToolRank ?? ""}
          title={nextToolDescription}
          aria-label={nextToolDescription}
        >
          <BrainCircuit aria-hidden="true" size={11} strokeWidth={2.2} />
          <span>{language === "ko" ? "다음" : "System next"}</span>
          <strong>{percent(evidence.nextToolProbability)}</strong>
        </span>
      ) : null}
      {evidence.demandForecastProbability !== null ? (
        <span
          className="operation-vlm-tool-badge next demand"
          data-vlm-evidence="tool-demand-forecast"
          title={demandForecastDescription}
          aria-label={demandForecastDescription}
        >
          <BrainCircuit aria-hidden="true" size={11} strokeWidth={2.2} />
          <span className="tool-demand-label" aria-hidden="true">
            <span className="tool-demand-label-full">
              {language === "ko" ? "재사용 예측" : "Reuse forecast"}
            </span>
            <span className="tool-demand-label-compact">
              {language === "ko" ? "재사용" : "Reuse"}
            </span>
          </span>
          <strong>{percent(evidence.demandForecastProbability)}</strong>
        </span>
      ) : null}
      {confirmation ? (
        <ToolConfirmationTimer
          confirmation={confirmation}
          language={language}
          reduceMotion={reduceMotion}
        />
      ) : null}
    </div>
  );
}

export function StageToolCard({
  anchorStyle,
  chip,
  footerBadges,
  language,
  moveDurationMs,
  presentation,
  reduceMotion,
}: {
  anchorStyle: CSSProperties;
  chip: DisplayToolChipPlacement;
  footerBadges: readonly StageToolChipBadge[];
  language: Language;
  moveDurationMs: number;
  presentation: StageToolCardPresentation;
  reduceMotion: boolean;
}) {
  const instanceMarker = visibleInstanceMarker(chip);
  const mayoLabel = chip.layoutVariant === "mayoList"
    ? mayoCompactLabel(chip.label)
    : "";
  const hasDemandForecast = presentation.evidence.demandForecastProbability !== null;
  const hasConfirmation = Boolean(
    presentation.evidence.mayoConfirmation ?? presentation.evidence.nextToolConfirmation,
  );
  return (
    <m.div
      layout
      className="tool-chip-anchor"
      data-tool-id={chip.id}
      data-tool-holder-id={chip.holderId}
      data-move-duration-ms={moveDurationMs}
      data-grid-index={chip.gridIndex}
      data-compact={chip.compact ? "true" : "false"}
      data-tool-count={chip.quantity}
      data-tool-instance-ids={chip.instanceIds.join(",")}
      style={
        {
          ...anchorStyle,
          "--tool-stack-spread": `${quantityStackSpread(chip.quantity)}px`,
        } as CSSProperties
      }
      transition={{
        layout: {
          duration: reduceMotion ? 0.01 : moveDurationMs / 1000,
          ease: SILK_EASE,
        },
      }}
      title={chip.label}
    >
      {chip.quantity > 1 ? (
        <span className="tool-chip-stack-layers" aria-hidden="true">
          {Array.from({ length: chip.quantity - 1 }, (_, index) => {
            const layerDepth = chip.quantity - 1 - index;
            return (
              <span
                className="tool-chip-stack-layer"
                key={`${chip.id}-quantity-layer-${layerDepth}`}
                style={
                  {
                    "--tool-stack-offset": `${quantityStackLayerOffset(layerDepth, chip.quantity)}px`,
                  } as CSSProperties
                }
              />
            );
          })}
        </span>
      ) : null}
      <m.article
        className={`stage-tool-card tool-chip ${chip.layoutVariant} ${chip.displayState} ${chip.highlight} ${
          chip.active ? "active" : ""
        } ${chip.contaminated ? "contaminated" : ""} ${chip.compact ? "compact" : ""} ${
          chip.quantity > 1 ? "quantity-stack" : ""
        } density-${chip.density}`}
        data-slot="stage-tool-card"
        initial={{ opacity: 0, scale: reduceMotion ? 1 : 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: reduceMotion ? 0.01 : MOTION_DURATION.normal, ease: SILK_EASE }}
      >
        <div className="tool-chip-header">
          <strong>
            <span className="chip-label-full">{chip.label}</span>
            <span className="chip-label-short">{chip.shortLabel}</span>
            {mayoLabel ? (
              <span className="chip-label-mayo-compact">{mayoLabel}</span>
            ) : null}
          </strong>
          {instanceMarker ? (
            <span
              className="tool-instance-marker"
              data-slot="stage-tool-instance-marker"
              data-tool-instance-id={chip.displayInstanceId}
              aria-label={
                language === "ko"
                  ? `${chip.label} 인스턴스 ${instanceMarker}`
                  : `${chip.label} instance ${instanceMarker}`
              }
            >
              {instanceMarker}
            </span>
          ) : null}
        </div>
        {chip.quantity > 1 ? (
          <span
            className="tool-quantity-badge"
            aria-label={
              language === "ko"
                ? `${chip.label} ${chip.quantity}개`
                : `${chip.quantity} ${chip.label} instruments`
            }
          >
            ×{chip.quantity}
          </span>
        ) : null}
        <div
          aria-label={chip.layoutVariant === "mayoList" ? `${chip.label} ${language === "ko" ? "상태와 예측" : "status and forecasts"}` : undefined}
          className={`stage-tool-card-meta ${hasDemandForecast ? "has-demand-forecast" : ""} ${
            hasConfirmation ? "has-confirmation" : ""
          }`.trim()}
          tabIndex={chip.layoutVariant === "mayoList" ? 0 : undefined}
        >
          <div className="tool-chip-footer-badges" aria-label={`${chip.label} status`}>
            {footerBadges.map((badge) => (
              <span key={`${chip.id}-${badge.label}`} className={badge.tone}>
                {badge.label}
              </span>
            ))}
          </div>
          <StageToolEvidenceBadges
            evidence={presentation.evidence}
            language={language}
            reduceMotion={reduceMotion}
          />
        </div>
      </m.article>
    </m.div>
  );
}
