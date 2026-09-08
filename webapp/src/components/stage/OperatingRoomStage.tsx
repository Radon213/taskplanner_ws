import { type CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, useReducedMotion } from "framer-motion";
import * as m from "framer-motion/m";
import { Hand } from "lucide-react";

import type {
  StagePhaseStep,
  StageToolChipBadge,
  StageToolChipPlacement,
  useDigitalTwinViewModel,
} from "../../hooks/useDigitalTwinViewModel";
import type { RankedToolPrediction, VLMResult } from "../../types";
import type { CameraPreviewContracts } from "../../ros/cameraPreviewContracts";
import type { LiveCameraMediaStore } from "../../ros/liveCameraMedia";
import type { TypedRfdetrObservationStore } from "../../ros/typedRfdetrObservationStore";
import type { ToolBeliefRuntime } from "../../ros/toolBeliefMessages";
import type { ToolPolicyStatus } from "../../ros/toolPolicyMessages";
import type {
  RosbagStageCameraId,
  RosbagStageCameraSlot,
  RosbagUiAuditEventName,
  RosbagUiPresentation,
} from "../../ros/rosbagUiAuditMessages";
import { MOTION_DURATION, SILK_EASE } from "../../motion-system";
import { BedRobotArmCard } from "./BedRobotArmCard";
import {
  HandHandoverSignalPopup,
  type HandHandoverSignal,
} from "../command/HandHandoverSignalStatus";
import {
  SurgeonFinalSentencePopup,
} from "../observability/OperationVlmObservability";
import type { OperationAsrFinalPresentation } from "../../presentation/operationPresentation";
import {
  mayoObservedToolIds,
  projectToolCardPresentation,
  systemToolPredictionsById,
  toolDemandForecastById,
} from "../../presentation/toolCardPresentation";
import {
  StageCameraToggleViewport,
  StageCameraViewport,
  type StageCameraFrames,
} from "./StageCameraViewport";
import { StageToolCard } from "./StageToolCard";
import {
  aggregateRackTools,
  projectToolBeliefsOntoStage,
  TOOL_BELIEF_STAGE_STALE_AFTER_MS,
} from "./toolBeliefStageProjection";

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
  if (reduceMotion) return 10;
  if (!previous) return 200;
  const dx = ((chip.left - previous.left) / 100) * metrics.width;
  const dy = ((chip.top - previous.top) / 100) * metrics.height;
  const distance = Math.hypot(dx, dy);
  if (distance < 2) return 200;
  return Math.round(Math.min(480, Math.max(240, 180 + distance * 0.5)));
}

function voiceAgeLabel(occurredAt: number | undefined, nowMs: number, language: "ko" | "en"): string {
  if (!occurredAt) return "";
  const elapsedSec = Math.max(0, Math.floor((nowMs - occurredAt) / 1000));
  if (elapsedSec < 1) return language === "ko" ? "방금" : "just now";
  return language === "ko" ? `${elapsedSec}초 전` : `${elapsedSec}s ago`;
}

function VoiceAge({ occurredAt, language }: { occurredAt: number; language: "ko" | "en" }) {
  const [nowMs, setNowMs] = useState(Date.now());
  useEffect(() => {
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [occurredAt]);
  return <time>{voiceAgeLabel(occurredAt, nowMs, language)}</time>;
}

function PhaseStepper({ steps, label }: { steps: StagePhaseStep[]; label: string }) {
  return (
    <div className="phase-stepper" aria-label={label} role="list">
      {steps.map((step, index) => (
        <div className={`phase-step ${step.state}`} key={step.id} role="listitem">
          <span>{index + 1}</span>
          <strong>{step.label}</strong>
        </div>
      ))}
    </div>
  );
}

export function OperatingRoomStage({
  vm,
  cameraFrames,
  cameraMediaStore,
  cameraPreviewContracts,
  typedRfdetrObservationStore,
  toolBeliefs,
  procedureRunning,
  surgeonRequestedTool,
  asrFinalSentence,
  handHandoverSignal,
  vlmResult,
  systemToolPredictions = [],
  toolPolicyStatus,
  onStageAspectChange,
  replayOnly = false,
  replayPresentation,
  onReplayStageCameraChange,
}: {
  vm: ViewModel;
  cameraFrames?: StageCameraFrames;
  cameraMediaStore?: LiveCameraMediaStore;
  cameraPreviewContracts?: CameraPreviewContracts;
  /** Latest-only typed facts; updates are isolated from the Stage/root tree. */
  typedRfdetrObservationStore?: TypedRfdetrObservationStore;
  /** Fixed-inventory, observation-only semantic location probabilities. */
  toolBeliefs?: ToolBeliefRuntime | null;
  procedureRunning: boolean;
  surgeonRequestedTool: string;
  /** Observer-only final transcript already formatted by the presentation owner. */
  asrFinalSentence: OperationAsrFinalPresentation | null;
  /** Reducer-authoritative direct hand-perception gate state. */
  handHandoverSignal: HandHandoverSignal;
  vlmResult: VLMResult;
  /** Reducer-accepted system-final ranking; never raw VLM candidates. */
  systemToolPredictions?: readonly RankedToolPrediction[];
  /** Current DT policy snapshot, already scoped to this procedure run. */
  toolPolicyStatus?: ToolPolicyStatus | null;
  onStageAspectChange?: (ratio: number) => void;
  replayOnly?: boolean;
  replayPresentation?: RosbagUiPresentation;
  onReplayStageCameraChange?: (
    event: Extract<
      RosbagUiAuditEventName,
      "stage_camera_selected" | "stage_camera_inspection_changed"
    >,
    slot: RosbagStageCameraSlot,
    camera: RosbagStageCameraId,
    inspecting: boolean,
  ) => void;
}) {
  const reduceMotion = useReducedMotion();
  const boardRef = useRef<HTMLDivElement>(null);
  const boardMetricsRef = useRef<BoardMetrics>({ width: 1, height: 1 });
  const previousToolRectsRef = useRef<Record<string, ToolMotionSnapshot>>({});
  const [nowMs, setNowMs] = useState(Date.now());
  useEffect(() => {
    if (toolBeliefs?.enabled !== true || !toolBeliefs.snapshot) return;
    const receivedAt = toolBeliefs.snapshot.receivedAt;
    const now = Date.now();
    setNowMs(now);
    // Freshness only changes at the expiry boundary. A permanent 2 Hz clock
    // unnecessarily reprojects every tool and camera in the operating room.
    const delay = Math.max(0, receivedAt + TOOL_BELIEF_STAGE_STALE_AFTER_MS - now) + 1;
    const timer = window.setTimeout(() => setNowMs(Date.now()), delay);
    return () => window.clearTimeout(timer);
  }, [toolBeliefs?.enabled, toolBeliefs?.snapshot?.receivedAt]);
  const toolBeliefProjection = useMemo(
    () => projectToolBeliefsOntoStage({
      activeBundle: vm.activeBundle,
      holders: vm.boardHolders,
      nowMs,
      placements: vm.toolChipPlacements,
      rackSlots: vm.boardRackSlots,
      runtime: toolBeliefs,
    }),
    [
      nowMs,
      toolBeliefs,
      vm.activeBundle,
      vm.boardHolders,
      vm.boardRackSlots,
      vm.toolChipPlacements,
    ],
  );
  const displayToolPlacements = useMemo(
    () => aggregateRackTools(
      toolBeliefProjection.placements,
      vm,
      toolBeliefProjection.rackSlots,
      toolBeliefProjection.hiddenInventoryCounts,
      toolBeliefProjection.exchangeableActiveCounts,
    ),
    [toolBeliefProjection, vm],
  );
  const systemPredictionByToolId = useMemo(
    () => systemToolPredictionsById(systemToolPredictions),
    [systemToolPredictions],
  );
  const demandForecastByToolId = useMemo(
    () => toolDemandForecastById(vlmResult),
    [vlmResult],
  );
  const mayoObservedToolIdSet = useMemo(
    () => mayoObservedToolIds(vlmResult),
    [vlmResult],
  );
  const cameraLiveLabel = vm.language === "ko" ? "영상 수신 중" : "Live";
  const cameraWaitingLabel = vm.language === "ko" ? "연결 대기" : "Waiting";
  const cameraLiveLabels = {
    cam3: cameraPreviewContracts?.cam3.semantic === "operator_overlay"
      ? vm.language === "ko" ? "인식 오버레이 수신 중" : "Detection overlay live"
      : cameraLiveLabel,
    cam4: cameraPreviewContracts?.cam4.semantic === "operator_overlay"
      ? vm.language === "ko" ? "인식 오버레이 수신 중" : "Detection overlay live"
      : cameraLiveLabel,
  };
  const cameraEmptyLabels = {
    cam3: cameraPreviewContracts?.cam3.semantic === "operator_overlay"
      ? vm.language === "ko" ? "인식 오버레이 없음 · 자동 재연결 중" : "Detection overlay missing · reconnecting"
      : cameraWaitingLabel,
    cam4: cameraPreviewContracts?.cam4.semantic === "operator_overlay"
      ? vm.language === "ko" ? "인식 오버레이 없음 · 자동 재연결 중" : "Detection overlay missing · reconnecting"
      : cameraWaitingLabel,
  };
  const surgeonRequestConfirmed = procedureRunning && Boolean(surgeonRequestedTool);
  const handHandoverActive = handHandoverSignal.active;
  const confirmedRequestTool = surgeonRequestedTool
    ? vm.displayToolName(surgeonRequestedTool)
    : vm.ui.none;
  const asrFinalEventKey = asrFinalSentence?.eventKey ?? "";
  const [visibleAsrFinalEventKey, setVisibleAsrFinalEventKey] = useState("");
  const visibleAsrFinalSentence = asrFinalSentence
    && asrFinalEventKey === visibleAsrFinalEventKey
    ? asrFinalSentence
    : null;
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
    for (const chip of displayToolPlacements) {
      nextRects[chip.id] = snapshotForChip(chip);
    }
    previousToolRectsRef.current = nextRects;
  }, [displayToolPlacements]);

  const surgeonAlertBubbles = vm.boardActionBubbles
    .filter((bubble) => bubble.id.startsWith("surgeon-"))
    .slice(0, 1);
  const displayedSurgeonAlerts = surgeonRequestConfirmed ? [] : surgeonAlertBubbles;
  const surgeonHolder = vm.boardHolders.find((holder) => holder.id === "surgeon");
  const interruptAlertKey = vm.stage.interruptAlert
    ? vm.stage.interruptAlert.eventKey
    : "";
  const [hiddenInterruptAlertKey, setHiddenInterruptAlertKey] = useState("");
  const visibleInterruptAlert =
    vm.stage.interruptAlert && hiddenInterruptAlertKey !== interruptAlertKey ? vm.stage.interruptAlert : null;

  useEffect(() => {
    if (!interruptAlertKey) {
      setHiddenInterruptAlertKey("");
      return;
    }
    setHiddenInterruptAlertKey("");
    const timer = window.setTimeout(() => setHiddenInterruptAlertKey(interruptAlertKey), 5200);
    return () => window.clearTimeout(timer);
  }, [interruptAlertKey]);

  useEffect(() => {
    if (!asrFinalEventKey) {
      setVisibleAsrFinalEventKey("");
      return;
    }
    setVisibleAsrFinalEventKey(asrFinalEventKey);
    const timer = window.setTimeout(() => {
      setVisibleAsrFinalEventKey((current) =>
        current === asrFinalEventKey ? "" : current,
      );
    }, 6200);
    return () => window.clearTimeout(timer);
  }, [asrFinalEventKey]);

  return (
    <section className="stage-card foxglove-stage-card" aria-label={vm.ui.stageTitle}>
      <div className="stage-chrome">
        <div className="stage-header">
          <div>
            <p className="section-kicker">{vm.ui.stageTitle}</p>
            <h2>{vm.stage.procedureLabel}</h2>
            {vm.stage.procedureTargetSite || vm.stage.procedureApproach ? (
              <dl className="stage-procedure-metadata" aria-label={vm.ui.procedureDetails}>
                {vm.stage.procedureTargetSite ? (
                  <div>
                    <dt>{vm.ui.targetSite}</dt>
                    <dd>{vm.stage.procedureTargetSite}</dd>
                  </div>
                ) : null}
                {vm.stage.procedureApproach ? (
                  <div>
                    <dt>{vm.ui.approach}</dt>
                    <dd>{vm.stage.procedureApproach}</dd>
                  </div>
                ) : null}
              </dl>
            ) : null}
          </div>
          <PhaseStepper steps={vm.stage.phaseSteps} label={vm.ui.phaseOverview} />
        </div>
        {vm.boardBedRobotArms.length ? (
          <div
            className="bed-robot-arm-rail"
            aria-label={vm.language === "ko" ? "리트랙션 로봇암 상태" : "Retraction robot arm status"}
          >
            {vm.boardBedRobotArms.map((arm) => (
              <BedRobotArmCard key={arm.armId} arm={arm} />
            ))}
          </div>
        ) : null}

        <AnimatePresence initial={false}>
          {visibleInterruptAlert ? (
            <m.div
              key={visibleInterruptAlert.phaseId}
              className="phase-interrupt-alert"
              role="status"
              aria-live="polite"
              initial={{ opacity: 0, y: reduceMotion ? 0 : -6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: reduceMotion ? 0 : -4 }}
              transition={{ duration: reduceMotion ? 0.1 : 0.18 }}
            >
              <span>{visibleInterruptAlert.title}</span>
              <strong>{visibleInterruptAlert.label}</strong>
              <p>{visibleInterruptAlert.message}</p>
            </m.div>
          ) : null}
        </AnimatePresence>
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
              <m.div
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
              </m.div>
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
          <m.div
            className="bed-phase-badge"
            key={vm.stage.phaseName}
            initial={{ opacity: 0, y: reduceMotion ? 0 : -4 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: reduceMotion ? 0.1 : 0.2 }}
          >
            <small>{vm.ui.currentPhase}</small>
            <strong>{vm.stage.phaseName}</strong>
          </m.div>
          <div className="surgical-bed-body" aria-hidden="true">
            <span />
          </div>
          <StageCameraToggleViewport
            frames={{ cam2: cameraFrames?.cam2, flir: cameraFrames?.flir }}
            mediaStore={cameraMediaStore}
            cameraIds={["cam2", "flir"]}
            sourceContracts={cameraPreviewContracts}
            initialCamera="flir"
            language={vm.language}
            liveLabel={cameraLiveLabel}
            emptyLabel={cameraWaitingLabel}
            className="surgical-bed-camera-view"
            replayOnly={replayOnly}
            replaySelectedCamera={replayPresentation?.stageSurgicalBedCamera}
            replayInspectionOpen={replayPresentation?.stageSurgicalBedInspecting}
            replaySlot="surgical_bed"
            onReplayPresentationChange={onReplayStageCameraChange}
          />
        </div>

        <div
          className={`mayo-stand-group ${
            vm.boardMayoStand.active || toolBeliefProjection.projectedHolderIds.has("mayo") ? "active" : ""
          }`}
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
          const inlineBadges = holder.id === "cleaner" ? holder.badges : undefined;
          const floatingBadges = holder.id === "cleaner" ? undefined : holder.badges;
          return (
            <div
              key={holder.id}
              className={`holder-zone ${holder.tone} ${
                holder.active || toolBeliefProjection.projectedHolderIds.has(holder.id) ? "active" : ""
              } ${
                holder.id === "surgeon" && (surgeonRequestConfirmed || displayedSurgeonAlerts.length || visibleAsrFinalSentence)
                  ? "has-evidence"
                  : ""
              }`}
              data-holder-id={holder.id}
              style={{
                left: `${holder.rect.left}%`,
                top: `${holder.rect.top}%`,
                width: `${holder.rect.width}%`,
                height: `${holder.rect.height}%`,
              }}
            >
              {holder.id !== "mayo" ? (
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

        <StageCameraToggleViewport
          frames={{ cam1: cameraFrames?.cam1, cam4: cameraFrames?.cam4 }}
          mediaStore={cameraMediaStore}
          typedRfdetrObservationStore={typedRfdetrObservationStore}
          sourceContracts={cameraPreviewContracts}
          cameraIds={["cam1", "cam4"]}
          initialCamera="cam1"
          language={vm.language}
          liveLabel={cameraLiveLabel}
          liveLabels={cameraLiveLabels}
          emptyLabel={cameraWaitingLabel}
          emptyLabels={cameraEmptyLabels}
          className="independent-stage-camera cam1-stage-camera"
          replayOnly={replayOnly}
          replaySelectedCamera={replayPresentation?.stageIndependentCamera}
          replayInspectionOpen={replayPresentation?.stageIndependentInspecting}
          replaySlot="independent"
          onReplayPresentationChange={onReplayStageCameraChange}
          style={{
            left: `${vm.boardCameraRects.cam1.left}%`,
            top: `${vm.boardCameraRects.cam1.top}%`,
            width: `${vm.boardCameraRects.cam1.width}%`,
            height: `${vm.boardCameraRects.cam1.height}%`,
          }}
        />

        <StageCameraViewport
          cameraId="cam3"
          frame={cameraFrames?.cam3}
          mediaStore={cameraMediaStore}
          typedRfdetrObservationStore={typedRfdetrObservationStore}
          sourceContract={cameraPreviewContracts?.cam3}
          liveLabel={cameraLiveLabels.cam3}
          emptyLabel={cameraEmptyLabels.cam3}
          className="independent-stage-camera cam3-stage-camera"
          replayOnly={replayOnly}
          replayInspectionOpen={replayPresentation?.stageCam3Inspecting}
          replaySlot="cam3"
          onReplayPresentationChange={onReplayStageCameraChange}
          style={{
            left: `${vm.boardCameraRects.cam3.left}%`,
            top: `${vm.boardCameraRects.cam3.top}%`,
            width: `${vm.boardCameraRects.cam3.width}%`,
            height: `${vm.boardCameraRects.cam3.height}%`,
          }}
        />

        <div className="rack-slots-layer" aria-hidden="true">
          {toolBeliefProjection.rackSlots.map((slot) => (
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
          {displayToolPlacements.map((chip) => {
            const systemPrediction = systemPredictionByToolId.get(chip.instrumentId);
            const presentation = projectToolCardPresentation({
              chip,
              procedureRunning,
              demandForecastReady: vm.vlmStatus.demandForecastReady,
              demandForecastByToolId,
              mayoObservedToolIdSet,
              systemPrediction,
              toolPolicyStatus,
            });
            const footerBadges = [
              ...chip.footerBadges,
              ...chipAttentionBadges(chip, vm),
              ...(presentation.evidence.mayoObserved
                ? [{
                    label: vm.language === "ko" ? "메이요 관측" : "Mayo observed",
                    tone: "neutral" as const,
                  }]
                : []),
            ];
            const previousRect = previousToolRects[chip.id];
            const moveDurationMs = toolMoveDurationMs(chip, previousRect, boardMetricsRef.current, Boolean(reduceMotion));
            return (
              <StageToolCard
                key={chip.id}
                anchorStyle={anchorStyleForChip(chip, moveDurationMs)}
                chip={chip}
                footerBadges={footerBadges}
                language={vm.language}
                moveDurationMs={moveDurationMs}
                presentation={presentation}
                reduceMotion={Boolean(reduceMotion)}
              />
            );
          })}
        </div>

        {surgeonHolder && (surgeonRequestConfirmed || displayedSurgeonAlerts.length || handHandoverActive || visibleAsrFinalSentence) ? (
          <div
            className="surgeon-evidence-overlay"
            aria-label={`${surgeonHolder.label} evidence`}
            style={{
              left: `${surgeonHolder.rect.left}%`,
              top: `${surgeonHolder.rect.top}%`,
              width: `${surgeonHolder.rect.width}%`,
              height: `${surgeonHolder.rect.height}%`,
            }}
          >
            <div
              className={`surgeon-evidence-stack ${handHandoverActive ? "has-hand-handover" : ""} ${visibleAsrFinalSentence ? "has-asr-final" : ""}`.trim()}
            >
              {visibleAsrFinalSentence ? (
                <SurgeonFinalSentencePopup
                  sentence={visibleAsrFinalSentence}
                  className="stage-asr-final-popup"
                />
              ) : null}
              {surgeonRequestConfirmed ? (
                <div
                  className="surgeon-hand-status active"
                  role="status"
                  aria-live="polite"
                  data-system-source="reducer-world-state"
                  data-system-event="tool-request-confirmed"
                >
                  <Hand aria-hidden="true" size={14} strokeWidth={2.2} />
                  <span>
                    {vm.language === "ko"
                      ? "도구 요청 확정"
                      : "Tool request confirmed"}
                  </span>
                  <strong>{confirmedRequestTool}</strong>
                </div>
              ) : null}
              {handHandoverActive ? (
                <HandHandoverSignalPopup
                  signal={handHandoverSignal}
                  language={vm.language}
                  observationOnly={!procedureRunning}
                  className="stage-hand-handover-popup"
                />
              ) : null}
              {displayedSurgeonAlerts.length ? (
                <div className="holder-alert-stack" aria-label={`${surgeonHolder.label} alerts`}>
                  <AnimatePresence initial={false}>
                    {displayedSurgeonAlerts.map((bubble) => (
                      <m.div
                        key={bubble.id}
                        layout
                        className="holder-embedded-bubble"
                        initial={{ opacity: 0, y: reduceMotion ? 0 : -6, scale: reduceMotion ? 1 : 0.98 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: reduceMotion ? 0 : -4, scale: reduceMotion ? 1 : 0.98 }}
                        transition={{
                          layout: { duration: reduceMotion ? 0.01 : MOTION_DURATION.normal, ease: SILK_EASE },
                          opacity: { duration: reduceMotion ? 0.1 : 0.16 },
                          y: { duration: reduceMotion ? 0.1 : 0.18 },
                          scale: { duration: reduceMotion ? 0.1 : 0.18 },
                        }}
                      >
                        <span>
                          <b>{bubble.title}</b>
                          {bubble.occurredAt ? <VoiceAge occurredAt={bubble.occurredAt} language={vm.language} /> : null}
                        </span>
                        <strong>{bubble.text}</strong>
                      </m.div>
                    ))}
                  </AnimatePresence>
                </div>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}
