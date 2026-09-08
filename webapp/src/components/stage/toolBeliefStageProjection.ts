import type {
  StageHolder,
  StageHolderId,
  StageMayoDecision,
  StageRackSlot,
  StageToolChipBadge,
  StageToolChipDensity,
  StageToolChipPlacement,
  useDigitalTwinViewModel,
} from "../../hooks/useDigitalTwinViewModel";
import {
  isExchangeableToolBelief,
  isInactiveCapacityToolBelief,
  type ToolBeliefRuntime,
} from "../../ros/toolBeliefMessages";

export const TOOL_BELIEF_STAGE_STALE_AFTER_MS = 1_500;

type StageProjectionInput = {
  activeBundle: string;
  holders: readonly StageHolder[];
  nowMs: number;
  placements: readonly StageToolChipPlacement[];
  rackSlots: readonly StageRackSlot[];
  runtime: ToolBeliefRuntime | null | undefined;
};

export type ToolBeliefStageProjection = {
  placements: StageToolChipPlacement[];
  rackSlots: StageRackSlot[];
  projectedHolderIds: ReadonlySet<StageHolderId>;
  projectedInstanceIds: readonly string[];
  hiddenInstanceIds: readonly string[];
  hiddenInventoryCounts: ReadonlyMap<string, number>;
  exchangeableActiveCounts: ReadonlyMap<string, number>;
};

type ViewModel = ReturnType<typeof useDigitalTwinViewModel>;

type QuantifiedToolChipPlacement = StageToolChipPlacement & {
  quantity?: number;
  count?: number;
  instanceIds?: string[];
};

export type DisplayToolChipPlacement = StageToolChipPlacement & {
  quantity: number;
  instanceIds: string[];
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

/** Preserve only a Mayo decision already emitted by the canonical DT. */
function canonicalMayoDecisionForChips(
  chips: readonly StageToolChipPlacement[],
): StageMayoDecision | undefined {
  return chips
    .map((chip) => chip.canonicalMayoDecision)
    .filter((decision): decision is StageMayoDecision => decision !== undefined)
    .sort((left, right) =>
      right.confidence - left.confidence
      || (left.disposition === "recover" ? -1 : 1),
    )[0];
}

function rankedRackRepresentative(
  chips: QuantifiedToolChipPlacement[],
): QuantifiedToolChipPlacement | undefined {
  return [...chips].sort((left, right) => {
    if (left.active !== right.active) return left.active ? -1 : 1;
    const highlightDelta = HIGHLIGHT_PRIORITY[right.highlight] - HIGHLIGHT_PRIORITY[left.highlight];
    if (highlightDelta) return highlightDelta;
    return left.gridIndex - right.gridIndex || left.id.localeCompare(right.id);
  })[0];
}

/** Convert instance placements into rack inventory cards without recreating tools moved outside the rack. */
export function aggregateRackTools(
  chips: StageToolChipPlacement[],
  vm: ViewModel,
  rackSlots = vm.boardRackSlots,
  hiddenInventoryCounts: ReadonlyMap<string, number> = new Map(),
  exchangeableActiveCounts: ReadonlyMap<string, number> = new Map(),
): DisplayToolChipPlacement[] {
  const exchangeableInstrumentIds = new Set([
    ...exchangeableActiveCounts.keys(),
    ...chips.filter((chip) => chip.exchangeable).map((chip) => chip.instrumentId),
  ]);
  const quantifiedChips = chips.filter(
    (chip) => !exchangeableInstrumentIds.has(chip.instrumentId),
  ) as QuantifiedToolChipPlacement[];
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
    if (exchangeableInstrumentIds.has(instrument.id)) continue;
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
    const hiddenQuantity = inventory
      ? hiddenInventoryCounts.get(inventory.id) ?? 0
      : 0;
    const rackQuantity = inventory
      ? Math.max(0, inventory.count - outsideQuantity - hiddenQuantity)
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
    const slot = rackSlots.find(
      (candidate) => candidate.instrumentId === inventory?.id
        || normalizedToolGroupKey(candidate.label) === labelKey,
    );
    if (!source || !slot) continue;
    rackPlacements.push({
      ...source,
      id: `rack-inventory-${inventory?.id ?? labelKey}`,
      instrumentId: inventory?.id ?? source.instrumentId,
      label: inventory?.label ?? source.label,
      holderId: "rack",
      holderLabel: vm.language === "ko" ? "랙" : "Rack",
      left: slot.rect.left,
      top: slot.rect.top,
      width: slot.rect.width,
      height: slot.rect.height,
      scale: 1,
      compact: false,
      gridIndex: rackSlots.findIndex((candidate) => candidate.id === slot.id),
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
    const rackQuantity = Math.max(
      0,
      inventory.count - (hiddenInventoryCounts.get(inventory.id) ?? 0),
    );
    if (rackQuantity <= 0) continue;
    const slot = rackSlots.find((candidate) => candidate.instrumentId === inventory.id);
    if (!slot) continue;
    rackPlacements.push({
      id: `rack-inventory-${inventory.id}`,
      instrumentId: inventory.id,
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
      gridIndex: rackSlots.findIndex((candidate) => candidate.id === slot.id),
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
  }

  const nonRackPlacements = quantifiedChips
    .filter((chip) => chip.holderId !== "rack")
    .map((chip) => ({
      ...chip,
      quantity: quantityForChip(chip),
      instanceIds: instanceIdsForChip(chip),
    }));

  const fixedPlacements = [
    ...rackPlacements.sort((left, right) => left.gridIndex - right.gridIndex),
    ...nonRackPlacements,
  ];
  if (!exchangeableInstrumentIds.size) return fixedPlacements;

  const exchangeableChips = chips.filter(
    (chip) => exchangeableInstrumentIds.has(chip.instrumentId),
  ) as QuantifiedToolChipPlacement[];
  const exchangeablePlacements: DisplayToolChipPlacement[] = [];
  const relayoutHolderIds = new Set<StageHolderId>();
  for (const instrumentId of exchangeableInstrumentIds) {
    const group = exchangeableChips.filter((chip) => chip.instrumentId === instrumentId);
    const activeCount = exchangeableActiveCounts.get(instrumentId)
      ?? group.reduce((total, chip) => total + quantityForChip(chip), 0)
        + (hiddenInventoryCounts.get(instrumentId) ?? 0);
    if (activeCount <= 0) continue;

    const byHolder = new Map<StageHolderId, QuantifiedToolChipPlacement[]>();
    for (const chip of group.filter((candidate) => candidate.holderId !== "rack")) {
      byHolder.set(chip.holderId, [...(byHolder.get(chip.holderId) ?? []), chip]);
    }
    let outsideQuantity = 0;
    for (const [holderId, holderChips] of byHolder) {
      const quantity = holderChips.reduce(
        (total, chip) => total + quantityForChip(chip),
        0,
      );
      if (quantity <= 0) continue;
      outsideQuantity += quantity;
      const representative = rankedRackRepresentative(holderChips);
      if (!representative) continue;
      const canonicalMayoDecision = holderId === "mayo"
        ? canonicalMayoDecisionForChips(holderChips)
        : undefined;
      exchangeablePlacements.push({
        ...representative,
        id: `belief:${instrumentId}:${holderId}`,
        displayInstanceId: undefined,
        instanceIds: [],
        s: [],
        exchangeable: true,
        quantity,
        contaminated: holderChips.some((chip) => chip.contaminated),
        active: false,
        highlight: "normal",
        canonicalMayoDecision,
        footerBadges: mergeToolBadges(holderChips),
      });
      if (holderChips.length > 1) relayoutHolderIds.add(holderId);
    }

    const hiddenQuantity = hiddenInventoryCounts.get(instrumentId) ?? 0;
    const rackQuantity = Math.max(0, activeCount - outsideQuantity - hiddenQuantity);
    if (rackQuantity <= 0) continue;
    const rackChips = group.filter((chip) => chip.holderId === "rack");
    const representative = rankedRackRepresentative(rackChips) ?? group[0];
    const slot = rackSlots.find((candidate) => candidate.instrumentId === instrumentId);
    if (!representative || !slot) continue;
    const slotIndex = rackSlots.findIndex((candidate) => candidate.id === slot.id);
    exchangeablePlacements.push({
      ...representative,
      id: `belief:${instrumentId}:rack`,
      instrumentId,
      label: vm.displayToolName(instrumentId),
      shortLabel: slot.shortLabel,
      holderId: "rack",
      holderLabel: vm.language === "ko" ? "랙" : "Rack",
      left: slot.rect.left,
      top: slot.rect.top,
      width: slot.rect.width,
      height: slot.rect.height,
      scale: 1,
      compact: false,
      gridIndex: slotIndex,
      displayState: "waiting",
      highlight: "normal",
      lifecycle: vm.ui.waitingState,
      footerBadges: [{ label: vm.ui.waitingState, tone: "neutral" }],
      contaminated: false,
      active: false,
      layoutVariant: "card",
      density: "regular",
      displayInstanceId: undefined,
      instanceIds: [],
      s: [],
      exchangeable: true,
      quantity: rackQuantity,
    });
  }

  const combined = [...fixedPlacements, ...exchangeablePlacements];
  return vm.boardHolders?.length && relayoutHolderIds.size
    ? relayoutPlacements(combined, vm.boardHolders, rackSlots, relayoutHolderIds) as DisplayToolChipPlacement[]
    : combined;
}

type InstanceProjection = {
  committedLocationId: string;
  exchangeable: boolean;
  inactive: boolean;
  instrumentId: string;
  hidden: boolean;
};

type ProjectionPartition = {
  holderId: StageHolderId;
  instanceIds: string[];
  anonymousQuantity: number;
  exchangeable: boolean;
};

const COMMITTED_LOCATION_HOLDERS: Readonly<Record<string, StageHolderId>> = {
  cleaner: "cleaner",
  cleaner_slot: "cleaner",
  field: "surgeon",
  mayo: "mayo",
  mayo_recovery_zone: "mayo",
  mayo_reuse_zone: "mayo",
  mayo_stand: "mayo",
  rack: "rack",
  surgeon: "surgeon",
  surgeon_hand: "surgeon",
  surgical_field: "surgeon",
  tray: "rack",
  tray_slot: "rack",
};

function normalizedLocationId(value: string): string {
  return value.trim().toLocaleLowerCase().replace(/[\s-]+/g, "_");
}

const UNKNOWN_LOCATION_IDS = new Set([
  "location_unknown",
  "not_visible",
  "unknown",
  "unknown_location",
  "unlocated",
  "unresolved",
]);

function isUnknownLocationId(value: string): boolean {
  return UNKNOWN_LOCATION_IDS.has(normalizedLocationId(value));
}

function projectedHolderId(locationId: string, currentHolderId: StageHolderId): StageHolderId | null {
  const normalized = normalizedLocationId(locationId);
  if (normalized === "robot" || normalized === "robot_grasp") {
    return currentHolderId === "humanoid_left" ? "humanoid_left" : "humanoid_right";
  }
  if (normalized === "robot_left" || normalized === "robot_left_hand") return "humanoid_left";
  if (normalized === "robot_right" || normalized === "robot_right_hand") return "humanoid_right";
  return COMMITTED_LOCATION_HOLDERS[normalized] ?? null;
}

function validProjectionByInstance({
  activeBundle,
  nowMs,
  runtime,
}: Pick<StageProjectionInput, "activeBundle" | "nowMs" | "runtime">): Map<string, InstanceProjection> {
  const snapshot = runtime?.snapshot;
  const snapshotAgeMs = snapshot ? Math.max(0, nowMs - snapshot.receivedAt) : Number.POSITIVE_INFINITY;
  if (
    runtime?.enabled !== true
    || !snapshot
    || snapshot.procedureId !== activeBundle
    || !Number.isFinite(snapshot.receivedAt)
    || snapshotAgeMs > TOOL_BELIEF_STAGE_STALE_AFTER_MS
  ) {
    return new Map();
  }

  const projection = new Map<string, InstanceProjection>();
  for (const tool of snapshot.tools) {
    const instanceId = tool.instanceId.trim();
    const instrumentId = tool.instrumentId.trim();
    if (!instanceId || !instrumentId || projection.has(instanceId)) continue;
    const exchangeable = isExchangeableToolBelief(tool);
    const inactive = exchangeable && isInactiveCapacityToolBelief(tool);
    const committedLocationId = tool.committedLocationId.trim();
    if (inactive) {
      projection.set(instanceId, {
        committedLocationId: "",
        exchangeable,
        inactive: true,
        instrumentId,
        hidden: true,
      });
      continue;
    }
    if (isUnknownLocationId(committedLocationId)) {
      projection.set(instanceId, {
        committedLocationId: "",
        exchangeable,
        inactive: false,
        instrumentId,
        hidden: true,
      });
      continue;
    }
    if (committedLocationId && projectedHolderId(committedLocationId, "rack")) {
      projection.set(instanceId, {
        committedLocationId,
        exchangeable,
        inactive: false,
        instrumentId,
        hidden: isUnknownLocationId(tool.mostLikelyLocationId),
      });
      continue;
    }
    if (
      !committedLocationId
      && (exchangeable || isUnknownLocationId(tool.mostLikelyLocationId))
    ) {
      projection.set(instanceId, {
        committedLocationId: "",
        exchangeable,
        inactive: false,
        instrumentId,
        hidden: true,
      });
    }
  }
  return projection;
}

function quantityForPlacement(placement: StageToolChipPlacement): number {
  const quantity = placement.quantity ?? placement.instanceIds?.length ?? 1;
  return Number.isFinite(quantity) ? Math.max(1, Math.floor(quantity)) : 1;
}

function instanceIdsForPlacement(placement: StageToolChipPlacement): string[] {
  return [...new Set((placement.instanceIds ?? []).map((value) => value.trim()).filter(Boolean))];
}

function isSurgeonOwnedInstance(
  placement: StageToolChipPlacement,
  instanceId: string,
): boolean {
  const index = placement.instanceIds?.indexOf(instanceId) ?? -1;
  return index >= 0 && placement.s?.[index] === true;
}

function activeExchangeableCounts(
  projectionByInstance: ReadonlyMap<string, InstanceProjection>,
): Map<string, number> {
  const counts = new Map<string, number>();
  for (const projection of projectionByInstance.values()) {
    if (!projection.exchangeable) continue;
    if (!counts.has(projection.instrumentId)) counts.set(projection.instrumentId, 0);
    if (projection.inactive) continue;
    counts.set(
      projection.instrumentId,
      (counts.get(projection.instrumentId) ?? 0) + 1,
    );
  }
  return counts;
}

function synthesizeMissingExchangeableSlots(
  placements: readonly StageToolChipPlacement[],
  holders: readonly StageHolder[],
  rackSlots: readonly StageRackSlot[],
  projectionByInstance: ReadonlyMap<string, InstanceProjection>,
): StageToolChipPlacement[] {
  const result = [...placements];
  const representedIds = new Set(placements.flatMap(instanceIdsForPlacement));
  const holderLabels = new Map(holders.map((holder) => [holder.id, holder.label]));

  for (const [instanceId, projection] of projectionByInstance) {
    if (!projection.exchangeable || representedIds.has(instanceId)) continue;
    const sameInstrument = result.find(
      (placement) => placement.instrumentId === projection.instrumentId,
    );
    const rackSlotIndex = rackSlots.findIndex(
      (slot) => slot.instrumentId === projection.instrumentId,
    );
    const rackSlot = rackSlotIndex >= 0 ? rackSlots[rackSlotIndex] : undefined;
    const fallbackHolder = holders.find((holder) => holder.id === "rack") ?? holders[0];
    const holderId = sameInstrument?.holderId ?? "rack";
    const fallbackRect = rackSlot?.rect ?? (sameInstrument ? {
      left: sameInstrument.left,
      top: sameInstrument.top,
      width: sameInstrument.width,
      height: sameInstrument.height,
    } : fallbackHolder?.contentRect) ?? {
      left: 0,
      top: 0,
      width: 8,
      height: 6,
    };
    result.push({
      id: `belief-slot:${projection.instrumentId}:${instanceId}`,
      instrumentId: projection.instrumentId,
      label: sameInstrument?.label ?? rackSlot?.label ?? projection.instrumentId,
      shortLabel: sameInstrument?.shortLabel ?? rackSlot?.shortLabel ?? projection.instrumentId,
      instanceIds: [instanceId],
      s: [false],
      exchangeable: true,
      displayInstanceId: undefined,
      quantity: 1,
      holderId,
      holderLabel: sameInstrument?.holderLabel ?? holderLabels.get(holderId) ?? holderId,
      left: fallbackRect.left,
      top: fallbackRect.top,
      width: fallbackRect.width,
      height: fallbackRect.height,
      scale: 1,
      compact: false,
      gridIndex: rackSlotIndex >= 0 ? rackSlotIndex : sameInstrument?.gridIndex ?? 0,
      displayState: "waiting",
      lifecycle: "observation_only",
      footerBadges: [],
      contaminated: false,
      active: false,
      highlight: "normal",
      layoutVariant: "card",
      density: "regular",
    });
    representedIds.add(instanceId);
  }
  return result;
}

type PlacementSlot = {
  instanceId: string;
  instrumentId: string;
  surgeonOwned: boolean;
};

function projectionOrder(projection: InstanceProjection): string {
  const priority = projection.inactive
    ? 3
    : projection.hidden && !projection.committedLocationId
      ? 0
      : projection.hidden
        ? 1
        : 2;
  return `${priority}:${normalizedLocationId(projection.committedLocationId)}`;
}

/**
 * Exchangeable tracker IDs are capacity slots, not durable physical identities.
 * Reassign their facts within one tool type so swapping #1/#2 cannot move a
 * physical-looking card while preserving the exact v1 path for fixed IDs.
 */
function projectionForPlacementSlots(
  placements: readonly StageToolChipPlacement[],
  projectionByInstance: ReadonlyMap<string, InstanceProjection>,
): Map<string, InstanceProjection> {
  const slotsByInstrument = new Map<string, PlacementSlot[]>();
  const seen = new Set<string>();
  for (const placement of placements) {
    for (const instanceId of instanceIdsForPlacement(placement)) {
      if (seen.has(instanceId)) continue;
      seen.add(instanceId);
      const slots = slotsByInstrument.get(placement.instrumentId) ?? [];
      slots.push({
        instanceId,
        instrumentId: placement.instrumentId,
        surgeonOwned: isSurgeonOwnedInstance(placement, instanceId),
      });
      slotsByInstrument.set(placement.instrumentId, slots);
    }
  }

  const result = new Map<string, InstanceProjection>();
  for (const [instrumentId, slots] of slotsByInstrument) {
    const facts = slots
      .map((slot) => projectionByInstance.get(slot.instanceId))
      .filter((projection): projection is InstanceProjection =>
        projection?.instrumentId === instrumentId
      );
    if (slots.length <= 1 || !facts.some((projection) => projection.exchangeable)) {
      for (const slot of slots) {
        const projection = projectionByInstance.get(slot.instanceId);
        if (projection?.instrumentId === instrumentId) {
          result.set(slot.instanceId, projection);
        }
      }
      continue;
    }

    const assignedFacts = new Set<InstanceProjection>();
    const assignedSlots = new Set<string>();
    const unknownActive = facts
      .filter((projection) => projection.hidden && !projection.inactive)
      .sort((left, right) => projectionOrder(left).localeCompare(projectionOrder(right)));
    for (const slot of slots.filter(({ surgeonOwned }) => surgeonOwned)) {
      const projection = unknownActive.find((candidate) => !assignedFacts.has(candidate));
      if (!projection) break;
      result.set(slot.instanceId, projection);
      assignedSlots.add(slot.instanceId);
      assignedFacts.add(projection);
    }

    const remainingSlots = slots.filter(({ instanceId }) => !assignedSlots.has(instanceId));
    const remainingFacts = facts
      .filter((projection) => !assignedFacts.has(projection))
      .sort((left, right) => projectionOrder(left).localeCompare(projectionOrder(right)));
    remainingSlots.forEach((slot, index) => {
      const projection = remainingFacts[index];
      if (projection) result.set(slot.instanceId, projection);
    });
  }
  return result;
}

function partitionPlacement(
  placement: StageToolChipPlacement,
  projectionByInstance: ReadonlyMap<string, InstanceProjection>,
): ProjectionPartition[] {
  const instanceIds = instanceIdsForPlacement(placement);
  if (!instanceIds.length) {
    return [{
      holderId: placement.holderId,
      instanceIds: [],
      anonymousQuantity: quantityForPlacement(placement),
      exchangeable: placement.exchangeable === true,
    }];
  }

  const partitions = new Map<StageHolderId, ProjectionPartition>();
  const partitionFor = (holderId: StageHolderId) => {
    const existing = partitions.get(holderId);
    if (existing) return existing;
    const created: ProjectionPartition = {
      holderId,
      instanceIds: [],
      anonymousQuantity: 0,
      exchangeable: placement.exchangeable === true,
    };
    partitions.set(holderId, created);
    return created;
  };

  for (const instanceId of instanceIds) {
    const projected = projectionByInstance.get(instanceId);
    const matchesProjection = projected?.instrumentId === placement.instrumentId;
    const preserveSurgeonOwned = matchesProjection
      && projected.hidden
      && !projected.inactive
      && isSurgeonOwnedInstance(placement, instanceId);
    if (
      matchesProjection
      && projected.hidden
      && (projected.inactive || !projected.committedLocationId)
      && !preserveSurgeonOwned
    ) {
      continue;
    }
    const targetHolder = matchesProjection && !preserveSurgeonOwned
      ? projectedHolderId(projected.committedLocationId, placement.holderId) ?? placement.holderId
      : placement.holderId;
    const partition = partitionFor(targetHolder);
    partition.instanceIds.push(instanceId);
    partition.exchangeable ||= projected?.exchangeable === true;
  }

  const anonymousQuantity = Math.max(0, quantityForPlacement(placement) - instanceIds.length);
  if (anonymousQuantity) {
    partitionFor(placement.holderId).anonymousQuantity += anonymousQuantity;
  }
  return [...partitions.values()];
}

function holderLabel(holderId: StageHolderId, languageLabel: string): string {
  return languageLabel || holderId;
}

function projectedPlacements(
  placements: readonly StageToolChipPlacement[],
  holders: readonly StageHolder[],
  projectionByInstance: ReadonlyMap<string, InstanceProjection>,
): StageToolChipPlacement[] {
  const labels = new Map(holders.map((holder) => [holder.id, holder.label]));
  const result: StageToolChipPlacement[] = [];
  for (const placement of placements) {
    const partitions = partitionPlacement(placement, projectionByInstance);
    partitions.forEach((partition, partitionIndex) => {
      const quantity = partition.instanceIds.length + partition.anonymousQuantity;
      if (quantity <= 0) return;
      const displayInstanceId = partition.exchangeable
        ? undefined
        : partition.instanceIds.includes(placement.displayInstanceId ?? "")
          ? placement.displayInstanceId
          : partition.instanceIds[0];
      // Real-to-Sim only projects a fresh, committed observation onto the
      // stage; it does not mutate the DT lifecycle.  Do not carry a stale
      // surgeon/robot `using`, `handover`, or `cleaning` presentation into
      // the Mayo card.  A current authoritative active task is preserved.
      const staleActionState = partition.holderId === "mayo"
        && !placement.active
        && ["using", "handover", "cleaning"].includes(placement.displayState);
      const projectedFooterBadges = staleActionState
        ? placement.footerBadges.filter((badge) =>
            badge.tone === "danger" || badge.tone === "recovery"
          )
        : placement.footerBadges;
      result.push({
        ...placement,
        id: partitions.length === 1
          ? placement.id
          : `${placement.id}:belief:${partition.holderId}:${partitionIndex}`,
        instanceIds: partition.instanceIds,
        s: partition.instanceIds.map((instanceId) =>
          isSurgeonOwnedInstance(placement, instanceId)
        ),
        exchangeable: partition.exchangeable,
        displayInstanceId,
        quantity,
        holderId: partition.holderId,
        holderLabel: holderLabel(partition.holderId, labels.get(partition.holderId) ?? ""),
        displayState: staleActionState ? "waiting" : placement.displayState,
        footerBadges: projectedFooterBadges,
        layoutVariant: partition.holderId === "mayo" ? "mayoList" : "card",
        canonicalMayoDecision: partition.holderId === "mayo"
          ? placement.canonicalMayoDecision
          : undefined,
      });
    });
  }
  return result;
}

function densityForHolder(holderId: StageHolderId, count: number, compact: boolean): StageToolChipDensity {
  if (holderId !== "mayo") return compact ? "dense" : "regular";
  if (count <= 1) return "comfortable";
  if (count <= 3) return "regular";
  if (count <= 4) return "dense";
  return "micro";
}

function layoutMayoPlacements(
  placements: StageToolChipPlacement[],
  holder: StageHolder,
): StageToolChipPlacement[] {
  const count = placements.length;
  if (!count) return [];
  // Preserve the same scan-first full-width Mayo list after a Real-to-Sim
  // visual projection moves an item. Otherwise projected cards regress to a
  // cramped half-width layout while canonical cards remain readable.
  const columns = count <= 3 ? 1 : 2;
  const rows = Math.max(1, Math.ceil(count / columns));
  const rowGap = rows > 1 ? 0.45 : 0;
  const columnGap = columns > 1 ? 0.8 : 0;
  const rowHeight = Math.max(
    1.9,
    (holder.contentRect.height - rowGap * (rows - 1)) / rows,
  );
  const rowWidth = columns === 1
    ? holder.contentRect.width
    : (holder.contentRect.width - columnGap) / columns;
  const gridWidth = rowWidth * columns + columnGap * (columns - 1);
  const startLeft = holder.contentRect.left + (holder.contentRect.width - gridWidth) / 2;
  return placements.map((placement, index) => {
    const column = index % columns;
    const row = Math.floor(index / columns);
    return {
      ...placement,
      left: startLeft + column * (rowWidth + columnGap) + rowWidth / 2,
      top: holder.contentRect.top + rowHeight / 2 + row * (rowHeight + rowGap),
      width: rowWidth,
      height: rowHeight,
      scale: Math.min(1, rowHeight / Math.max(1, placement.height / Math.max(placement.scale, 0.01))),
      compact: false,
      gridIndex: index,
      density: densityForHolder("mayo", count, false),
      layoutVariant: "mayoList",
    };
  });
}

function layoutGridPlacements(
  placements: StageToolChipPlacement[],
  holder: StageHolder,
): StageToolChipPlacement[] {
  const count = placements.length;
  if (!count) return [];
  const columns = holder.id === "cleaner" || holder.id === "surgeon"
    ? 1
    : Math.min(2, count);
  const rows = Math.max(1, Math.ceil(count / columns));
  const gap = 0.55;
  const cellWidth = Math.max(0.1, (holder.contentRect.width - gap * (columns - 1)) / columns);
  const cellHeight = Math.max(0.1, (holder.contentRect.height - gap * (rows - 1)) / rows);
  return placements.map((placement, index) => {
    const column = index % columns;
    const row = Math.floor(index / columns);
    const width = Math.min(cellWidth, placement.width);
    const height = Math.min(cellHeight, placement.height);
    const compact = width < placement.width * 0.78 || height < placement.height * 0.78;
    return {
      ...placement,
      left: holder.contentRect.left + column * (cellWidth + gap) + cellWidth / 2,
      top: holder.contentRect.top + row * (cellHeight + gap) + cellHeight / 2,
      width,
      height,
      scale: Math.min(1, width / Math.max(placement.width, 0.01), height / Math.max(placement.height, 0.01)),
      compact,
      gridIndex: index,
      density: densityForHolder(holder.id, count, compact),
      layoutVariant: "card",
    };
  });
}

function layoutRackPlacements(
  placements: StageToolChipPlacement[],
  rackSlots: readonly StageRackSlot[],
): StageToolChipPlacement[] {
  const claimedSlots = new Set<number>();
  return placements.map((placement) => {
    const matchingSlots = rackSlots
      .map((slot, index) => ({ slot, index }))
      .filter(({ slot }) => slot.instrumentId === placement.instrumentId);
    const existing = matchingSlots.find(({ index }) => index === placement.gridIndex && !claimedSlots.has(index));
    const target = existing ?? matchingSlots.find(({ index }) => !claimedSlots.has(index)) ?? matchingSlots[0];
    if (!target) return placement;
    claimedSlots.add(target.index);
    return {
      ...placement,
      left: target.slot.rect.left,
      top: target.slot.rect.top,
      width: target.slot.rect.width,
      height: target.slot.rect.height,
      scale: Math.min(1, target.slot.rect.height / Math.max(placement.height / Math.max(placement.scale, 0.01), 0.01)),
      compact: false,
      gridIndex: target.index,
      density: "regular",
      layoutVariant: "card",
    };
  });
}

function relayoutPlacements(
  placements: StageToolChipPlacement[],
  holders: readonly StageHolder[],
  rackSlots: readonly StageRackSlot[],
  affectedHolderIds: ReadonlySet<StageHolderId>,
): StageToolChipPlacement[] {
  const holdersById = new Map(holders.map((holder) => [holder.id, holder]));
  const result: StageToolChipPlacement[] = [];
  for (const holderId of [
    "rack",
    "humanoid_left",
    "humanoid_right",
    "surgeon",
    "cleaner",
    "mayo",
  ] satisfies StageHolderId[]) {
    const inHolder = placements.filter((placement) => placement.holderId === holderId);
    if (!affectedHolderIds.has(holderId)) {
      result.push(...inHolder);
      continue;
    }
    if (holderId === "rack") {
      result.push(...layoutRackPlacements(inHolder, rackSlots));
      continue;
    }
    const holder = holdersById.get(holderId);
    if (!holder) {
      result.push(...inHolder);
      continue;
    }
    result.push(...(
      holderId === "mayo"
        ? layoutMayoPlacements(inHolder, holder)
        : layoutGridPlacements(inHolder, holder)
    ));
  }
  return result;
}

/**
 * Visual-only projection of fresh Real-to-Sim facts. A latched committed
 * location remains authoritative across probability jitter. Once that commit
 * is released and the most likely location is explicitly unknown, the exact
 * instance is hidden unless the DT says it is already surgeon-owned.
 * This never mutates Digital Twin state and fails back to the DT layout when
 * tracking is off, stale, for another bundle, or has no fact for the instance.
 */
export function projectToolBeliefsOntoStage(input: StageProjectionInput): ToolBeliefStageProjection {
  const rawProjectionByInstance = validProjectionByInstance(input);
  const exchangeableActiveCounts = activeExchangeableCounts(rawProjectionByInstance);
  if (!rawProjectionByInstance.size) {
    return {
      placements: [...input.placements],
      rackSlots: [...input.rackSlots],
      projectedHolderIds: new Set(),
      projectedInstanceIds: [],
      hiddenInstanceIds: [],
      hiddenInventoryCounts: new Map(),
      exchangeableActiveCounts,
    };
  }

  const sourcePlacements = synthesizeMissingExchangeableSlots(
    input.placements,
    input.holders,
    input.rackSlots,
    rawProjectionByInstance,
  );
  const projectionByInstance = projectionForPlacementSlots(
    sourcePlacements,
    rawProjectionByInstance,
  );

  const projectedInstanceIds: string[] = [];
  const hiddenInstanceIds = new Set<string>();
  const hiddenInventoryCounts = new Map<string, number>();
  const projectedHolderIds = new Set<StageHolderId>();
  const affectedHolderIds = new Set<StageHolderId>();
  for (const placement of sourcePlacements) {
    for (const instanceId of instanceIdsForPlacement(placement)) {
      const projection = projectionByInstance.get(instanceId);
      if (!projection || projection.instrumentId !== placement.instrumentId) continue;
      if (
        projection.hidden
        && !projection.inactive
        && isSurgeonOwnedInstance(placement, instanceId)
      ) continue;
      if (projection.hidden && (projection.inactive || !projection.committedLocationId)) {
        if (hiddenInstanceIds.has(instanceId)) continue;
        hiddenInstanceIds.add(instanceId);
        if (!projection.inactive) {
          hiddenInventoryCounts.set(
            placement.instrumentId,
            (hiddenInventoryCounts.get(placement.instrumentId) ?? 0) + 1,
          );
        }
        affectedHolderIds.add(placement.holderId);
        continue;
      }
      const targetHolderId = projectedHolderId(
        projection.committedLocationId,
        placement.holderId,
      );
      if (!targetHolderId || targetHolderId === placement.holderId) continue;
      projectedInstanceIds.push(instanceId);
      projectedHolderIds.add(targetHolderId);
      affectedHolderIds.add(placement.holderId);
      affectedHolderIds.add(targetHolderId);
    }
  }
  const hasExchangeableProjection = [...projectionByInstance.values()].some(
    (projection) => projection.exchangeable,
  );
  if (!projectedInstanceIds.length && !hiddenInstanceIds.size && !hasExchangeableProjection) {
    return {
      placements: [...input.placements],
      rackSlots: [...input.rackSlots],
      projectedHolderIds: new Set(),
      projectedInstanceIds: [],
      hiddenInstanceIds: [],
      hiddenInventoryCounts: new Map(),
      exchangeableActiveCounts,
    };
  }

  const projected = relayoutPlacements(
    projectedPlacements(sourcePlacements, input.holders, projectionByInstance),
    input.holders,
    input.rackSlots,
    affectedHolderIds,
  );
  const occupiedRackSlotIndexes = new Set(
    projected
      .filter((placement) => placement.holderId === "rack")
      .map((placement) => placement.gridIndex),
  );
  return {
    placements: projected,
    rackSlots: input.rackSlots.map((slot, index) => ({
      ...slot,
      occupied: occupiedRackSlotIndexes.has(index),
    })),
    projectedHolderIds,
    projectedInstanceIds,
    hiddenInstanceIds: [...hiddenInstanceIds],
    hiddenInventoryCounts,
    exchangeableActiveCounts,
  };
}
