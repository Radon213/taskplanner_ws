import {
  isExchangeableToolBelief,
  isInactiveCapacityToolBelief,
  type ToolBeliefStatus,
  type TrackedToolBelief,
} from "../../ros/toolBeliefMessages";

export type ToolBeliefPanelRow = {
  key: string;
  tool: TrackedToolBelief;
  quantity: number;
  exchangeable: boolean;
};

const STATUS_CERTAINTY: Record<ToolBeliefStatus, number> = {
  uncertain: 0,
  probable: 1,
  confirmed: 2,
};

/** Aggregate exchangeable observations by semantic tool type and location. */
export function aggregateToolBeliefPanelRows(
  tools: readonly TrackedToolBelief[],
): ToolBeliefPanelRow[] {
  const rows: ToolBeliefPanelRow[] = [];
  const exchangeableGroups = new Map<string, TrackedToolBelief[]>();
  for (const tool of tools) {
    if (isInactiveCapacityToolBelief(tool)) continue;
    if (!isExchangeableToolBelief(tool)) {
      rows.push({
        key: `fixed:${tool.trackId}`,
        tool,
        quantity: 1,
        exchangeable: false,
      });
      continue;
    }
    const key = `${tool.instrumentId}\u0000${tool.mostLikelyLocationId}`;
    exchangeableGroups.set(key, [...(exchangeableGroups.get(key) ?? []), tool]);
  }

  for (const [groupKey, group] of exchangeableGroups) {
    const representative = [...group].sort((left, right) =>
      STATUS_CERTAINTY[left.status] - STATUS_CERTAINTY[right.status]
      || right.mostLikelyProbability - left.mostLikelyProbability
    )[0];
    if (!representative) continue;
    const locationTotals = new Map<string, number>();
    for (const tool of group) {
      for (const location of tool.locations) {
        locationTotals.set(
          location.locationId,
          (locationTotals.get(location.locationId) ?? 0) + location.probability,
        );
      }
    }
    const locations = [...locationTotals]
      .map(([locationId, total]) => ({
        locationId,
        probability: total / group.length,
      }))
      .sort((left, right) =>
        right.probability - left.probability
        || left.locationId.localeCompare(right.locationId)
      );
    const evidenceAges = group
      .map((tool) => tool.lastPositiveAgeSec)
      .filter((age): age is number => age !== null);
    const [instrumentId, locationId] = groupKey.split("\u0000");
    rows.push({
      key: `exchangeable:${instrumentId}:${locationId}`,
      quantity: group.length,
      exchangeable: true,
      tool: {
        ...representative,
        trackId: `exchangeable:${instrumentId}:${locationId}`,
        instanceId: "",
        mostLikelyProbability:
          locations.find((location) => location.locationId === locationId)?.probability ?? 0,
        locations,
        lastPositiveAgeSec: evidenceAges.length ? Math.min(...evidenceAges) : null,
        evidenceSources: [...new Set(group.flatMap((tool) => tool.evidenceSources))],
        statusFlags: [...new Set(group.flatMap((tool) => tool.statusFlags))],
      },
    });
  }
  return rows;
}
