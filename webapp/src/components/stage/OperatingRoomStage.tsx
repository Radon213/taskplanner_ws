import { type CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, useReducedMotion } from "framer-motion";
import * as m from "framer-motion/m";
import { Hand, ScanLine } from "lucide-react";

import type {
  StagePhaseStep,
  StageToolChipBadge,
  StageToolChipPlacement,
  useDigitalTwinViewModel,
} from "../../hooks/useDigitalTwinViewModel";
import type { PerceptionLayerHealth } from "../../hooks/useRosBridge";
import { MOTION_DURATION, SILK_EASE } from "../../motion-system";
import { BedRobotArmCard } from "./BedRobotArmCard";
import {
  StageCameraToggleViewport,
  StageCameraViewport,
  type StageCameraFrames,
} from "./StageCameraViewport";

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

type QuantifiedToolChipPlacement = StageToolChipPlacement & {
  quantity?: number;
  count?: number;
  instanceIds?: string[];
};

type DisplayToolChipPlacement = StageToolChipPlacement & {
  quantity: number;
  instanceIds: string[];
};

type SystemSurgeonRequest = {
  confirmed: boolean;
  requestedTool: string;
};

const HIGHLIGHT_PRIORITY: Record<StageToolChipPlacement["highlight"], number> = {
  requested: 2,
  predicted: 1,
  normal: 0,
};

function normalizedToolGroupKey(value: string): string {
  return value.trim().replace(/\s+/g, " ").toLocaleLowerCase();
}

function quantityForChip(chip: QuantifiedToolChipPlacement): number {
  const rawQuantity = chip.quantity ?? chip.count ?? 1;
  return Number.isFinite(rawQuantity) ? Math.max(1, Math.floor(rawQuantity)) : 1;
}

function quantityStackSpread(quantity: number): number {
  return Math.min(10, Math.max(0, quantity - 1) * 4);
}

function quantityStackLayerOffset(layerDepth: number, quantity: number): number {
  const layerCount = Math.max(1, quantity - 1);
  return (quantityStackSpread(quantity) * layerDepth) / layerCount;
}

function instanceIdsForChip(chip: QuantifiedToolChipPlacement): string[] {
  const ids = chip.instanceIds?.filter(Boolean) ?? [];
  return ids.length ? ids : chip.id ? [chip.id] : [];
}

function mergeToolBadges(chips: StageToolChipPlacement[]): StageToolChipBadge[] {
  const badges = new Map<string, StageToolChipBadge>();
  for (const chip of chips) {
    for (const badge of chip.footerBadges) {
      badges.set(`${badge.tone}:${badge.label}`, badge);
    }
  }
  return [...badges.values()];
}

function rankedRackRepresentative(chips: QuantifiedToolChipPlacement[]): QuantifiedToolChipPlacement | undefined {
  return [...chips].sort((left, right) => {
    if (left.active !== right.active) return left.active ? -1 : 1;
    const highlightDelta = HIGHLIGHT_PRIORITY[right.highlight] - HIGHLIGHT_PRIORITY[left.highlight];
    if (highlightDelta) return highlightDelta;
    return left.gridIndex - right.gridIndex || left.id.localeCompare(right.id);
  })[0];
}

function aggregateRackTools(chips: StageToolChipPlacement[], vm: ViewModel): DisplayToolChipPlacement[] {
  const quantifiedChips = chips as QuantifiedToolChipPlacement[];
  const chipsByLabel = new Map<string, QuantifiedToolChipPlacement[]>();
  for (const chip of quantifiedChips) {
    const key = normalizedToolGroupKey(chip.label) || chip.id;
    chipsByLabel.set(key, [...(chipsByLabel.get(key) ?? []), chip]);
  }

  const inventoryByLabel = new Map<
    string,
    { id: string; label: string; count: number }
  >();
  const selectedBundleInstruments = vm.layout.metadata?.bundles?.find(
    (bundle) => bundle.id === vm.activeBundle,
  )?.instruments;
  const inventoryInstruments = selectedBundleInstruments?.length
    ? selectedBundleInstruments
    : vm.layout.metadata?.instruments ?? [];
  for (const instrument of inventoryInstruments) {
    const label = vm.displayToolName(instrument.id);
    inventoryByLabel.set(normalizedToolGroupKey(label) || instrument.id, {
      id: instrument.id,
      label,
      count: Math.max(1, Math.floor(instrument.inventory_count ?? 1)),
    });
  }

  const rackPlacements: DisplayToolChipPlacement[] = [];
  const processedRackLabels = new Set<string>();

  for (const [labelKey, group] of chipsByLabel) {
    const rackChips = group.filter((chip) => chip.holderId === "rack");
    const inventory = inventoryByLabel.get(labelKey);
    const outsideQuantity = group
      .filter((chip) => chip.holderId !== "rack")
      .reduce((total, chip) => total + quantityForChip(chip), 0);
    const rackQuantity = inventory
      ? Math.max(0, inventory.count - outsideQuantity)
      : rackChips.reduce((total, chip) => total + quantityForChip(chip), 0);
    if (rackQuantity <= 0) continue;

    const representative = rankedRackRepresentative(rackChips);
    if (representative) {
      rackPlacements.push({
        ...representative,
        quantity: rackQuantity,
        instanceIds: [...new Set(rackChips.flatMap(instanceIdsForChip))],
        contaminated: rackChips.some((chip) => chip.contaminated),
        active: rackChips.some((chip) => chip.active),
        footerBadges: mergeToolBadges(rackChips),
      });
      processedRackLabels.add(labelKey);
      continue;
    }

    const source = group[0];
    const slot = vm.boardRackSlots.find(
      (candidate) => candidate.instrumentId === inventory?.id || normalizedToolGroupKey(candidate.label) === labelKey,
    );
    if (!source || !slot) continue;
    rackPlacements.push({
      ...source,
      id: `rack-inventory-${inventory?.id ?? labelKey}`,
      label: inventory?.label ?? source.label,
      holderId: "rack",
      holderLabel: vm.language === "ko" ? "랙" : "Rack",
      left: slot.rect.left,
      top: slot.rect.top,
      width: slot.rect.width,
      height: slot.rect.height,
      scale: 1,
      compact: false,
      gridIndex: vm.boardRackSlots.findIndex((candidate) => candidate.id === slot.id),
      displayState: "waiting",
      highlight: "normal",
      lifecycle: vm.ui.waitingState,
      footerBadges: [{ label: vm.ui.waitingState, tone: "neutral" }],
      contaminated: false,
      active: false,
      layoutVariant: "card",
      density: "regular",
      quantity: rackQuantity,
      instanceIds: [],
    });
    processedRackLabels.add(labelKey);
  }

  for (const [labelKey, inventory] of inventoryByLabel) {
    if (processedRackLabels.has(labelKey) || chipsByLabel.has(labelKey)) continue;
    const slot = vm.boardRackSlots.find((candidate) => candidate.instrumentId === inventory.id);
    if (!slot) continue;
    rackPlacements.push({
      id: `rack-inventory-${inventory.id}`,
      label: inventory.label,
      shortLabel: slot.shortLabel,
      holderId: "rack",
      holderLabel: vm.language === "ko" ? "랙" : "Rack",
      left: slot.rect.left,
      top: slot.rect.top,
      width: slot.rect.width,
      height: slot.rect.height,
      scale: 1,
      compact: false,
      gridIndex: vm.boardRackSlots.findIndex((candidate) => candidate.id === slot.id),
      displayState: "waiting",
      highlight: "normal",
      lifecycle: vm.ui.waitingState,
      footerBadges: [{ label: vm.ui.waitingState, tone: "neutral" }],
      contaminated: false,
      active: false,
      layoutVariant: "card",
      density: "regular",
      quantity: inventory.count,
      instanceIds: [],
    });
  }

  const nonRackPlacements = quantifiedChips
    .filter((chip) => chip.holderId !== "rack")
    .map((chip) => ({
      ...chip,
      quantity: quantityForChip(chip),
      instanceIds: instanceIdsForChip(chip),
    }));

  return [
    ...rackPlacements.sort((left, right) => left.gridIndex - right.gridIndex),
    ...nonRackPlacements,
  ];
}

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
  cameraFrames,
  perceptionCameraFrames,
  perceptionOverlayFrames,
  perceptionHealth,
  perceptionControlPending,
  onPerceptionEnabledChange,
  systemSurgeonRequest,
  onStageAspectChange,
}: {
  vm: ViewModel;
  cameraFrames?: StageCameraFrames;
  perceptionCameraFrames?: StageCameraFrames;
  perceptionOverlayFrames?: StageCameraFrames;
  perceptionHealth?: PerceptionLayerHealth;
  perceptionControlPending?: boolean;
  onPerceptionEnabledChange?: (enabled: boolean) => void;
  systemSurgeonRequest: SystemSurgeonRequest;
  onStageAspectChange?: (ratio: number) => void;
}) {
  const reduceMotion = useReducedMotion();
  const boardRef = useRef<HTMLDivElement>(null);
  const boardMetricsRef = useRef<BoardMetrics>({ width: 1, height: 1 });
  const previousToolRectsRef = useRef<Record<string, ToolMotionSnapshot>>({});
  const [nowMs, setNowMs] = useState(Date.now());
  const displayToolPlacements = useMemo(
    () => aggregateRackTools(vm.toolChipPlacements, vm),
    [vm],
  );
  const cameraLiveLabel = vm.language === "ko" ? "영상 수신 중" : "Live";
  const cameraWaitingLabel = vm.language === "ko" ? "연결 대기" : "Waiting";
  const recognitionLiveLabel = vm.language === "ko" ? "인식 결과" : "Detected";
  const recognitionWaitingLabel =
    vm.language === "ko" ? "인식 결과 대기" : "Waiting for detections";
  const perceptionEnabled = Boolean(
    perceptionHealth?.received && perceptionHealth.enabled,
  );
  const perceptionAvailable = Boolean(perceptionHealth?.received);
  const perceptionError = perceptionHealth?.status === "error";
  const perceptionStateLabel = !perceptionAvailable
    ? vm.language === "ko"
      ? "사용 불가"
      : "Unavailable"
    : perceptionError
      ? vm.language === "ko"
        ? "오류"
        : "Error"
      : perceptionEnabled
        ? vm.language === "ko"
          ? "켜짐"
          : "On"
        : vm.language === "ko"
          ? "꺼짐"
          : "Off";
  const perceptionControlLabel =
    vm.language === "ko" ? "객체 인식" : "Object detection";
  const perceptionTitle =
    perceptionHealth?.lastError ||
    (perceptionEnabled
      ? vm.language === "ko"
        ? "RF-DETR 객체 인식 결과를 영상에 표시합니다."
        : "Show RF-DETR detections in the camera views."
      : vm.language === "ko"
        ? "원본 영상을 표시합니다."
        : "Show raw camera frames.");
  const surgeonRequestConfirmed = systemSurgeonRequest.confirmed;
  const confirmedRequestTool = systemSurgeonRequest.requestedTool
    ? vm.displayToolName(systemSurgeonRequest.requestedTool)
    : vm.ui.none;

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
  const surgeonAlertClockKey = surgeonAlertBubbles
    .map((bubble) => `${bubble.id}:${bubble.occurredAt ?? 0}`)
    .join("|");
  const interruptAlertKey = vm.stage.interruptAlert
    ? vm.stage.interruptAlert.eventKey
    : "";
  const [hiddenInterruptAlertKey, setHiddenInterruptAlertKey] = useState("");
  const visibleInterruptAlert =
    vm.stage.interruptAlert && hiddenInterruptAlertKey !== interruptAlertKey ? vm.stage.interruptAlert : null;

  useEffect(() => {
    if (!surgeonAlertBubbles.some((bubble) => bubble.occurredAt)) return;
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [surgeonAlertClockKey]);

  useEffect(() => {
    if (!interruptAlertKey) {
      setHiddenInterruptAlertKey("");
      return;
    }
    setHiddenInterruptAlertKey("");
    const timer = window.setTimeout(() => setHiddenInterruptAlertKey(interruptAlertKey), 5200);
    return () => window.clearTimeout(timer);
  }, [interruptAlertKey]);

  return (
    <section className="stage-card foxglove-stage-card" aria-label={vm.ui.stageTitle}>
      <div className="stage-chrome">
        <div className="stage-header">
          <div>
            <p className="section-kicker">{vm.ui.stageTitle}</p>
            <h2>{vm.stage.procedureLabel}</h2>
            <button
              type="button"
              className={`stage-perception-toggle ${
                perceptionEnabled ? "enabled" : "disabled"
              } ${perceptionError ? "error" : ""}`}
              role="switch"
              aria-checked={perceptionEnabled}
              aria-label={`${perceptionControlLabel}: ${perceptionStateLabel}`}
              title={perceptionTitle}
              disabled={
                !perceptionAvailable ||
                Boolean(perceptionControlPending) ||
                !onPerceptionEnabledChange
              }
              onClick={() =>
                onPerceptionEnabledChange?.(!perceptionEnabled)
              }
            >
              <ScanLine aria-hidden="true" size={16} strokeWidth={2.1} />
              <span>{perceptionControlLabel}</span>
              <strong>
                {perceptionControlPending
                  ? vm.language === "ko"
                    ? "변경 중"
                    : "Updating"
                  : perceptionStateLabel}
              </strong>
            </button>
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
            frames={{
              cam2: cameraFrames?.cam2,
              flir:
                cameraFrames?.flir ??
                (perceptionEnabled ? perceptionCameraFrames?.flir : null),
            }}
            overlays={
              perceptionEnabled
                ? { flir: perceptionOverlayFrames?.flir }
                : undefined
            }
            cameraIds={["cam2", "flir"]}
            initialCamera="flir"
            liveLabel={cameraLiveLabel}
            liveLabels={{
              flir: perceptionEnabled
                ? recognitionLiveLabel
                : cameraLiveLabel,
            }}
            emptyLabel={cameraWaitingLabel}
            emptyLabels={{
              flir: perceptionEnabled
                ? recognitionWaitingLabel
                : cameraWaitingLabel,
            }}
            className="surgical-bed-camera-view"
          />
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
          const inlineBadges = holder.id === "cleaner" ? holder.badges : undefined;
          const floatingBadges = holder.id === "cleaner" ? undefined : holder.badges;
          return (
            <div
              key={holder.id}
              className={`holder-zone ${holder.tone} ${holder.active ? "active" : ""} ${
                holder.id === "surgeon" && (surgeonRequestConfirmed || displayedSurgeonAlerts.length)
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

        <StageCameraViewport
          cameraId="cam1"
          frame={cameraFrames?.cam1}
          liveLabel={cameraLiveLabel}
          emptyLabel={cameraWaitingLabel}
          className="independent-stage-camera cam1-stage-camera"
          style={{
            left: `${vm.boardCameraRects.cam1.left}%`,
            top: `${vm.boardCameraRects.cam1.top}%`,
            width: `${vm.boardCameraRects.cam1.width}%`,
            height: `${vm.boardCameraRects.cam1.height}%`,
          }}
        />

        <StageCameraToggleViewport
          frames={{
            cam3: cameraFrames?.cam3,
            cam4:
              cameraFrames?.cam4 ??
              (perceptionEnabled ? perceptionCameraFrames?.cam4 : null),
          }}
          overlays={
            perceptionEnabled
              ? { cam4: perceptionOverlayFrames?.cam4 }
              : undefined
          }
          cameraIds={["cam3", "cam4"]}
          initialCamera="cam3"
          liveLabel={cameraLiveLabel}
          liveLabels={{
            cam4: perceptionEnabled
              ? recognitionLiveLabel
              : cameraLiveLabel,
          }}
          emptyLabel={cameraWaitingLabel}
          emptyLabels={{
            cam4: perceptionEnabled
              ? recognitionWaitingLabel
              : cameraWaitingLabel,
          }}
          className="independent-stage-camera cam3-stage-camera"
          style={{
            left: `${vm.boardCameraRects.cam3.left}%`,
            top: `${vm.boardCameraRects.cam3.top}%`,
            width: `${vm.boardCameraRects.cam3.width}%`,
            height: `${vm.boardCameraRects.cam3.height}%`,
          }}
        />

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
          {displayToolPlacements.map((chip) => {
            const footerBadges = [...chip.footerBadges, ...chipAttentionBadges(chip, vm)];
            const previousRect = previousToolRects[chip.id];
            const moveDurationMs = toolMoveDurationMs(chip, previousRect, boardMetricsRef.current, Boolean(reduceMotion));
            return (
              <m.div
                key={chip.id}
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
                    ...anchorStyleForChip(chip, moveDurationMs),
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
                  className={`tool-chip ${chip.layoutVariant} ${chip.displayState} ${chip.highlight} ${chip.active ? "active" : ""} ${
                    chip.contaminated ? "contaminated" : ""
                  } ${chip.compact ? "compact" : ""} ${chip.quantity > 1 ? "quantity-stack" : ""} density-${chip.density}`}
                  initial={{ opacity: 0, scale: reduceMotion ? 1 : 0.96 }}
                  animate={{ opacity: 1, scale: 1 }}
                  transition={{ duration: reduceMotion ? 0.01 : MOTION_DURATION.normal, ease: SILK_EASE }}
                >
                  <div className="tool-chip-header">
                    <strong>
                      <span className="chip-label-full">{chip.label}</span>
                      <span className="chip-label-short">{chip.shortLabel}</span>
                    </strong>
                  </div>
                  {chip.quantity > 1 ? (
                    <span
                      className="tool-quantity-badge"
                      aria-label={
                        vm.language === "ko"
                          ? `${chip.label} ${chip.quantity}개`
                          : `${chip.quantity} ${chip.label} instruments`
                      }
                    >
                      ×{chip.quantity}
                    </span>
                  ) : null}
                  <div className="tool-chip-footer-badges" aria-label={`${chip.label} status`}>
                    {footerBadges.map((badge) => (
                      <span key={`${chip.id}-${badge.label}`} className={badge.tone}>
                        {badge.label}
                      </span>
                    ))}
                  </div>
                </m.article>
              </m.div>
            );
          })}
        </div>

        {surgeonHolder && (surgeonRequestConfirmed || displayedSurgeonAlerts.length) ? (
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
            <div className="surgeon-evidence-stack">
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
                          {bubble.occurredAt ? <time>{voiceAgeLabel(bubble.occurredAt, nowMs, vm.language)}</time> : null}
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
