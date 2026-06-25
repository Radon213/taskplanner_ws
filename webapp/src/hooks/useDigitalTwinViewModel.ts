import { useEffect, useMemo, useRef } from "react";

import { layouts } from "../layouts";
import { applyVisualLayout } from "../visualLayouts";
import type {
  DisplayCatalog,
  DisplayCatalogEntry,
  InstrumentState,
  LayoutAnchor,
  LayoutBundle,
  LayoutDisplayMetadata,
  LayoutEntity,
  SimulationEvent,
  SimulationState,
  SkillStatus,
  SurgeonState,
  VLMHealth,
  VLMResult,
} from "../types";
import {
  displayPhaseName,
  displayToolName,
  elapsedLabel,
  parseEventDetail,
  titleize,
  type Language,
} from "../utils/display";
import { getUiCopy } from "../utils/uiCopy";
import { fanOutAnchorPoint, type ScenePoint } from "../utils/stageGeometry";
import type { OverrideAck } from "./useRosBridge";

export type StagePoint = ScenePoint;

export type StageToolTone = "ready" | "active" | "surgeon" | "cleaning" | "recovery" | "danger";

export type StageTool = {
  id: string;
  label: string;
  shortLabel: string;
  anchorId: string;
  point: StagePoint;
  lifecycle: string;
  tone: StageToolTone;
  contaminated: boolean;
  active: boolean;
  compact: boolean;
};

export type StageRoute = {
  source: LayoutAnchor;
  target: LayoutAnchor;
  kind: string;
  label: string;
  progress: number;
};

export type StageHolderId =
  | "rack"
  | "humanoid_left"
  | "humanoid_right"
  | "surgeon"
  | "cleaner"
  | "mayo_recovery"
  | "mayo_reuse";

export type StageHolderRect = {
  left: number;
  top: number;
  width: number;
  height: number;
};

export type StageHolderBadge = {
  label: string;
  tone: "neutral" | "active" | "warning";
};

export type StageHolder = {
  id: StageHolderId;
  label: string;
  rect: StageHolderRect;
  contentRect: StageHolderRect;
  tone: "rack" | "robot" | "surgeon" | "cleaner" | "mayo";
  active: boolean;
  meta?: string;
  badges?: StageHolderBadge[];
};

export type StageHumanoidGroup = {
  label: string;
  rect: StageHolderRect;
  active: boolean;
  action: {
    title: string;
    label: string;
    milestone: string;
    toolLabel: string;
    active: boolean;
    rect: StageHolderRect;
  };
};

export type StageRackSlot = {
  id: string;
  instrumentId: string;
  label: string;
  shortLabel: string;
  occupied: boolean;
  rect: StageHolderRect;
};

export type StageMayoStand = {
  label: string;
  rect: StageHolderRect;
  active: boolean;
};

export type StageSurgicalBed = {
  label: string;
  rect: StageHolderRect;
  phaseLabel: string;
  active: boolean;
};

export type StageInterruptAlert = {
  phaseId: string;
  label: string;
  title: string;
  message: string;
  eventKey: string;
};

export type StageToolDisplayState = "waiting" | "handover" | "using" | "recovery" | "cleaning";

export type StageToolChipBadge = {
  label: string;
  tone: "neutral" | "active" | "warning" | "danger" | "predicted";
};

export type StageToolChipDensity = "comfortable" | "regular" | "dense" | "micro";

export type StageToolChipPlacement = {
  id: string;
  label: string;
  shortLabel: string;
  holderId: StageHolderId;
  holderLabel: string;
  left: number;
  top: number;
  width: number;
  height: number;
  scale: number;
  compact: boolean;
  gridIndex: number;
  displayState: StageToolDisplayState;
  highlight: "requested" | "predicted" | "normal";
  lifecycle: string;
  footerBadges: StageToolChipBadge[];
  contaminated: boolean;
  active: boolean;
  layoutVariant: "card" | "mayoList";
  density: StageToolChipDensity;
};

export type StageBoardRoute = {
  sourceHolderId: StageHolderId;
  targetHolderId: StageHolderId;
  source: StagePoint;
  target: StagePoint;
  kind: string;
  label: string;
  progress: number;
};

export type StageAudioBubble = {
  title: string;
  text: string;
  tone: "audio" | "override";
};

export type StageActionBubble = {
  id: string;
  title: string;
  text: string;
  tone: "surgeon" | "robot";
  left: number;
  top: number;
  occurredAt?: number;
};

export type StagePhaseStep = {
  id: string;
  label: string;
  state: "past" | "active" | "future";
};

export type TimelineItem = {
  id: string;
  uiId: string;
  title: string;
  meta: string;
  tone: "neutral" | "robot" | "surgeon" | "cleaning" | "warning";
  severity: "normal" | "warning" | "error";
};

export type BundleOption = {
  id: string;
  label: string;
};

export type RequestableToolOption = {
  id: string;
  label: string;
  voicePrompt: string;
};

function timelineSortParts(event: SimulationEvent, index: number, total: number): {
  time: number;
  sequence: number;
  fallback: number;
} {
  const [timestamp, sequence] = String(event.ui_id ?? "").split("-");
  const timeValue = Number(timestamp);
  const sequenceValue = Number(sequence);
  return {
    time: Number.isFinite(timeValue) ? timeValue : -1,
    sequence: Number.isFinite(sequenceValue) ? sequenceValue : 0,
    fallback: total - index,
  };
}

type ActiveVoiceCommand = {
  text: string;
  toolId: string;
  occurredAt: number;
};

type SurgeonReadySignalKind = "handover" | "retrieval";

type SurgeonQueuedCue = {
  id: string;
  eventType: string;
  toolId: string;
  voiceText: string;
  occurredAt: number;
};

function eventReceivedAt(event: SimulationEvent): number {
  const [timestamp] = String(event.ui_id ?? "").split("-");
  const timeValue = Number(timestamp);
  return Number.isFinite(timeValue) && timeValue > 0 ? timeValue : 0;
}

function requestedToolFromDetail(event: SimulationEvent, detail: Record<string, unknown>): string {
  return (
    event.instrument_id ||
    detailString(detail, "requested_tool") ||
    detailString(detail, "queued_tool") ||
    detailString(detail, "active_request_tool") ||
    detailString(detail, "tool_id") ||
    detailString(detail, "instrument_id")
  );
}

function activeVoiceCommandFromEvents(events: SimulationEvent[]): ActiveVoiceCommand | undefined {
  let activeCommand: ActiveVoiceCommand | undefined;
  for (const event of [...events].reverse()) {
    const detail = parseEventDetail(event.detail);
    const detailEventType = detailString(detail, "event_type");
    const voiceText = detailString(detail, "voice_text");
    const requestedTool = requestedToolFromDetail(event, detail);

    if (
      voiceText &&
      requestedTool &&
      (detailEventType === "voice_request" ||
        event.event_type === "SurgeonRequestObserved" ||
        event.event_type === "SurgeonActorEventObserved")
    ) {
      activeCommand = {
        text: voiceText,
        toolId: requestedTool,
        occurredAt: eventReceivedAt(event),
      };
      continue;
    }

    if (
      activeCommand &&
      event.event_type === "ToolHandoverCompleted" &&
      requestedToolFromDetail(event, detail) === activeCommand.toolId
    ) {
      activeCommand = undefined;
    }
  }
  return activeCommand;
}

function readySignalOccurredAt(events: SimulationEvent[], kind: SurgeonReadySignalKind, toolId: string): number {
  const eventTypes =
    kind === "handover"
      ? new Set(["request_tool", "voice_request", "extend_hand_for_handover"])
      : new Set(["return_tool", "extend_hand_for_retrieval"]);
  let latest = 0;
  for (const event of events) {
    const detail = parseEventDetail(event.detail);
    const detailEventType = detailString(detail, "event_type");
    const readyFlag = detailString(detail, kind === "handover" ? "ready_for_handover" : "ready_for_retrieval");
    const requestedTool = requestedToolFromDetail(event, detail);
    const matchesTool = !toolId || !requestedTool || requestedTool === toolId;
    const matchesKind = eventTypes.has(detailEventType) || readyFlag === "true" || readyFlag === "True";
    const timestamp = eventReceivedAt(event);
    if (matchesKind && matchesTool && timestamp > latest) latest = timestamp;
  }
  return latest;
}

function activeHandoverCuesFromEvents(events: SimulationEvent[]): SurgeonQueuedCue[] {
  const queue: SurgeonQueuedCue[] = [];
  for (const event of [...events].reverse()) {
    const detail = parseEventDetail(event.detail);
    if (event.event_type === "SurgeonRequestQueued") {
      const eventType = detailString(detail, "event_type");
      const toolId = detailString(detail, "queued_tool") || requestedToolFromDetail(event, detail);
      if (toolId && (eventType === "request_tool" || eventType === "voice_request" || eventType === "extend_hand_for_handover")) {
        const occurredAt = eventReceivedAt(event);
        queue.push({
          id: `surgeon-handover-${toolId}-${occurredAt || queue.length}`,
          eventType,
          toolId,
          voiceText: detailString(detail, "voice_text"),
          occurredAt,
        });
      }
      continue;
    }
    if (event.event_type === "SurgeonRequestDequeued") {
      const completedTool = detailString(detail, "completed_tool");
      const index = completedTool ? queue.findIndex((cue) => cue.toolId === completedTool) : 0;
      if (index >= 0) queue.splice(index, 1);
    }
  }
  return queue;
}

function compareTimelineEvents(
  leftEvent: SimulationEvent,
  rightEvent: SimulationEvent,
  leftIndex: number,
  rightIndex: number,
  total: number,
): number {
  const left = timelineSortParts(leftEvent, leftIndex, total);
  const right = timelineSortParts(rightEvent, rightIndex, total);
  if (left.time !== right.time) return right.time - left.time;
  if (left.sequence !== right.sequence) return right.sequence - left.sequence;
  return right.fallback - left.fallback;
}

const BOARD_HOLDER_ORDER: StageHolderId[] = [
  "rack",
  "humanoid_left",
  "humanoid_right",
  "surgeon",
  "cleaner",
  "mayo_recovery",
  "mayo_reuse",
];

const TERMINAL_SKILL_STATES = new Set([
  "completed",
  "result_failed",
  "dispatch_failed",
  "server_unavailable",
  "rejected",
]);

const STAGE_TOP = 9;
const STAGE_BOTTOM = 94;
const STAGE_HEIGHT = STAGE_BOTTOM - STAGE_TOP;
const STAGE_GAP = 3;
const DEFAULT_STAGE_ASPECT_RATIO = 1.55;
const STAGE_RIGHT = 99;
const RACK_LEFT = 3;
const RACK_WIDTH = 30;
const RACK_PADDING_X = 1;
const RACK_SLOT_GAP_X = 0.8;
const RACK_TITLE_SPACE = 5.8;
const RACK_PADDING_Y = 1;
const RACK_SLOT_COLUMNS = 2;
const RACK_SLOT_ROWS = 5;
const RACK_SLOT_COUNT = RACK_SLOT_COLUMNS * RACK_SLOT_ROWS;
const RACK_SLOT_GAP_Y = 0.8;
const RACK_CLEANER_GAP = 1;
const TOOL_CARD_W = (RACK_WIDTH - RACK_PADDING_X * 2 - RACK_SLOT_GAP_X) / RACK_SLOT_COLUMNS;
const HOLDER_LABEL_SPACE = 4.4;
const HOLDER_BADGE_SPACE = 3.2;
const HOLDER_BADGE_GAP = 1.1;
const HOLDER_PAD_X = 1.2;
const HOLDER_PAD_BOTTOM = 0.8;
const TOOL_CARD_MIN_SCALE = 0.46;
const TOOL_GRID_GAP = 0.8;
const TOOL_CARD_H =
  (STAGE_HEIGHT -
    RACK_TITLE_SPACE -
    RACK_PADDING_Y * 2 -
    RACK_SLOT_GAP_Y * (RACK_SLOT_ROWS - 1) -
    RACK_CLEANER_GAP -
    HOLDER_LABEL_SPACE -
    HOLDER_BADGE_SPACE -
    HOLDER_BADGE_GAP -
    HOLDER_PAD_BOTTOM) /
  (RACK_SLOT_ROWS + 1);
const HUMANOID_HOLDER_W = TOOL_CARD_W + HOLDER_PAD_X * 2;
const SINGLE_TOOL_HOLDER_H = HOLDER_LABEL_SPACE + TOOL_CARD_H + HOLDER_PAD_BOTTOM;
const BADGED_TOOL_HOLDER_H =
  HOLDER_LABEL_SPACE + HOLDER_BADGE_SPACE + HOLDER_BADGE_GAP + TOOL_CARD_H + HOLDER_PAD_BOTTOM;
const HUMANOID_GROUP_PAD = 1;
const HUMANOID_GROUP_TITLE_SPACE = HOLDER_LABEL_SPACE + 1;
const HUMANOID_ACTION_GAP = 1;
const HUMANOID_GROUP_BOTTOM_PAD = 1.4;
const HUMANOID_GROUP_LEFT = RACK_LEFT + RACK_WIDTH + STAGE_GAP;
const HUMANOID_LEFT = HUMANOID_GROUP_LEFT + HUMANOID_GROUP_PAD;
const BED_LEFT = HUMANOID_GROUP_LEFT + HUMANOID_HOLDER_W + HUMANOID_GROUP_PAD * 2 + STAGE_GAP;
const BED_WIDTH = 25;
const MAYO_LABEL_SPACE = 4.4;
const MAYO_PAD_X = 1;
const MAYO_PAD_BOTTOM = 0.9;
const MAYO_LANE_GAP = 1;
const SURGEON_LEFT = BED_LEFT + BED_WIDTH + STAGE_GAP;
const SURGEON_WIDTH = STAGE_RIGHT - SURGEON_LEFT;
const CLEANER_HOLDER_H = SINGLE_TOOL_HOLDER_H + 1.1;
const HAND_HOLDER_H = SINGLE_TOOL_HOLDER_H;
const CLEANER_TOP = STAGE_BOTTOM - RACK_PADDING_Y - CLEANER_HOLDER_H;
const MAYO_STAND_TOP = CLEANER_TOP - MAYO_LABEL_SPACE;
const BED_BOTTOM = MAYO_STAND_TOP - STAGE_GAP * DEFAULT_STAGE_ASPECT_RATIO;
const MAYO_STAND_LEFT = HUMANOID_GROUP_LEFT;
const MAYO_STAND_RIGHT = SURGEON_LEFT + SURGEON_WIDTH;
const HUMANOID_PANEL_TOP = STAGE_TOP + HUMANOID_GROUP_TITLE_SPACE;
const HUMANOID_PANEL_H =
  (BED_BOTTOM - HUMANOID_PANEL_TOP - HUMANOID_ACTION_GAP * 2 - HUMANOID_GROUP_BOTTOM_PAD) / 3;
const HUMANOID_LEFT_TOP = HUMANOID_PANEL_TOP;
const HUMANOID_ACTION_TOP = HUMANOID_LEFT_TOP + HUMANOID_PANEL_H + HUMANOID_ACTION_GAP;
const HUMANOID_RIGHT_TOP = HUMANOID_ACTION_TOP + HUMANOID_PANEL_H + HUMANOID_ACTION_GAP;

const BASE_HOLDER_RECTS: Record<StageHolderId, StageHolderRect> = {
  rack: { left: RACK_LEFT, top: STAGE_TOP, width: RACK_WIDTH, height: STAGE_HEIGHT },
  humanoid_left: { left: HUMANOID_LEFT, top: HUMANOID_LEFT_TOP, width: HUMANOID_HOLDER_W, height: HUMANOID_PANEL_H },
  humanoid_right: { left: HUMANOID_LEFT, top: HUMANOID_RIGHT_TOP, width: HUMANOID_HOLDER_W, height: HUMANOID_PANEL_H },
  surgeon: { left: SURGEON_LEFT, top: STAGE_TOP, width: SURGEON_WIDTH, height: BED_BOTTOM - STAGE_TOP },
  cleaner: {
    left: RACK_LEFT + RACK_PADDING_X,
    top: CLEANER_TOP,
    width: RACK_WIDTH - RACK_PADDING_X * 2,
    height: CLEANER_HOLDER_H,
  },
  mayo_recovery: { left: 0, top: 0, width: 0, height: 0 },
  mayo_reuse: { left: 0, top: 0, width: 0, height: 0 },
};

const BOARD_SURGICAL_BED_RECT: StageHolderRect = {
  left: BED_LEFT,
  top: STAGE_TOP,
  width: BED_WIDTH,
  height: BED_BOTTOM - STAGE_TOP,
};
const BOARD_HUMANOID_GROUP_RECT: StageHolderRect = {
  left: HUMANOID_GROUP_LEFT,
  top: STAGE_TOP,
  width: HUMANOID_HOLDER_W + HUMANOID_GROUP_PAD * 2,
  height: BED_BOTTOM - STAGE_TOP,
};
const BOARD_MAYO_STAND_RECT: StageHolderRect = {
  left: MAYO_STAND_LEFT,
  top: MAYO_STAND_TOP,
  width: MAYO_STAND_RIGHT - MAYO_STAND_LEFT,
  height: STAGE_BOTTOM - MAYO_STAND_TOP,
};

function holderTone(holderId: StageHolderId): StageHolder["tone"] {
  if (holderId === "rack") return "rack";
  if (holderId.startsWith("humanoid")) return "robot";
  if (holderId.startsWith("surgeon")) return "surgeon";
  if (holderId === "cleaner") return "cleaner";
  return "mayo";
}

function holderCenter(holderId: StageHolderId, holderRects: Record<StageHolderId, StageHolderRect>): StagePoint {
  const rect = holderRects[holderId];
  return {
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
  };
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function holderIdForAnchor(anchorId: string, lifecycleStage = "", locationType = ""): StageHolderId {
  if (anchorId.startsWith("main_tray_slot") || anchorId.includes("tray")) return "rack";
  if (anchorId === "robot_left_hand" || locationType === "robot_left_hand") return "humanoid_left";
  if (anchorId === "robot_right_hand" || locationType === "robot_right_hand") return "humanoid_right";
  if (
    anchorId === "surgeon_left_hand" ||
    anchorId === "surgeon_return_zone" ||
    anchorId === "surgeon_right_hand" ||
    anchorId === "surgeon_hand" ||
    anchorId === "surgeon_receive_zone" ||
    anchorId.startsWith("field_region") ||
    locationType === "handover_zone" ||
    locationType === "surgeon_hand" ||
    locationType === "surgical_field" ||
    lifecycleStage === "surgeon_owned"
  ) {
    return "surgeon";
  }
  if (anchorId === "cleaner_slot" || locationType === "cleaner_slot") return "cleaner";
  if (anchorId === "mayo_recovery_zone" || locationType === "mayo_recovery_zone") return "mayo_recovery";
  if (anchorId === "mayo_reuse_zone" || anchorId === "mayo_stand" || locationType === "mayo_reuse_zone" || locationType === "mayo_stand") {
    return "mayo_reuse";
  }
  return "rack";
}

function rackSlotRect(index: number): StageHolderRect {
  const rect = BASE_HOLDER_RECTS.rack;
  const column = index % RACK_SLOT_COLUMNS;
  const row = Math.floor(index / RACK_SLOT_COLUMNS);
  return {
    left: rect.left + RACK_PADDING_X + column * (TOOL_CARD_W + RACK_SLOT_GAP_X) + TOOL_CARD_W / 2,
    top: rect.top + RACK_TITLE_SPACE + RACK_PADDING_Y + row * (TOOL_CARD_H + RACK_SLOT_GAP_Y) + TOOL_CARD_H / 2,
    width: TOOL_CARD_W,
    height: TOOL_CARD_H,
  };
}

function holderUsesBadgeStrip(holderId: StageHolderId): boolean {
  return holderId === "surgeon";
}

function contentRectForHolder(holderId: StageHolderId, rect: StageHolderRect): StageHolderRect {
  const badgeSpace = holderUsesBadgeStrip(holderId) ? HOLDER_BADGE_SPACE : 0;
  const badgeGap = holderUsesBadgeStrip(holderId) ? HOLDER_BADGE_GAP : 0;
  return {
    left: rect.left + HOLDER_PAD_X,
    top: rect.top + HOLDER_LABEL_SPACE + badgeSpace + badgeGap,
    width: Math.max(0.1, rect.width - HOLDER_PAD_X * 2),
    height: Math.max(0.1, rect.height - HOLDER_LABEL_SPACE - badgeSpace - badgeGap - HOLDER_PAD_BOTTOM),
  };
}

function gridRectForHolder(
  holderId: StageHolderId,
  index: number,
  count: number,
  holderRects: Record<StageHolderId, StageHolderRect>,
): StageHolderRect & { scale: number; compact: boolean; gridIndex: number } {
  const rect = holderRects[holderId];
  const cardCount = Math.max(1, count);
  const maxColumns =
    holderId === "cleaner"
      ? 1
      : holderId === "mayo_recovery" || holderId === "mayo_reuse"
      ? 2
      : holderId === "surgeon"
        ? 1
        : cardCount > 1
          ? 2
          : 1;
  const columns = Math.max(1, Math.min(maxColumns, cardCount));
  const rows = Math.max(1, Math.ceil(cardCount / columns));
  const contentRect = contentRectForHolder(holderId, rect);
  const contentLeft = contentRect.left;
  const contentTop = contentRect.top;
  const contentWidth = contentRect.width;
  const contentHeight = contentRect.height;
  const scale = Math.max(
    TOOL_CARD_MIN_SCALE,
    Math.min(
      1,
      (contentWidth - TOOL_GRID_GAP * (columns - 1)) / (TOOL_CARD_W * columns),
      (contentHeight - TOOL_GRID_GAP * (rows - 1)) / (TOOL_CARD_H * rows),
    ),
  );
  const cardWidth = TOOL_CARD_W * scale;
  const cardHeight = TOOL_CARD_H * scale;
  const gridWidth = columns * cardWidth + (columns - 1) * TOOL_GRID_GAP;
  const gridHeight = rows * cardHeight + (rows - 1) * TOOL_GRID_GAP;
  const startLeft = contentLeft + Math.max(0, (contentWidth - gridWidth) / 2);
  const startTop = contentTop + Math.max(0, (contentHeight - gridHeight) / 2);
  const column = index % columns;
  const row = Math.floor(index / columns);
  return {
    left: startLeft + column * (cardWidth + TOOL_GRID_GAP) + cardWidth / 2,
    top: startTop + row * (cardHeight + TOOL_GRID_GAP) + cardHeight / 2,
    width: cardWidth,
    height: cardHeight,
    scale,
    compact: scale < 0.78 || cardWidth < 10,
    gridIndex: index,
  };
}

function mayoListRectForHolder(
  holderId: StageHolderId,
  index: number,
  count: number,
  holderRects: Record<StageHolderId, StageHolderRect>,
): StageHolderRect & { scale: number; compact: boolean; gridIndex: number } {
  const contentRect = contentRectForHolder(holderId, holderRects[holderId]);
  const rowCount = Math.max(1, count);
  const rowGap = rowCount > 1 ? 0.45 : 0;
  const rowHeight = Math.max(1.9, (contentRect.height - rowGap * (rowCount - 1)) / rowCount);
  const rowWidth = contentRect.width * 0.5;
  return {
    left: contentRect.left + contentRect.width / 2,
    top: contentRect.top + rowHeight / 2 + index * (rowHeight + rowGap),
    width: rowWidth,
    height: rowHeight,
    scale: Math.min(1, rowHeight / TOOL_CARD_H),
    compact: false,
    gridIndex: index,
  };
}

function toolChipDensityForHolder(holderId: StageHolderId, count: number, compact: boolean): StageToolChipDensity {
  if (holderId !== "mayo_recovery" && holderId !== "mayo_reuse") return compact ? "dense" : "regular";
  if (count <= 1) return "comfortable";
  if (count <= 3) return "regular";
  if (count <= 4) return "dense";
  return "micro";
}

function chipRectForHolder(
  holderId: StageHolderId,
  index: number,
  holderCount: number,
  holderRects: Record<StageHolderId, StageHolderRect>,
): StageHolderRect & { scale: number; compact: boolean; gridIndex: number } {
  if (holderId === "rack") {
    return { ...rackSlotRect(index), scale: 1, compact: false, gridIndex: index };
  }
  if (holderId === "mayo_recovery" || holderId === "mayo_reuse") {
    return mayoListRectForHolder(holderId, index, holderCount, holderRects);
  }
  return gridRectForHolder(holderId, index, holderCount, holderRects);
}

const RECOVERY_BADGE_LIFECYCLES = new Set(["surgeon_owned", "mayo_reuse", "mayo_recovery"]);

function displayLifecycleForInstrument(
  instrument: InstrumentState,
  activeRecoveryToolIds: Set<string>,
): string {
  const recoveryPending =
    (activeRecoveryToolIds.has(instrument.instrument_id) ||
      instrument.next_required_transition === "recover_left") &&
    RECOVERY_BADGE_LIFECYCLES.has(instrument.lifecycle_stage);
  return recoveryPending ? "mayo_recovery" : instrument.lifecycle_stage;
}

function displayStateForInstrument(
  instrument: InstrumentState,
  catalog: DisplayCatalog | undefined,
  activeRecoveryToolIds: Set<string>,
): StageToolDisplayState {
  return catalogToolDisplayState(catalogEntry(catalog, "lifecycle", displayLifecycleForInstrument(instrument, activeRecoveryToolIds)));
}

function footerBadgesForInstrument(
  instrument: InstrumentState,
  catalog: DisplayCatalog | undefined,
  language: Language,
  ui: ReturnType<typeof getUiCopy>,
  activeRecoveryToolIds: Set<string>,
): StageToolChipBadge[] {
  const lifecycleEntry = catalogEntry(catalog, "lifecycle", displayLifecycleForInstrument(instrument, activeRecoveryToolIds));
  const stateBadge = {
    label: localizedDisplayName(lifecycleEntry, language, ui.waitingState),
    tone: catalogBadgeTone(lifecycleEntry),
  };
  const badges: StageToolChipBadge[] = [stateBadge];
  if (instrument.contaminated) {
    badges.push({ label: ui.contaminated, tone: "danger" });
  }
  return badges;
}

function isLayoutBundle(value: unknown): value is LayoutBundle {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<LayoutBundle>;
  return Array.isArray(candidate.entities) && Array.isArray(candidate.anchors);
}

type BundleMetadata = NonNullable<LayoutDisplayMetadata["bundles"]>[number];

function bundleMetadataFor(metadata: LayoutDisplayMetadata | undefined, bundleName: string): BundleMetadata | undefined {
  if (!bundleName) return undefined;
  return metadata?.bundles?.find((bundle) => bundle.id === bundleName);
}

function selectedBundleMetadata(
  metadata: LayoutDisplayMetadata | undefined,
  bundleName: string,
): LayoutDisplayMetadata | undefined {
  const selected = bundleMetadataFor(metadata, bundleName);
  if (!selected) {
    if (!metadata || !bundleName || metadata.procedure?.id === bundleName) return metadata;
    return {
      display_catalog: metadata.display_catalog,
      bundles: metadata.bundles,
    };
  }
  return {
    procedure: {
      id: selected.id,
      display_name: selected.display_name,
      display_name_ko: selected.display_name_ko,
    },
    phases: selected.phases ?? [],
    normal_phase_ids: selected.normal_phase_ids ?? [],
    interrupt_phase_ids: selected.interrupt_phase_ids ?? [],
    instruments: selected.instruments ?? [],
    requestable_instruments: selected.requestable_instruments ?? [],
    display_catalog: metadata?.display_catalog,
    bundles: metadata?.bundles,
  };
}

function genericProcedureLayout(bundleName: string, instrumentCount = RACK_SLOT_COUNT): LayoutBundle {
  const slotCount = Math.max(RACK_SLOT_COUNT, instrumentCount);
  return {
    entities: [
      { id: "humanoid_body", type: "humanoid", x: 36.5, y: 34, width: 17, height: 32, label: "Humanoid Assistant" },
      { id: "surgeon_actor", type: "surgeon", x: 84.5, y: 29, width: 12, height: 27, label: "Surgeon" },
      { id: `${bundleName || "procedure"}_bed`, type: "surgical_bed", x: 58, y: 32.5, width: 24, height: 28, label: "OR Bed" },
      { id: "instrument_rack", type: "instrument_rack", x: 9.5, y: 43.5, width: 23, height: 32, label: "Instrument Rack" },
      { id: "mayo_stand", type: "mayo_stand", x: 55.5, y: 62.5, width: 29, height: 8.5, label: "Mayo Stand" },
      { id: "cleaner_station", type: "cleaner_station", x: 13, y: 14, width: 11.5, height: 11.5, label: "Cleaner" },
      { id: "unknown_zone", type: "unknown_zone", x: 88, y: 62, width: 10, height: 8, label: "Unresolved" },
    ],
    anchors: [
      { id: "cleaner_slot", attached_to: "cleaner_station", x: 18.8, y: 19.8, label: "Cleaner Slot" },
      { id: "robot_left_hand", attached_to: "humanoid_body", x: 37.5, y: 46, label: "Left Hand" },
      { id: "robot_right_hand", attached_to: "humanoid_body", x: 53.5, y: 41.5, label: "Right Hand" },
      { id: "surgeon_receive_zone", attached_to: "surgeon_actor", x: 79.8, y: 43.5, label: "Receive Zone" },
      { id: "surgeon_return_zone", attached_to: "surgeon_actor", x: 78.5, y: 53, label: "Return Zone" },
      { id: "surgeon_hand", attached_to: "surgeon_actor", x: 88, y: 42.5, label: "Surgeon Hand" },
      { id: "field_region_procedure", attached_to: `${bundleName || "procedure"}_bed`, x: 66.2, y: 44.8, label: "Surgical Field" },
      { id: "mayo_recovery_zone", attached_to: "mayo_stand", x: 61.5, y: 66.5, label: "Recovery Zone" },
      { id: "mayo_reuse_zone", attached_to: "mayo_stand", x: 76.5, y: 66.5, label: "Reuse Zone" },
      { id: "unknown_zone_anchor", attached_to: "unknown_zone", x: 93, y: 66, label: "Unresolved" },
      ...Array.from({ length: slotCount }, (_, index) => ({
        id: `main_tray_slot_${index + 1}`,
        attached_to: "instrument_rack",
        x: 14.5 + (index % RACK_SLOT_COLUMNS) * 9,
        y: 51.5 + Math.floor(index / RACK_SLOT_COLUMNS) * 6.2,
      })),
    ],
  };
}

function layoutWithSelectedMetadata(
  layout: LayoutBundle,
  metadata: LayoutDisplayMetadata | undefined,
  bundleName: string,
): LayoutBundle {
  const selectedMetadata = selectedBundleMetadata(metadata, bundleName);
  return selectedMetadata ? { ...layout, metadata: selectedMetadata } : layout;
}

function layoutJsonProcedureId(layoutJson: string | undefined): string {
  if (!layoutJson) return "";
  try {
    const parsed = JSON.parse(layoutJson) as unknown;
    return isLayoutBundle(parsed) ? parsed.metadata?.procedure?.id ?? "" : "";
  } catch {
    return "";
  }
}

function placeholderInstrumentStates(metadata: LayoutDisplayMetadata | undefined): InstrumentState[] {
  const instruments = metadata?.instruments ?? [];
  return instruments.map((instrument, index) => {
    const slotId = `main_tray_slot_${index + 1}`;
    return {
      instrument_id: instrument.id,
      home_location_type: "tray_slot",
      home_location_id: slotId,
      location_type: "tray_slot",
      location_id: slotId,
      owner: "rack",
      status: "ready",
      confidence: 1,
      cleanliness_state: "ready",
      contaminated: false,
      reserved_for: "",
      last_holder: "",
      lifecycle_stage: "home",
      next_required_transition: "",
      visual_anchor_id: slotId,
    };
  });
}

function runtimeLayout(bundleName: string, state: SimulationState): LayoutBundle {
  const curated = layouts[bundleName];
  if (state.layout_json) {
    try {
      const parsed = JSON.parse(state.layout_json) as unknown;
      if (isLayoutBundle(parsed)) {
        const parsedProcedureId = parsed.metadata?.procedure?.id ?? "";
        if (!bundleName || !parsedProcedureId || parsedProcedureId === bundleName) {
          return parsed;
        }
        const selected = bundleMetadataFor(parsed.metadata, bundleName);
        const fallback = curated ?? genericProcedureLayout(bundleName, selected?.instruments?.length);
        return layoutWithSelectedMetadata(fallback, parsed.metadata, bundleName);
      }
    } catch {
      // Fall back to the curated bundle below when ROS sends a partial frame during startup.
    }
  }
  if (curated) {
    return curated;
  }
  return genericProcedureLayout(bundleName);
}

type LocalizedDisplayRecord = {
  id?: string;
  display_name?: string;
  display_name_ko?: string;
};

function localizedDisplayName(
  record: LocalizedDisplayRecord | undefined,
  language: Language,
  fallback: string,
): string {
  if (!record) return fallback;
  const localized = language === "ko" ? record.display_name_ko || record.display_name : record.display_name || record.display_name_ko;
  return localized?.trim() || fallback;
}

function phaseIdList(values: unknown): string[] {
  return Array.isArray(values) ? values.map((value) => String(value)).filter(Boolean) : [];
}

function interruptPhaseIdsFromMetadata(metadata: LayoutDisplayMetadata | undefined): string[] {
  const declared = phaseIdList(metadata?.interrupt_phase_ids);
  if (declared.length) return declared;
  return (metadata?.phases ?? [])
    .filter((phase) => `${phase.id} ${phase.display_name ?? ""}`.toLowerCase().includes("interrupt"))
    .map((phase) => phase.id);
}

function normalPhaseIdsFromMetadata(metadata: LayoutDisplayMetadata | undefined, interruptIds: Set<string>): string[] {
  const declared = phaseIdList(metadata?.normal_phase_ids).filter((phaseId) => !interruptIds.has(phaseId));
  if (declared.length) return declared;
  return (metadata?.phases ?? []).map((phase) => phase.id).filter((phaseId) => !interruptIds.has(phaseId));
}

function catalogEntry(
  catalog: DisplayCatalog | undefined,
  section: keyof DisplayCatalog,
  key: string,
): DisplayCatalogEntry | undefined {
  return key ? catalog?.[section]?.[key] : undefined;
}

function catalogLabel(
  catalog: DisplayCatalog | undefined,
  section: keyof DisplayCatalog,
  key: string,
  language: Language,
  fallback = titleize(key),
): string {
  return localizedDisplayName(catalogEntry(catalog, section, key), language, fallback);
}

function catalogToolDisplayState(entry: DisplayCatalogEntry | undefined): StageToolDisplayState {
  const state = entry?.tool_display_state;
  return state === "handover" || state === "using" || state === "recovery" || state === "cleaning" ? state : "waiting";
}

function catalogToolTone(entry: DisplayCatalogEntry | undefined): StageToolTone {
  const tone = entry?.tool_tone;
  return tone === "active" || tone === "surgeon" || tone === "cleaning" || tone === "recovery" || tone === "danger"
    ? tone
    : "ready";
}

function catalogBadgeTone(entry: DisplayCatalogEntry | undefined): StageToolChipBadge["tone"] {
  const tone = entry?.badge_tone;
  return tone === "active" || tone === "warning" || tone === "danger" ? tone : "neutral";
}

function detailString(detail: Record<string, unknown>, key: string): string {
  const value = detail[key];
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function detailPercent(detail: Record<string, unknown>, key: string): string {
  const value = detail[key];
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "";
}

function displayTransition(value: string, catalog: DisplayCatalog | undefined, language: Language): string {
  if (!value) return "";
  return value
    .split(/\s*(?:->|=>)\s*/)
    .filter(Boolean)
    .map((segment) => catalogLabel(catalog, "transitions", segment, language, titleize(segment)))
    .join(" -> ");
}

function anchorLabel(anchor: LayoutAnchor, language: Language): string {
  return localizedDisplayName(anchor, language, anchor.label ?? titleize(anchor.id));
}

function toolShortLabel(label: string): string {
  const clean = label.replace(/\(.+\)/g, "").trim();
  const tokens = clean.split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return label.slice(0, 3).toUpperCase();
  if (tokens.length === 1) return tokens[0].slice(0, 3).toUpperCase();
  return tokens.map((token) => token[0]).join("").slice(0, 3).toUpperCase();
}

function routeLabel(kind: string, catalog: DisplayCatalog | undefined, language: Language): string {
  return catalogLabel(catalog, "transitions", kind, language, titleize(kind));
}

function eventSeverity(
  eventType: string,
  detail: Record<string, unknown>,
  catalog: DisplayCatalog | undefined,
): TimelineItem["severity"] {
  const detailSeverity = detailString(detail, "severity");
  if (detailSeverity === "error" || detailSeverity === "warning" || detailSeverity === "normal") return detailSeverity;
  const catalogSeverity = catalogEntry(catalog, "events", eventType)?.severity;
  return catalogSeverity === "error" || catalogSeverity === "warning" ? catalogSeverity : "normal";
}

function eventTone(
  eventType: string,
  detail: Record<string, unknown>,
  catalog: DisplayCatalog | undefined,
): TimelineItem["tone"] {
  const detailTone = detailString(detail, "tone");
  if (detailTone === "robot" || detailTone === "surgeon" || detailTone === "cleaning" || detailTone === "warning") {
    return detailTone;
  }
  const catalogTone = catalogEntry(catalog, "events", eventType)?.tone;
  if (catalogTone === "robot" || catalogTone === "surgeon" || catalogTone === "cleaning" || catalogTone === "warning") {
    return catalogTone;
  }
  return eventSeverity(eventType, detail, catalog) === "error" ? "warning" : "neutral";
}

function readableEventTitle(
  event: SimulationEvent,
  catalog: DisplayCatalog | undefined,
  language: Language,
  toolNameForId: (instrumentId: string) => string,
): string {
  const detail = parseEventDetail(event.detail);
  const eventLabel = catalogLabel(catalog, "events", event.event_type, language, titleize(event.event_type));
  const toolId = event.instrument_id || detailString(detail, "tool_id") || detailString(detail, "instrument_id");
  return toolId ? `${toolNameForId(toolId)} · ${eventLabel}` : eventLabel;
}

function readableEventMeta(
  event: SimulationEvent,
  catalog: DisplayCatalog | undefined,
  language: Language,
  toolNameForId: (instrumentId: string) => string,
  anchorNameForId: (anchorId: string) => string,
): string {
  const detail = parseEventDetail(event.detail);
  const toolId = event.instrument_id || detailString(detail, "tool_id") || detailString(detail, "instrument_id") || detailString(detail, "requested_tool");
  const tool = toolId ? toolNameForId(toolId) : "";
  const voice = detailString(detail, "voice_text") || detailString(detail, "text") || detailString(detail, "transcript");
  const proposal =
    detailString(detail, "proposal") ||
    detailString(detail, "proposed_transition") ||
    detailString(detail, "proposed_lifecycle") ||
    detailString(detail, "transition") ||
    detailString(detail, "proposal_id");
  const reason =
    detailString(detail, "reason") ||
    detailString(detail, "reducer_reason") ||
    detailString(detail, "blocking_guard") ||
    detailString(detail, "note");
  const confidence = detailPercent(detail, "confidence");
  const source = anchorNameForId(event.from_anchor || detailString(detail, "source") || detailString(detail, "source_anchor_id"));
  const target = anchorNameForId(event.to_anchor || detailString(detail, "target") || detailString(detail, "target_anchor_id"));
  const action = detailString(detail, "action") || detailString(detail, "task_type") || detailString(detail, "skill");
  const commandId = detailString(detail, "command_id") || detailString(detail, "task_id");
  const parts: string[] = [];

  if (language === "ko") {
    if (tool) parts.push(`도구: ${tool}`);
    if (voice) parts.push(`음성: "${voice}"`);
    if (proposal) parts.push(`제안: ${displayTransition(proposal, catalog, language)}`);
    if (reason) parts.push(`사유: ${reason}`);
    if (source || target) parts.push(`경로: ${source || "?"} -> ${target || "?"}`);
    if (action) parts.push(`작업: ${catalogLabel(catalog, "actions", action, language, titleize(action))}`);
    if (commandId) parts.push(`ID: ${commandId.slice(0, 12)}`);
    if (event.arm) parts.push(`팔: ${event.arm}`);
    if (confidence) parts.push(`신뢰도: ${confidence}`);
    if (event.status) parts.push(`상태: ${event.status}`);
  } else {
    if (tool) parts.push(`Tool: ${tool}`);
    if (voice) parts.push(`Voice: "${voice}"`);
    if (proposal) parts.push(`Proposal: ${displayTransition(proposal, catalog, language)}`);
    if (reason) parts.push(`Reason: ${reason}`);
    if (source || target) parts.push(`Route: ${source || "?"} -> ${target || "?"}`);
    if (action) parts.push(`Task: ${catalogLabel(catalog, "actions", action, language, titleize(action))}`);
    if (commandId) parts.push(`ID: ${commandId.slice(0, 12)}`);
    if (event.arm) parts.push(`Arm: ${event.arm}`);
    if (confidence) parts.push(`Confidence: ${confidence}`);
    if (event.status) parts.push(`Status: ${event.status}`);
  }

  return parts.join(" · ") || catalogLabel(catalog, "events", event.event_type, language, titleize(event.event_type));
}

function normalizeCleanerBoardGroup(
  groups: Record<StageHolderId, InstrumentState[]>,
  activeToolId: string,
): Record<StageHolderId, InstrumentState[]> {
  const cleanerTools = groups.cleaner ?? [];
  if (cleanerTools.length <= 1) return groups;

  const normalized: Record<StageHolderId, InstrumentState[]> = { ...groups, cleaner: [...cleanerTools] };
  normalized.cleaner.sort((left, right) => {
    const leftActive = left.instrument_id === activeToolId ? 0 : 1;
    const rightActive = right.instrument_id === activeToolId ? 0 : 1;
    if (leftActive !== rightActive) return leftActive - rightActive;
    const leftCleaning = left.lifecycle_stage === "cleaning_left" ? 0 : 1;
    const rightCleaning = right.lifecycle_stage === "cleaning_left" ? 0 : 1;
    if (leftCleaning !== rightCleaning) return leftCleaning - rightCleaning;
    return left.instrument_id.localeCompare(right.instrument_id);
  });

  const [visibleCleanerTool, ...queuedTools] = normalized.cleaner;
  normalized.cleaner = visibleCleanerTool ? [visibleCleanerTool] : [];
  normalized.mayo_recovery = [...(normalized.mayo_recovery ?? []), ...queuedTools];
  return normalized;
}

function holderShortLabel(holderId: StageHolderId, language: Language): string {
  if (holderId === "rack") return language === "ko" ? "랙" : "Rack";
  if (holderId === "cleaner") return language === "ko" ? "클리너" : "Cleaner";
  if (holderId === "surgeon") return language === "ko" ? "집도의" : "Surgeon";
  if (holderId === "humanoid_left") return language === "ko" ? "왼손" : "Left";
  if (holderId === "humanoid_right") return language === "ko" ? "오른손" : "Right";
  if (holderId === "mayo_recovery") return language === "ko" ? "회수" : "Recovery";
  return language === "ko" ? "재사용" : "Reuse";
}

function buildHolderRects(stageAspectRatio = DEFAULT_STAGE_ASPECT_RATIO): {
  holderRects: Record<StageHolderId, StageHolderRect>;
  holderContentRects: Record<StageHolderId, StageHolderRect>;
  humanoidGroupRect: StageHolderRect;
  surgicalBedRect: StageHolderRect;
  mayoStandRect: StageHolderRect;
} {
  const bedBottom = MAYO_STAND_TOP - STAGE_GAP * stageAspectRatio;
  const mayoStandRect = BOARD_MAYO_STAND_RECT;
  const mayoLaneTop = BASE_HOLDER_RECTS.cleaner.top;
  const laneWidth = (mayoStandRect.width - MAYO_PAD_X * 2 - MAYO_LANE_GAP) / 2;
  const mayoLaneHeight = SINGLE_TOOL_HOLDER_H;
  const humanoidGroupRect = {
    ...BOARD_HUMANOID_GROUP_RECT,
    height: bedBottom - BOARD_HUMANOID_GROUP_RECT.top,
  };
  const humanoidPanelTop = humanoidGroupRect.top + HUMANOID_GROUP_TITLE_SPACE;
  const humanoidPanelHeight =
    (humanoidGroupRect.top +
      humanoidGroupRect.height -
      humanoidPanelTop -
      HUMANOID_ACTION_GAP * 2 -
      HUMANOID_GROUP_BOTTOM_PAD) /
    3;
  const humanoidLeftRect = {
    left: HUMANOID_LEFT,
    top: humanoidPanelTop,
    width: HUMANOID_HOLDER_W,
    height: humanoidPanelHeight,
  };
  const humanoidRightRect = {
    left: HUMANOID_LEFT,
    top: humanoidPanelTop + (humanoidPanelHeight + HUMANOID_ACTION_GAP) * 2,
    width: HUMANOID_HOLDER_W,
    height: humanoidPanelHeight,
  };
  const holderRects: Record<StageHolderId, StageHolderRect> = {
    rack: BASE_HOLDER_RECTS.rack,
    humanoid_left: humanoidLeftRect,
    humanoid_right: humanoidRightRect,
    surgeon: { left: SURGEON_LEFT, top: STAGE_TOP, width: SURGEON_WIDTH, height: bedBottom - STAGE_TOP },
    cleaner: BASE_HOLDER_RECTS.cleaner,
    mayo_recovery: {
      left: mayoStandRect.left + MAYO_PAD_X,
      top: mayoLaneTop,
      width: laneWidth,
      height: mayoLaneHeight,
    },
    mayo_reuse: {
      left: mayoStandRect.left + MAYO_PAD_X + laneWidth + MAYO_LANE_GAP,
      top: mayoLaneTop,
      width: laneWidth,
      height: mayoLaneHeight,
    },
  };
  return {
    holderRects,
    holderContentRects: Object.fromEntries(
      Object.entries(holderRects).map(([holderId, rect]) => [
        holderId,
        contentRectForHolder(holderId as StageHolderId, rect),
      ]),
    ) as Record<StageHolderId, StageHolderRect>,
    humanoidGroupRect,
    surgicalBedRect: {
      ...BOARD_SURGICAL_BED_RECT,
      height: bedBottom - BOARD_SURGICAL_BED_RECT.top,
    },
    mayoStandRect,
  };
}

function runtimeStateLabel(state: SimulationState, ui: ReturnType<typeof getUiCopy>): string {
  if (state.execution_state === "starting") return ui.starting;
  if (state.execution_state === "running" && state.running) return ui.running;
  if (state.execution_state === "paused" && state.running) return ui.paused;
  if (state.execution_state === "finishing") return ui.finishing;
  if (state.execution_state === "completed") return ui.completed;
  if (state.execution_state === "halted") return ui.halted;
  if (state.execution_state === "idle") return ui.idle;
  return state.execution_state || ui.idle;
}

function runtimeStatusText(state: SimulationState, ui: ReturnType<typeof getUiCopy>, procedure: string, language: Language): string {
  if (state.execution_state === "starting") {
    return language === "ko" ? "시뮬레이션 시작을 준비하고 있습니다." : "Simulation is preparing to start.";
  }
  if (state.execution_state === "running" && state.running) {
    return language === "ko" ? `${procedure} 시뮬레이션 실행 중` : `Simulation running on ${procedure}`;
  }
  if (state.execution_state === "paused" && state.running) {
    return language === "ko" ? `${procedure} 시뮬레이션 일시정지` : `Simulation paused on ${procedure}`;
  }
  if (state.execution_state === "finishing") {
    return language === "ko" ? "수술 종료 정리 절차를 진행 중입니다." : "Cleanup is running before completion.";
  }
  if (state.execution_state === "completed") {
    return language === "ko" ? "수술 및 정리 절차가 완료되었습니다." : "Procedure and cleanup completed.";
  }
  if (state.execution_state === "halted" && !state.running) {
    return language === "ko" ? "시뮬레이션이 정지되었습니다." : "Simulation stopped.";
  }
  if (state.execution_state === "idle" && !state.running) {
    return language === "ko" ? "시뮬레이션 런타임이 대기 상태로 초기화되었습니다." : "Simulation runtime reset to idle.";
  }
  return ui.idle;
}

export function useDigitalTwinViewModel({
  language,
  activeBundle,
  simulationState,
  skillStatus,
  surgeonState,
  events,
  overrideAck,
  vlmHealth,
  vlmResult,
  vlmHealthReceivedAt,
  vlmResultReceivedAt,
  stageAspectRatio = DEFAULT_STAGE_ASPECT_RATIO,
}: {
  language: Language;
  activeBundle: string;
  simulationState: SimulationState;
  skillStatus: SkillStatus;
  surgeonState: SurgeonState;
  events: SimulationEvent[];
  overrideAck: OverrideAck | null;
  vlmHealth: VLMHealth;
  vlmResult: VLMResult;
  vlmHealthReceivedAt: number | null;
  vlmResultReceivedAt: number | null;
  stageAspectRatio?: number;
}) {
  const lastNormalPhaseRef = useRef("");
  const viewModel = useMemo(() => {
    const ui = getUiCopy(language);
    const logicalLayout = runtimeLayout(activeBundle, simulationState);
    const layout = applyVisualLayout(activeBundle, logicalLayout);
    const metadata = layout.metadata;
    const catalog = metadata?.display_catalog;
    const phaseDisplayById = new Map((metadata?.phases ?? []).map((phase) => [phase.id, phase]));
    const toolDisplayById = new Map((metadata?.instruments ?? []).map((instrument) => [instrument.id, instrument]));
    const bundleDisplayById = new Map((metadata?.bundles ?? []).map((bundle) => [bundle.id, bundle]));
    const localizedToolName = (instrumentId: string) =>
      localizedDisplayName(toolDisplayById.get(instrumentId), language, displayToolName(instrumentId, language));
    const localizedPhaseName = (phaseId: string) =>
      localizedDisplayName(phaseDisplayById.get(phaseId), language, displayPhaseName(phaseId, language));
    const localizedBundleName = (bundleName: string) =>
      localizedDisplayName(bundleDisplayById.get(bundleName), language, titleize(bundleName));
    const localizedActionName = (actionId: string) => catalogLabel(catalog, "actions", actionId, language, titleize(actionId));
    const localizedSkillStateName = (stateId: string) =>
      catalogLabel(catalog, "skill_states", stateId, language, titleize(stateId));
    const localizedTransitionName = (transitionId: string) =>
      catalogLabel(catalog, "transitions", transitionId, language, titleize(transitionId));
    const localizedLifecycleName = (lifecycleId: string) =>
      catalogLabel(catalog, "lifecycle", lifecycleId, language, titleize(lifecycleId));
    const localizedProcedureName = localizedDisplayName(
      metadata?.procedure,
      language,
      localizedBundleName(activeBundle),
    );
    const anchorMap = Object.fromEntries(layout.anchors.map((anchor) => [anchor.id, anchor])) as Record<
      string,
      LayoutAnchor
    >;
    const entityMap = Object.fromEntries(layout.entities.map((entity) => [entity.id, entity])) as Record<
      string,
      LayoutEntity
    >;
    const fieldAnchor = layout.anchors.find((anchor) => anchor.id.startsWith("field_region"));
    const unknownAnchor = anchorMap.unknown_zone_anchor;
    const anchorNameForId = (anchorId: string) => {
      const anchor = anchorMap[anchorId];
      return anchor ? anchorLabel(anchor, language) : anchorId ? titleize(anchorId) : "";
    };
    const bundleOptions: BundleOption[] = metadata?.bundles?.length
      ? metadata.bundles.map((bundle) => ({
          id: bundle.id,
          label: localizedDisplayName(bundle, language, titleize(bundle.id)),
        }))
      : activeBundle
        ? [{ id: activeBundle, label: localizedBundleName(activeBundle) }]
        : [];
    const requestableInstrumentIds = metadata?.requestable_instruments?.length
      ? metadata.requestable_instruments
      : (metadata?.instruments ?? []).filter((instrument) => instrument.requestable !== false).map((instrument) => instrument.id);
    const requestableTools: RequestableToolOption[] = requestableInstrumentIds
      .filter((instrumentId) => toolDisplayById.has(instrumentId))
      .map((instrumentId) => {
        const instrument = toolDisplayById.get(instrumentId);
        const alias = instrument?.aliases?.find(Boolean) ?? instrument?.display_name ?? instrumentId;
        return {
          id: instrumentId,
          label: localizedToolName(instrumentId),
          voicePrompt: `${alias} please`,
        };
      });
    const holderLabels: Record<StageHolderId, string> = {
      rack: ui.instrumentRack,
      humanoid_left: language === "ko" ? "왼손" : "Left Hand",
      humanoid_right: language === "ko" ? "오른손" : "Right Hand",
      surgeon: ui.surgeon,
      cleaner: ui.cleanerStation,
      mayo_recovery: ui.recovery,
      mayo_reuse: ui.reuse,
    };
    const labelForHolder = (holderId: StageHolderId) => holderLabels[holderId];
    const runtimeBundleId = simulationState.active_bundle || simulationState.procedure_id || "";
    const runtimeStateMatchesActiveBundle = !activeBundle || !runtimeBundleId || runtimeBundleId === activeBundle;
    const runtimeFrameProcedureId = layoutJsonProcedureId(simulationState.layout_json);
    const runtimeFrameMatchesActiveBundle =
      runtimeStateMatchesActiveBundle && (!activeBundle || !runtimeFrameProcedureId || runtimeFrameProcedureId === activeBundle);
    const metadataInstrumentStates = placeholderInstrumentStates(metadata);
    const displayInstrumentStates =
      runtimeFrameMatchesActiveBundle && simulationState.instrument_states.length
        ? simulationState.instrument_states
        : metadataInstrumentStates;
    const displayEvents = runtimeFrameMatchesActiveBundle ? events : [];
    const displayRecentEvents = runtimeFrameMatchesActiveBundle ? simulationState.recent_events : [];
    const runtimeAllowsActiveTask =
      runtimeFrameMatchesActiveBundle && simulationState.running && simulationState.execution_state !== "completed";

    const surgeonOwnedInstruments = displayInstrumentStates.filter(
      (instrument) =>
        instrument.lifecycle_stage === "surgeon_owned" ||
        instrument.lifecycle_stage === "mayo_reuse",
    );
    const requestedSurgeonToolId = simulationState.surgeon_request_tool || surgeonState.requested_tool || "";
    const retrievalTargetToolId =
      surgeonState.ready_for_retrieval && requestedSurgeonToolId
        ? surgeonOwnedInstruments.find((instrument) => instrument.instrument_id === requestedSurgeonToolId)?.instrument_id ?? ""
        : "";
    const surgeonLeftInstrumentId = surgeonState.ready_for_retrieval
      ? retrievalTargetToolId || surgeonOwnedInstruments[0]?.instrument_id || ""
      : surgeonOwnedInstruments[1]?.instrument_id || "";
    const surgeonRightInstrumentId =
      surgeonOwnedInstruments.find((instrument) => instrument.instrument_id !== surgeonLeftInstrumentId)?.instrument_id ?? "";

    function anchorForInstrument(instrument: InstrumentState): LayoutAnchor | undefined {
      if (instrument.visual_anchor_id && anchorMap[instrument.visual_anchor_id]) {
        return anchorMap[instrument.visual_anchor_id];
      }
      if (instrument.instrument_id === surgeonRightInstrumentId && anchorMap.surgeon_right_hand) {
        return anchorMap.surgeon_right_hand;
      }
      if (instrument.instrument_id === surgeonLeftInstrumentId && anchorMap.surgeon_left_hand) {
        return anchorMap.surgeon_left_hand;
      }
      if (instrument.lifecycle_stage === "prepositioned_right" && anchorMap.robot_right_hand) return anchorMap.robot_right_hand;
      if (instrument.lifecycle_stage === "recovering_left" && anchorMap.robot_left_hand) return anchorMap.robot_left_hand;
      if (instrument.lifecycle_stage === "cleaned_left" && anchorMap.robot_left_hand) return anchorMap.robot_left_hand;
      if (instrument.lifecycle_stage === "cleaning_left" && anchorMap.cleaner_slot) return anchorMap.cleaner_slot;
      if (instrument.lifecycle_stage === "mayo_recovery" && anchorMap.mayo_recovery_zone) return anchorMap.mayo_recovery_zone;
      if (instrument.lifecycle_stage === "mayo_reuse" && anchorMap.mayo_reuse_zone) return anchorMap.mayo_reuse_zone;
      if (instrument.lifecycle_stage === "surgeon_owned" && fieldAnchor) return fieldAnchor;

      const byLocationType =
        (instrument.location_type === "robot_right_hand" && anchorMap.robot_right_hand) ||
        (instrument.location_type === "robot_left_hand" && anchorMap.robot_left_hand) ||
        (instrument.location_type === "surgeon_hand" && anchorMap.surgeon_hand) ||
        (instrument.location_type === "surgical_field" && fieldAnchor) ||
        (instrument.location_type === "mayo_recovery_zone" && anchorMap.mayo_recovery_zone) ||
        (instrument.location_type === "mayo_reuse_zone" && anchorMap.mayo_reuse_zone) ||
        (instrument.location_type === "mayo_stand" && anchorMap.mayo_reuse_zone) ||
        (instrument.location_type === "return_zone" && anchorMap.surgeon_return_zone) ||
        (instrument.location_type === "handover_zone" && anchorMap.surgeon_receive_zone) ||
        (instrument.location_type === "cleaner_slot" && anchorMap.cleaner_slot);

      return anchorMap[instrument.location_id] ?? byLocationType ?? anchorMap[instrument.home_location_id] ?? unknownAnchor;
    }

    const grouped = displayInstrumentStates.reduce(
      (groups, instrument) => {
        const anchor = anchorForInstrument(instrument);
        if (!anchor) return groups;
        if (!groups[anchor.id]) groups[anchor.id] = { anchor, instruments: [] };
        groups[anchor.id].instruments.push(instrument);
        return groups;
      },
      {} as Record<string, { anchor: LayoutAnchor; instruments: InstrumentState[] }>,
    );

    const activeToolId = runtimeAllowsActiveTask
      ? simulationState.active_robot_task_tool_id ||
        simulationState.right_hand_tool ||
        simulationState.left_hand_tool ||
        simulationState.prepositioned_tool ||
        overrideAck?.toolId ||
        requestedSurgeonToolId ||
        ""
      : "";

    const tools: StageTool[] = Object.values(grouped).flatMap(({ anchor, instruments }) =>
      [...instruments]
        .sort((left, right) => left.instrument_id.localeCompare(right.instrument_id))
        .map((instrument, index) => {
          const label = localizedToolName(instrument.instrument_id);
          const compact = anchor.id.startsWith("main_tray_slot") || instrument.lifecycle_stage === "home";
          return {
            id: instrument.instrument_id,
            label,
            shortLabel: toolShortLabel(label),
            anchorId: anchor.id,
            point: fanOutAnchorPoint(anchor, index, compact),
            lifecycle: catalogLabel(catalog, "lifecycle", instrument.lifecycle_stage, language, titleize(instrument.lifecycle_stage)).toUpperCase(),
            tone: catalogToolTone(catalogEntry(catalog, "lifecycle", instrument.lifecycle_stage)),
            contaminated: instrument.contaminated,
            active: instrument.instrument_id === activeToolId,
            compact,
          };
        }),
    );

    const leftHandInstrument = simulationState.left_hand_tool
      ? displayInstrumentStates.find((instrument) => instrument.instrument_id === simulationState.left_hand_tool)
      : undefined;
    const returnAnchorId = leftHandInstrument?.home_location_id || "main_tray_slot_1";
    const routes: StageRoute[] = [];
    const pushRoute = (fromId: string, toId: string, kind: string, progress = simulationState.active_robot_task_progress || 0) => {
      const source = anchorMap[fromId];
      const target = anchorMap[toId];
      if (source && target) {
        routes.push({ source, target, kind, progress, label: routeLabel(kind, catalog, language) });
      }
    };

    if (
      runtimeAllowsActiveTask &&
      simulationState.active_robot_task_id &&
      simulationState.active_robot_task_source_anchor &&
      simulationState.active_robot_task_target_anchor
    ) {
      pushRoute(
        simulationState.active_robot_task_source_anchor,
        simulationState.active_robot_task_target_anchor,
        simulationState.active_robot_task_type || "active_task",
      );
    } else if (runtimeAllowsActiveTask && simulationState.cleaner_busy) {
      pushRoute("robot_left_hand", "cleaner_slot", "cleaning", 0.62);
    } else if (runtimeAllowsActiveTask && simulationState.left_hand_tool) {
      pushRoute(
        leftHandInstrument?.cleanliness_state === "ready" ? "cleaner_slot" : "mayo_recovery_zone",
        leftHandInstrument?.cleanliness_state === "ready" ? returnAnchorId : "robot_left_hand",
        leftHandInstrument?.cleanliness_state === "ready" ? "auto_return" : "recovery",
        0.48,
      );
    }
    if (runtimeAllowsActiveTask && (simulationState.surgeon_ready_for_handover || requestedSurgeonToolId)) {
      pushRoute("robot_right_hand", "surgeon_receive_zone", "handover", 0.72);
    } else if (runtimeAllowsActiveTask && simulationState.prepositioned_tool && fieldAnchor) {
      pushRoute("robot_right_hand", fieldAnchor.id, "anticipatory", 0.45);
    }

    const rawBoardGrouped = Object.values(grouped).reduce(
      (groups, { anchor, instruments }) => {
        for (const instrument of instruments) {
          const holderId = holderIdForAnchor(anchor.id, instrument.lifecycle_stage, instrument.location_type);
          if (!groups[holderId]) groups[holderId] = [];
          groups[holderId].push(instrument);
        }
        return groups;
      },
      {} as Record<StageHolderId, InstrumentState[]>,
    );
    const boardGrouped = normalizeCleanerBoardGroup(rawBoardGrouped, activeToolId);
    const { holderRects, holderContentRects, humanoidGroupRect, surgicalBedRect, mayoStandRect } = buildHolderRects(stageAspectRatio);
    const rackSlotIds = Array.from({ length: RACK_SLOT_COUNT }).map((_, index) => `main_tray_slot_${index + 1}`);
    const boardRackSlotCount = rackSlotIds.length;
    const rackSlotIndexById = new Map(rackSlotIds.map((slotId, index) => [slotId, index]));
    const instrumentByHomeSlot = new Map(
      displayInstrumentStates
        .filter((instrument) => instrument.home_location_id)
        .map((instrument) => [instrument.home_location_id, instrument]),
    );
    const rackOccupiedInstrumentIds = new Set((boardGrouped.rack ?? []).map((instrument) => instrument.instrument_id));
    const boardRackSlots: StageRackSlot[] = rackSlotIds.map((slotId, index) => {
      const instrument = instrumentByHomeSlot.get(slotId);
      const label = instrument ? localizedToolName(instrument.instrument_id) : titleize(slotId);
      return {
        id: slotId,
        instrumentId: instrument?.instrument_id ?? "",
        label,
        shortLabel: instrument ? toolShortLabel(label) : `${index + 1}`,
        occupied: instrument ? rackOccupiedInstrumentIds.has(instrument.instrument_id) : false,
        rect: rackSlotRect(index),
      };
    });
    const activeRequestIntent =
      runtimeAllowsActiveTask &&
      [simulationState.surgeon_intent, surgeonState.intent, overrideAck?.eventType ?? ""].some(
        (intent) => intent === "request_tool" || intent === "voice_request" || intent === "extend_hand_for_handover",
      );
    const requestedHighlightToolId = activeRequestIntent ? overrideAck?.toolId || requestedSurgeonToolId : "";
    const activeRecoveryToolIds = new Set(runtimeFrameMatchesActiveBundle ? simulationState.active_recovery_tools ?? [] : []);
    const toolChipPlacements: StageToolChipPlacement[] = BOARD_HOLDER_ORDER.flatMap((holderId) => {
      const instruments = [...(boardGrouped[holderId] ?? [])].sort((left, right) => {
        if (holderId === "rack") {
          const leftIndex = rackSlotIndexById.get(left.home_location_id) ?? Number.MAX_SAFE_INTEGER;
          const rightIndex = rackSlotIndexById.get(right.home_location_id) ?? Number.MAX_SAFE_INTEGER;
          return leftIndex - rightIndex || left.instrument_id.localeCompare(right.instrument_id);
        }
        const leftKey = `${left.home_location_id}-${left.location_id}-${left.instrument_id}`;
        const rightKey = `${right.home_location_id}-${right.location_id}-${right.instrument_id}`;
        return leftKey.localeCompare(rightKey);
      });
      return instruments.map((instrument, index) => {
        const label = localizedToolName(instrument.instrument_id);
        const placementIndex = holderId === "rack" ? rackSlotIndexById.get(instrument.home_location_id) ?? index : index;
          const rect = chipRectForHolder(
            holderId,
            placementIndex,
            holderId === "rack" ? boardRackSlotCount : instruments.length,
            holderRects,
          );
          const active = instrument.instrument_id === activeToolId;
          const displayState = displayStateForInstrument(instrument, catalog, activeRecoveryToolIds);
        const requested =
          holderId === "humanoid_right" &&
          displayState === "handover" &&
          Boolean(requestedHighlightToolId) &&
          instrument.instrument_id === requestedHighlightToolId;
        const predictionEligible =
          holderId === "humanoid_right" &&
          displayState === "handover";
        const predicted =
          !requested && predictionEligible && (active || instrument.instrument_id === simulationState.prepositioned_tool);
        const footerBadges = footerBadgesForInstrument(instrument, catalog, language, ui, activeRecoveryToolIds);
        return {
          id: instrument.instrument_id,
          label,
          shortLabel: toolShortLabel(label),
          holderId,
          holderLabel: holderShortLabel(holderId, language),
          left: rect.left,
          top: rect.top,
          width: rect.width,
          height: rect.height,
          scale: rect.scale,
          compact: rect.compact,
          gridIndex: rect.gridIndex,
          density: toolChipDensityForHolder(holderId, instruments.length, rect.compact),
          displayState,
          highlight: requested ? "requested" : predicted ? "predicted" : "normal",
          lifecycle: catalogLabel(
            catalog,
            "lifecycle",
            displayLifecycleForInstrument(instrument, activeRecoveryToolIds),
            language,
            titleize(displayLifecycleForInstrument(instrument, activeRecoveryToolIds)),
          ),
          footerBadges,
          contaminated: instrument.contaminated,
          active,
          layoutVariant: holderId === "mayo_recovery" || holderId === "mayo_reuse" ? "mayoList" : "card",
        };
      });
    });

    const activeBoardRoutes: StageBoardRoute[] = routes.flatMap((route) => {
      const sourceHolderId = holderIdForAnchor(route.source.id);
      const targetHolderId = holderIdForAnchor(route.target.id);
      if (sourceHolderId === targetHolderId) return [];
      return [
        {
          sourceHolderId,
          targetHolderId,
          source: holderCenter(sourceHolderId, holderRects),
          target: holderCenter(targetHolderId, holderRects),
          kind: route.kind,
          label: route.label,
          progress: route.progress,
        },
      ];
    });

    const activeVoiceCommand = activeVoiceCommandFromEvents(displayEvents);
    const latestVoiceText = activeVoiceCommand?.text ?? "";
    const activeVoiceText =
      latestVoiceText ||
      (overrideAck?.eventType === "voice_request" ? overrideAck.voiceText || overrideAck.message : "");
    const cleanerCountdown = runtimeAllowsActiveTask && simulationState.cleaner_busy
      ? Math.max(1, Math.ceil(simulationState.cleaner_remaining_sec || 0))
      : 0;
    const surgeonPanelInactive = !runtimeAllowsActiveTask;
    const displayIntent = surgeonPanelInactive ? "idle" : surgeonState.intent || simulationState.surgeon_intent || "idle";
    const displayRequestedTool = surgeonPanelInactive
      ? ""
      : surgeonState.requested_tool || simulationState.surgeon_request_tool || "";
    const displayReadyForHandover =
      !surgeonPanelInactive &&
      (surgeonState.ready_for_handover ||
        simulationState.surgeon_ready_for_handover ||
        overrideAck?.eventType === "request_tool" ||
        overrideAck?.eventType === "voice_request");
    const displayReadyForRetrieval =
      !surgeonPanelInactive &&
      (surgeonState.ready_for_retrieval ||
        simulationState.surgeon_ready_for_retrieval ||
        overrideAck?.eventType === "return_tool");
    const intentBubble =
      activeVoiceText ||
      overrideAck?.message ||
      (displayReadyForRetrieval
        ? `${localizedToolName(displayRequestedTool || activeToolId)} return`
        : displayReadyForHandover
          ? `${localizedToolName(displayRequestedTool || activeToolId)} request`
          : "");

    const interruptPhaseIdSet = new Set(interruptPhaseIdsFromMetadata(metadata));
    const normalPhaseIds = normalPhaseIdsFromMetadata(metadata, interruptPhaseIdSet);
    const rawPhaseId = runtimeFrameMatchesActiveBundle ? simulationState.filtered_phase : normalPhaseIds[0] || "";
    const currentPhaseIsInterrupt = Boolean(rawPhaseId && interruptPhaseIdSet.has(rawPhaseId));
    const recentInterruptEvent = displayRecentEvents.find((event) =>
      event === "InterruptEventDetected" || event.startsWith("InterruptEventDetected:")
    );
    const recentInterruptPhaseId = recentInterruptEvent?.includes(":")
      ? recentInterruptEvent.split(":")[1]
      : "";
    const activeInterruptPhaseId = currentPhaseIsInterrupt
      ? rawPhaseId
      : recentInterruptPhaseId && interruptPhaseIdSet.has(recentInterruptPhaseId)
        ? recentInterruptPhaseId
        : "";
    const surgeonNormalPhaseId =
      runtimeFrameMatchesActiveBundle && normalPhaseIds.includes(surgeonState.phase_id) ? surgeonState.phase_id : "";
    const displayedPhaseId = currentPhaseIsInterrupt
      ? lastNormalPhaseRef.current || surgeonNormalPhaseId || normalPhaseIds[0] || rawPhaseId
      : rawPhaseId;
    const phaseName = localizedPhaseName(displayedPhaseId);
    const interruptLabel = activeInterruptPhaseId ? localizedPhaseName(activeInterruptPhaseId) : "";
    const procedurePhaseSpecs = normalPhaseIds.length
      ? normalPhaseIds.map((phaseId) => ({ id: phaseId }))
      : displayedPhaseId
        ? [{ id: displayedPhaseId }]
        : [];
    const activePhaseIndex = procedurePhaseSpecs.findIndex((phase) => phase.id === displayedPhaseId);
    const phaseSteps: StagePhaseStep[] = procedurePhaseSpecs.map((phase, index) => ({
      id: phase.id,
      label: localizedPhaseName(phase.id),
      state: activePhaseIndex === -1 ? "future" : index < activePhaseIndex ? "past" : index === activePhaseIndex ? "active" : "future",
    }));
    const interruptAlert: StageInterruptAlert | null = activeInterruptPhaseId
      ? {
          phaseId: activeInterruptPhaseId,
          label: interruptLabel,
          title: ui.interruptAlertTitle,
          eventKey: `${activeInterruptPhaseId}:${displayRecentEvents.join("|")}`,
          message:
            language === "ko"
              ? `${interruptLabel} 이벤트를 감지했습니다. 진행 단계는 ${phaseName}로 유지합니다.`
              : `${interruptLabel} event detected. Procedure progress remains at ${phaseName}.`,
        }
      : null;
    const activeHolderIds = new Set<StageHolderId>();
    for (const route of activeBoardRoutes) {
      activeHolderIds.add(route.sourceHolderId);
      activeHolderIds.add(route.targetHolderId);
    }
    for (const chip of toolChipPlacements) {
      if (chip.active || chip.highlight !== "normal") {
        activeHolderIds.add(chip.holderId);
      }
    }
    if (runtimeAllowsActiveTask && cleanerCountdown > 0) activeHolderIds.add("cleaner");
    if (displayReadyForHandover || displayReadyForRetrieval) activeHolderIds.add("surgeon");
    if (runtimeAllowsActiveTask && (simulationState.active_robot_task_id || simulationState.prepositioned_tool)) {
      const arm = `${simulationState.active_robot_task_arm} ${simulationState.active_robot_task_source_anchor} ${simulationState.active_robot_task_target_anchor}`.toLowerCase();
      activeHolderIds.add(arm.includes("left") ? "humanoid_left" : "humanoid_right");
    }

    const skillState = skillStatus.state || "";
    const skillIsInFlight =
      runtimeAllowsActiveTask && Boolean(skillStatus.action) && !TERMINAL_SKILL_STATES.has(skillState);
    const activeActionId = skillIsInFlight
      ? skillStatus.action
      : runtimeAllowsActiveTask
        ? simulationState.active_robot_task_type
        : "";
    const robotTaskLabel =
      runtimeAllowsActiveTask && activeActionId
        ? localizedActionName(activeActionId)
        : runtimeAllowsActiveTask
          ? simulationState.robot_state || ui.idle
          : ui.idle;
    const activeTaskProgress =
      runtimeAllowsActiveTask && simulationState.active_robot_task_id
        ? Math.round((simulationState.active_robot_task_progress || 0) * 100)
        : 0;
    const skillMilestoneLabel =
      skillIsInFlight && skillState
        ? localizedSkillStateName(skillState)
        : runtimeAllowsActiveTask && simulationState.active_robot_task_id
          ? language === "ko"
            ? "진행 중"
            : "In progress"
          : "";
    const activeActionToolId = skillIsInFlight
      ? skillStatus.instrument_id
      : runtimeAllowsActiveTask
        ? simulationState.active_robot_task_tool_id
        : "";
    const humanoidActionToolLabel = activeActionToolId
      ? localizedToolName(activeActionToolId)
      : language === "ko"
        ? "도구 없음"
        : "No tool";
    const humanoidActionMilestone =
      skillMilestoneLabel || (language === "ko" ? "상위 명령 대기" : "Awaiting command");
    const holderBadges: Partial<Record<StageHolderId, StageHolderBadge[]>> = {};
    const pushHolderBadge = (holderId: StageHolderId, badge: StageHolderBadge) => {
      holderBadges[holderId] = [...(holderBadges[holderId] ?? []), badge];
    };
    pushHolderBadge("cleaner", {
      label: cleanerCountdown > 0 ? `${ui.busy} ${cleanerCountdown}s` : ui.idle,
      tone: cleanerCountdown > 0 ? "active" : "neutral",
    });
    const boardHolders: StageHolder[] = BOARD_HOLDER_ORDER.map((holderId) => ({
      id: holderId,
      label: labelForHolder(holderId),
      rect: holderRects[holderId],
      contentRect: holderContentRects[holderId],
      tone: holderTone(holderId),
      active: activeHolderIds.has(holderId),
      meta: undefined,
      badges: holderBadges[holderId],
    }));
    const humanoidActionRect = {
      left: HUMANOID_LEFT,
      top: holderRects.humanoid_left.top + holderRects.humanoid_left.height + HUMANOID_ACTION_GAP,
      width: HUMANOID_HOLDER_W,
      height: holderRects.humanoid_left.height,
    };
    const hasActiveRobotTask =
      runtimeAllowsActiveTask && (Boolean(simulationState.active_robot_task_id) || skillIsInFlight);
    const boardHumanoidGroup: StageHumanoidGroup = {
      label: language === "ko" ? "휴머노이드" : "Humanoid",
      rect: humanoidGroupRect,
      active: hasActiveRobotTask || activeHolderIds.has("humanoid_left") || activeHolderIds.has("humanoid_right"),
      action: {
        title: ui.actionPanel,
        label: hasActiveRobotTask ? robotTaskLabel : ui.idle,
        milestone: humanoidActionMilestone,
        toolLabel: humanoidActionToolLabel,
        active: hasActiveRobotTask,
        rect: humanoidActionRect,
      },
    };
    const boardSurgicalBed: StageSurgicalBed = {
      label: ui.surgicalBed,
      rect: surgicalBedRect,
      phaseLabel: phaseName,
      active: Boolean(
        runtimeAllowsActiveTask &&
          (simulationState.active_robot_task_id ||
            simulationState.prepositioned_tool ||
            displayReadyForHandover ||
            displayReadyForRetrieval),
      ),
    };
    const boardMayoStand: StageMayoStand = {
      label: ui.mayoStand,
      rect: mayoStandRect,
      active: activeHolderIds.has("mayo_recovery") || activeHolderIds.has("mayo_reuse"),
    };
    const boardActionBubbles: StageActionBubble[] = [];
    const surgeonAlertBubbles: StageActionBubble[] = [];
    const surgeonRect = holderRects.surgeon;
    const surgeonAlertLeft = clamp(surgeonRect.left + surgeonRect.width / 2, 9, 89);
    const surgeonAlertTop = clamp(surgeonRect.top + 6.1, surgeonRect.top + 5.2, surgeonRect.top + 7.4);
    const requestToolLabel = localizedToolName(displayRequestedTool || activeToolId);
    if (activeVoiceText) {
      surgeonAlertBubbles.push({
        id: "surgeon-voice",
        title: language === "ko" ? "음성 명령" : "Voice Command",
        text: activeVoiceText,
        tone: "surgeon",
        left: surgeonAlertLeft,
        top: surgeonAlertTop,
        occurredAt: activeVoiceCommand?.occurredAt,
      });
    }
    const queuedHandoverCues = activeHandoverCuesFromEvents(displayEvents);
    if (queuedHandoverCues.length) {
      for (const cue of queuedHandoverCues) {
        const cueToolLabel = localizedToolName(cue.toolId);
        surgeonAlertBubbles.push({
          id: cue.id,
          title: ui.handover,
          text: language === "ko" ? `${cueToolLabel} 전달 큐` : `${cueToolLabel} handover cue`,
          tone: "surgeon",
          left: surgeonAlertLeft,
          top: surgeonAlertTop,
          occurredAt: cue.occurredAt,
        });
      }
    } else if (displayReadyForHandover) {
      surgeonAlertBubbles.push({
        id: `surgeon-handover-ready-${displayRequestedTool || activeToolId || "pending"}`,
        title: ui.handover,
        text:
          displayRequestedTool || activeToolId
            ? language === "ko"
              ? `${requestToolLabel} 전달 큐`
              : `${requestToolLabel} handover cue`
            : language === "ko"
              ? "도구 전달 대기"
              : "Awaiting tool handover",
        tone: "surgeon",
        left: surgeonAlertLeft,
        top: surgeonAlertTop,
        occurredAt: readySignalOccurredAt(displayEvents, "handover", displayRequestedTool || activeToolId),
      });
    }
    if (displayReadyForRetrieval) {
      surgeonAlertBubbles.push({
        id: `surgeon-retrieval-ready-${displayRequestedTool || activeToolId || "pending"}`,
        title: ui.retrieval,
        text:
          displayRequestedTool || activeToolId
            ? language === "ko"
              ? `${requestToolLabel} 회수 큐`
              : `${requestToolLabel} retrieval cue`
            : language === "ko"
              ? "도구 회수 대기"
              : "Awaiting tool retrieval",
        tone: "surgeon",
        left: surgeonAlertLeft,
        top: surgeonAlertTop,
        occurredAt: readySignalOccurredAt(displayEvents, "retrieval", displayRequestedTool || activeToolId),
      });
    }
    boardActionBubbles.push(
      ...surgeonAlertBubbles.sort((a, b) => {
        const timeDelta = (b.occurredAt || 0) - (a.occurredAt || 0);
        if (timeDelta) return timeDelta;
        const priority = new Map([
          ["surgeon-voice", 3],
          ["surgeon-handover-ready", 2],
          ["surgeon-retrieval-ready", 1],
        ]);
        return (priority.get(b.id) ?? 0) - (priority.get(a.id) ?? 0);
      }),
    );
    const boardAudioBubble: StageAudioBubble | undefined = activeVoiceText
      ? {
          title: language === "ko" ? "음성 명령" : "Voice Command",
          text: activeVoiceText,
          tone: "audio",
        }
      : overrideAck?.message
        ? {
            title: language === "ko" ? "집도의 오버라이드" : "Surgeon Override",
            text: overrideAck.message,
            tone: "override",
          }
        : undefined;
    const newestFirstEvents = displayEvents
      .map((event, index) => ({ event, index }))
      .sort((a, b) => compareTimelineEvents(a.event, b.event, a.index, b.index, displayEvents.length))
      .map(({ event }) => event);
    const timeline: TimelineItem[] = newestFirstEvents.length
      ? (() => {
          const seenUiIds = new Map<string, number>();
          return newestFirstEvents.map((event, index) => {
            const detail = parseEventDetail(event.detail);
            const fallbackUiId = `${event.event_type}-${event.instrument_id}-${event.status}-${event.detail}`;
            const baseUiId = event.ui_id ?? fallbackUiId;
            const occurrence = seenUiIds.get(baseUiId) ?? 0;
            seenUiIds.set(baseUiId, occurrence + 1);
            const uniqueUiId = occurrence ? `${baseUiId}-${occurrence}` : baseUiId;
            return {
              id: `${uniqueUiId}-${event.event_type}-${event.instrument_id}-${index}`,
              uiId: uniqueUiId,
              title: readableEventTitle(event, catalog, language, localizedToolName),
              meta: readableEventMeta(event, catalog, language, localizedToolName, anchorNameForId),
              tone: eventTone(event.event_type, detail, catalog),
              severity: eventSeverity(event.event_type, detail, catalog),
            };
          });
        })()
      : displayRecentEvents.slice(0, 8).map((event, index, recentEvents) => {
          const sameEventOrdinalFromTail = recentEvents.slice(index).filter((candidate) => candidate === event).length;
          const stableKey = `${event}-${sameEventOrdinalFromTail}`;
          return {
            id: `recent-${stableKey}`,
            uiId: `recent-${stableKey}`,
            title: catalogLabel(catalog, "events", event, language, titleize(event)),
            meta: activeBundle,
            tone: "neutral" as const,
            severity: eventSeverity(event, {}, catalog),
          };
        });

    const vlmShouldRun =
      simulationState.running || ["starting", "running", "finishing"].includes(simulationState.execution_state);
    const hasVlmHealth = vlmHealthReceivedAt !== null;
    const vlmConnectionLabel = !vlmShouldRun
      ? hasVlmHealth && vlmHealth.connected
        ? language === "ko"
          ? "연결됨"
          : "connected"
        : language === "ko"
          ? "대기"
          : "idle"
      : !hasVlmHealth
        ? language === "ko"
          ? "확인 중"
          : "checking"
        : vlmHealth.connected
          ? language === "ko"
            ? "연결됨"
            : "connected"
          : language === "ko"
            ? "끊김"
            : "disconnected";
    const vlmHealthLabel = !vlmShouldRun
      ? language === "ko"
        ? "대기"
        : "idle"
      : !hasVlmHealth
        ? language === "ko"
          ? "확인 중"
          : "checking"
        : vlmHealth.healthy
          ? language === "ko"
            ? "정상"
            : "healthy"
          : language === "ko"
            ? "주의"
            : "degraded";
    const vlmClassName = !vlmShouldRun || !hasVlmHealth ? "idle" : vlmHealth.connected && vlmHealth.healthy ? "ok" : "warn";
    const vlmStatus = {
      kind: "VLM",
      connection: vlmConnectionLabel,
      health: vlmHealthLabel,
      className: vlmClassName,
      healthAge: elapsedLabel(vlmHealthReceivedAt, language),
      resultAge: elapsedLabel(vlmResultReceivedAt, language),
    };

    return {
      ui,
      language,
      layout,
      anchorMap,
      entityMap,
      activeBundle,
      bundleOptions,
      requestableTools,
      fieldAnchor,
      tools,
      routes,
      boardHolders,
      boardHumanoidGroup,
      boardSurgicalBed,
      boardMayoStand,
      boardRackSlotCount,
      boardRackSlots,
      toolChipPlacements,
      activeBoardRoutes,
      boardAudioBubble,
      boardActionBubbles,
      timeline,
      activeToolId,
      stage: {
        procedureLabel: localizedProcedureName,
        phaseName,
        rawPhaseId,
        displayedPhaseId,
        currentPhaseKind: currentPhaseIsInterrupt ? "interrupt" : "normal",
        interruptAlert,
        phaseSteps,
        robotTaskLabel,
        activeTaskProgress,
        cleanerCountdown,
        intentBubble,
        displayReadyForHandover,
        displayReadyForRetrieval,
      },
      surgeon: {
        intent: catalogLabel(catalog, "intents", displayIntent, language, titleize(displayIntent)),
        requestedTool: displayRequestedTool ? localizedToolName(displayRequestedTool) : ui.none,
        spoken: activeVoiceText || ui.none,
        readyForHandover: displayReadyForHandover,
        readyForRetrieval: displayReadyForRetrieval,
        note:
          overrideAck?.message ||
          surgeonState.scene_note ||
          (simulationState.cleaner_busy
            ? `Cleaner engaged for ${cleanerCountdown}s`
            : displayIntent === "idle"
              ? "Waiting for surgeon script"
              : `Intent in progress: ${catalogLabel(catalog, "intents", displayIntent, language, titleize(displayIntent))}`),
      },
      metrics: {
        phase: phaseName,
        robot: robotTaskLabel,
        cleaner: cleanerCountdown > 0 ? `${ui.busy} · ${cleanerCountdown}s` : ui.idle,
        progress: activeTaskProgress,
      },
      runtime: {
        stateLabel: runtimeStateLabel(simulationState, ui),
        statusMessage: runtimeStatusText(simulationState, ui, localizedProcedureName, language),
      },
      displayToolName: localizedToolName,
      displayPhaseName: localizedPhaseName,
      displayBundleName: localizedBundleName,
      displayActionName: localizedActionName,
      displayTransitionName: localizedTransitionName,
      displayLifecycleName: localizedLifecycleName,
      vlmStatus,
      anchorLabel: anchorNameForId,
    };
  }, [
    language,
    activeBundle,
    simulationState,
    skillStatus,
    surgeonState,
    events,
    overrideAck,
    vlmHealth,
    vlmResult,
    vlmHealthReceivedAt,
    vlmResultReceivedAt,
    stageAspectRatio,
  ]);

  useEffect(() => {
    if (viewModel.stage.currentPhaseKind === "normal" && viewModel.stage.rawPhaseId) {
      lastNormalPhaseRef.current = viewModel.stage.rawPhaseId;
    }
  }, [viewModel.stage.currentPhaseKind, viewModel.stage.rawPhaseId]);

  return viewModel;
}
