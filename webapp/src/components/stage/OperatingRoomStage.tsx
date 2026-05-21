import { type CSSProperties, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

import type {
  StagePhaseStep,
  StageToolChipBadge,
  StageToolChipPlacement,
  useDigitalTwinViewModel,
} from "../../hooks/useDigitalTwinViewModel";

type ViewModel = ReturnType<typeof useDigitalTwinViewModel>;

type ToolMotionSnapshot = {
  left: number;
  top: number;
  width: number;
  height: number;
};

type BoardMetrics = {
  width: number;
  height: number;
};

function chipAttentionBadges(chip: StageToolChipPlacement, vm: ViewModel): StageToolChipBadge[] {
  const badges: StageToolChipBadge[] = [];
  if (chip.highlight === "requested") badges.push({ label: vm.ui.requestedBadge, tone: "warning" });
  if (chip.highlight === "predicted") badges.push({ label: vm.ui.predictedBadge, tone: "predicted" });
  return badges;
}

function chipZIndex(chip: StageToolChipPlacement): number {
  if (chip.active) return 20;
  if (chip.displayState === "handover") return 18;
  if (chip.displayState === "cleaning") return 17;
  if (chip.displayState === "recovery") return 16;
  if (chip.highlight !== "normal") return 15;
  return 10;
}

function snapshotForChip(chip: StageToolChipPlacement): ToolMotionSnapshot {
  return {
    left: chip.left,
    top: chip.top,
    width: chip.width,
    height: chip.height,
  };
}

function anchorStyleForChip(chip: StageToolChipPlacement, moveDurationMs: number): CSSProperties {
  return {
    left: `${chip.left - chip.width / 2}%`,
    top: `${chip.top - chip.height / 2}%`,
    width: `${chip.width}%`,
    height: `${chip.height}%`,
    zIndex: chipZIndex(chip),
    "--tool-move-duration": `${moveDurationMs}ms`,
  } as CSSProperties;
}

function toolMoveDurationMs(
  chip: StageToolChipPlacement,
  previous: ToolMotionSnapshot | undefined,
  metrics: BoardMetrics,
  reduceMotion: boolean,
): number {
  if (reduceMotion || !previous) return 220;
  const dx = ((chip.left - previous.left) / 100) * metrics.width;
  const dy = ((chip.top - previous.top) / 100) * metrics.height;
  const distance = Math.hypot(dx, dy);
  if (distance < 2) return 260;
  return Math.round(Math.min(980, Math.max(360, 260 + distance * 1.15)));
}

function voiceAgeLabel(occurredAt: number | undefined, nowMs: number, language: "ko" | "en"): string {
  if (!occurredAt) return "";
  const elapsedSec = Math.max(0, Math.floor((nowMs - occurredAt) / 1000));
  if (elapsedSec < 1) return language === "ko" ? "방금" : "just now";
  return language === "ko" ? `${elapsedSec}초 전` : `${elapsedSec}s ago`;
}

function PhaseStepper({ steps, label }: { steps: StagePhaseStep[]; label: string }) {
  return (
    <div className="phase-stepper" aria-label={label}>
      {steps.map((step, index) => (
        <div className={`phase-step ${step.state}`} key={step.id}>
          <span>{index + 1}</span>
          <strong>{step.label}</strong>
        </div>
      ))}
    </div>
  );
}

export function OperatingRoomStage({
  vm,
  onStageAspectChange,
}: {
  vm: ViewModel;
  onStageAspectChange?: (ratio: number) => void;
}) {
  const reduceMotion = useReducedMotion();
  const boardRef = useRef<HTMLDivElement>(null);
  const boardMetricsRef = useRef<BoardMetrics>({ width: 1, height: 1 });
  const previousToolRectsRef = useRef<Record<string, ToolMotionSnapshot>>({});
  const [nowMs, setNowMs] = useState(Date.now());

  useEffect(() => {
    const board = boardRef.current;
    if (!board) return;

    const reportAspect = () => {
      const rect = board.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        boardMetricsRef.current = { width: rect.width, height: rect.height };
        onStageAspectChange?.(rect.width / rect.height);
      }
    };

    reportAspect();
    const observer = new ResizeObserver(reportAspect);
    observer.observe(board);
    return () => observer.disconnect();
  }, [onStageAspectChange]);

  const previousToolRects = previousToolRectsRef.current;

  useEffect(() => {
    const nextRects: Record<string, ToolMotionSnapshot> = {};
    for (const chip of vm.toolChipPlacements) {
      nextRects[chip.id] = snapshotForChip(chip);
    }
    previousToolRectsRef.current = nextRects;
  }, [vm.toolChipPlacements]);

  const surgeonAlertBubbles = vm.boardActionBubbles.filter((bubble) => bubble.id.startsWith("surgeon-"));
  const surgeonAlertClockKey = surgeonAlertBubbles
    .map((bubble) => `${bubble.id}:${bubble.occurredAt ?? 0}`)
    .join("|");

  useEffect(() => {
    if (!surgeonAlertBubbles.some((bubble) => bubble.occurredAt)) return;
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [surgeonAlertClockKey]);

  return (
    <section className="stage-card foxglove-stage-card" aria-label={vm.ui.stageTitle}>
      <div className="stage-header">
        <div>
          <p className="section-kicker">{vm.ui.stageTitle}</p>
          <h2>{vm.stage.procedureLabel}</h2>
        </div>
        <PhaseStepper steps={vm.stage.phaseSteps} label={vm.ui.phaseOverview} />
      </div>

      <div className="or-stage foxglove-board" ref={boardRef}>
          <div className="stage-grid-floor" />

          <div
            className={`humanoid-group-zone ${vm.boardHumanoidGroup.active ? "active" : ""}`}
            data-visual-id="humanoid-group"
            style={{
              left: `${vm.boardHumanoidGroup.rect.left}%`,
              top: `${vm.boardHumanoidGroup.rect.top}%`,
              width: `${vm.boardHumanoidGroup.rect.width}%`,
              height: `${vm.boardHumanoidGroup.rect.height}%`,
          }}
        >
            <div className="humanoid-group-title">
              <strong>{vm.boardHumanoidGroup.label}</strong>
            </div>
          </div>

          <div
            className={`humanoid-action-status ${vm.boardHumanoidGroup.action.active ? "active" : "idle"}`}
            data-visual-id="humanoid-action"
            style={{
              left: `${vm.boardHumanoidGroup.action.rect.left}%`,
              top: `${vm.boardHumanoidGroup.action.rect.top}%`,
              width: `${vm.boardHumanoidGroup.action.rect.width}%`,
              height: `${vm.boardHumanoidGroup.action.rect.height}%`,
            } as CSSProperties}
          >
            <div className="humanoid-action-topline">
              <span className="humanoid-action-kicker">{vm.boardHumanoidGroup.action.title}</span>
              <span className="humanoid-action-state">
                {vm.boardHumanoidGroup.action.active ? vm.ui.busy : vm.ui.idle}
              </span>
            </div>
            <div className="humanoid-action-copy">
              <strong>{vm.boardHumanoidGroup.action.label}</strong>
              {vm.boardHumanoidGroup.action.milestone ? <em>{vm.boardHumanoidGroup.action.milestone}</em> : null}
              <span className="humanoid-action-tool">{vm.boardHumanoidGroup.action.toolLabel}</span>
            </div>
          </div>

          <AnimatePresence>
            {vm.boardActionBubbles.filter((bubble) => !bubble.id.startsWith("surgeon-")).map((bubble) => (
              <motion.div
                key={bubble.id}
                className={`holder-bubble ${bubble.tone}`}
                style={{ left: `${bubble.left}%`, top: `${bubble.top}%` }}
                initial={{ opacity: 0, y: -5, scale: 0.97 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: -4, scale: 0.97 }}
                transition={{ duration: reduceMotion ? 0.1 : 0.18 }}
              >
                <span>{bubble.title}</span>
                <strong>{bubble.text}</strong>
              </motion.div>
            ))}
          </AnimatePresence>

        <div
          className={`surgical-bed-zone ${vm.boardSurgicalBed.active ? "active" : ""}`}
          data-visual-id="surgical-bed"
          style={{
            left: `${vm.boardSurgicalBed.rect.left}%`,
            top: `${vm.boardSurgicalBed.rect.top}%`,
            width: `${vm.boardSurgicalBed.rect.width}%`,
            height: `${vm.boardSurgicalBed.rect.height}%`,
          }}
        >
          <div className="surgical-bed-label">
            <strong>{vm.boardSurgicalBed.label}</strong>
          </div>
          <motion.div
            className="bed-phase-badge"
            key={vm.stage.phaseName}
            initial={{ opacity: 0, y: reduceMotion ? 0 : -4 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: reduceMotion ? 0.1 : 0.2 }}
          >
            <small>{vm.ui.currentPhase}</small>
            <strong>{vm.stage.phaseName}</strong>
          </motion.div>
          <div className="surgical-bed-body" aria-hidden="true">
            <span />
          </div>
        </div>

        <div
          className={`mayo-stand-group ${vm.boardMayoStand.active ? "active" : ""}`}
          style={{
            left: `${vm.boardMayoStand.rect.left}%`,
            top: `${vm.boardMayoStand.rect.top}%`,
            width: `${vm.boardMayoStand.rect.width}%`,
            height: `${vm.boardMayoStand.rect.height}%`,
          }}
        >
          <strong>{vm.boardMayoStand.label}</strong>
        </div>

        {vm.boardHolders.map((holder) => {
          const holderSurgeonAlerts = holder.id === "surgeon" ? surgeonAlertBubbles : [];
          const inlineBadges = holder.id === "cleaner" ? holder.badges : undefined;
          const floatingBadges = holder.id === "cleaner" ? undefined : holder.badges;
          return (
            <div
              key={holder.id}
              className={`holder-zone ${holder.tone} ${holder.active ? "active" : ""} ${
                holderSurgeonAlerts.length ? "has-alerts" : ""
              }`}
              data-holder-id={holder.id}
              style={{
                left: `${holder.rect.left}%`,
                top: `${holder.rect.top}%`,
                width: `${holder.rect.width}%`,
                height: `${holder.rect.height}%`,
              }}
            >
              <div className="holder-title">
                <strong>{holder.label}</strong>
                {inlineBadges?.length ? (
                  <div className="holder-inline-badges" aria-label={`${holder.label} status`}>
                    {inlineBadges.map((badge) => (
                      <span key={`${holder.id}-${badge.label}`} className={badge.tone}>
                        {badge.label}
                      </span>
                    ))}
                  </div>
                ) : holder.meta ? (
                  <span>{holder.meta}</span>
                ) : null}
              </div>
              {holderSurgeonAlerts.length ? (
                <div className="holder-alert-stack" aria-label={`${holder.label} alerts`}>
                  <AnimatePresence initial={false}>
                    {holderSurgeonAlerts.map((bubble) => (
                      <motion.div
                        key={bubble.id}
                        layout
                        className="holder-embedded-bubble"
                        initial={{ opacity: 0, y: reduceMotion ? 0 : -6, scale: reduceMotion ? 1 : 0.98 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: reduceMotion ? 0 : -4, scale: reduceMotion ? 1 : 0.98 }}
                        transition={{
                          layout: { duration: reduceMotion ? 0.1 : 0.22, ease: [0.22, 1, 0.36, 1] },
                          opacity: { duration: reduceMotion ? 0.1 : 0.16 },
                          y: { duration: reduceMotion ? 0.1 : 0.18 },
                          scale: { duration: reduceMotion ? 0.1 : 0.18 },
                        }}
                      >
                        <span>
                          <b>{bubble.title}</b>
                          {bubble.occurredAt ? <time>{voiceAgeLabel(bubble.occurredAt, nowMs, vm.language)}</time> : null}
                        </span>
                        <strong>{bubble.text}</strong>
                      </motion.div>
                    ))}
                  </AnimatePresence>
                </div>
              ) : null}
              {floatingBadges?.length ? (
                <div className="holder-badges" aria-label={`${holder.label} status`}>
                  {floatingBadges.map((badge) => (
                    <span key={`${holder.id}-${badge.label}`} className={badge.tone}>
                      {badge.label}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}

        <div className="rack-slots-layer" aria-hidden="true">
          {vm.boardRackSlots.map((slot) => (
            <span
              key={slot.id}
              className={slot.occupied ? "occupied" : "vacant"}
              data-slot-id={slot.id}
              data-instrument-id={slot.instrumentId}
              style={{
                left: `${slot.rect.left}%`,
                top: `${slot.rect.top}%`,
                width: `${slot.rect.width}%`,
                height: `${slot.rect.height}%`,
              }}
            >
              {slot.occupied ? (
                <>
                  <strong>{slot.shortLabel}</strong>
                  <small>{slot.label}</small>
                </>
              ) : null}
            </span>
          ))}
        </div>

        <div className="stage-tools board-tools">
          {vm.toolChipPlacements.map((chip) => {
            const footerBadges = [...chip.footerBadges, ...chipAttentionBadges(chip, vm)];
            const previousRect = previousToolRects[chip.id];
            const moveDurationMs = toolMoveDurationMs(chip, previousRect, boardMetricsRef.current, Boolean(reduceMotion));
            return (
              <motion.div
                key={chip.id}
                layout
                className="tool-chip-anchor"
                data-tool-id={chip.id}
                data-tool-holder-id={chip.holderId}
                data-move-duration-ms={moveDurationMs}
                data-grid-index={chip.gridIndex}
                data-compact={chip.compact ? "true" : "false"}
                style={anchorStyleForChip(chip, moveDurationMs)}
                transition={{
                  layout: {
                    duration: reduceMotion ? 0.12 : moveDurationMs / 1000,
                    ease: [0.22, 1, 0.36, 1],
                  },
                }}
                title={chip.label}
              >
                <motion.article
                  className={`tool-chip ${chip.displayState} ${chip.highlight} ${chip.active ? "active" : ""} ${
                    chip.contaminated ? "contaminated" : ""
                  } ${chip.compact ? "compact" : ""}`}
                  initial={{ opacity: 0, scale: reduceMotion ? 1 : 0.96 }}
                  animate={{ opacity: 1, scale: 1 }}
                  transition={{ duration: reduceMotion ? 0.1 : 0.2, ease: [0.22, 1, 0.36, 1] }}
                >
                  <div className="tool-chip-header">
                    <strong>
                      <span className="chip-label-full">{chip.label}</span>
                      <span className="chip-label-short">{chip.shortLabel}</span>
                    </strong>
                  </div>
                  <div className="tool-chip-footer-badges" aria-label={`${chip.label} status`}>
                    {footerBadges.map((badge) => (
                      <span key={`${chip.id}-${badge.label}`} className={badge.tone}>
                        {badge.label}
                      </span>
                    ))}
                  </div>
                </motion.article>
              </motion.div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
