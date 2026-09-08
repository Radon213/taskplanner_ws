import fallbackProfiles from "../../config/debugToolHandoverProfiles.json";
import type { IntegrationDebugStatus } from "../../hooks/useIntegrationDebugBridge";

export interface DebugToolHandoverOption {
  catalogId: string;
  instrumentId: string;
  instanceIds: readonly string[];
}

export interface DebugToolProfileProjection {
  profiles: readonly DebugToolHandoverOption[];
  source: "scenario_catalog" | "debug_fallback";
  revision: string;
}

function normalizedProfile(value: unknown): DebugToolHandoverOption | null {
  if (!value || typeof value !== "object") return null;
  const profile = value as Record<string, unknown>;
  const catalogId = String(profile.catalog_id ?? profile.catalogId ?? "").trim();
  const instrumentId = String(profile.instrument_id ?? profile.instrumentId ?? "").trim();
  const rawInstances = profile.instance_ids ?? profile.instanceIds;
  const instanceIds = Array.isArray(rawInstances)
    ? rawInstances.map((item) => String(item).trim()).filter(Boolean)
    : [];
  if (!catalogId || !instrumentId || !instanceIds.length) return null;
  return { catalogId, instrumentId, instanceIds };
}

/**
 * Prefer a current scenario/catalog owner projection.  The compact JSON file
 * is only a compatibility fallback for an older Debug observer which does not
 * yet publish a catalog row.
 */
export function debugToolProfiles(
  status: IntegrationDebugStatus | null,
): DebugToolProfileProjection {
  const catalogProfiles = (status?.tool_catalog?.profiles ?? [])
    .map(normalizedProfile)
    .filter((profile): profile is DebugToolHandoverOption => profile !== null);
  if (catalogProfiles.length) {
    return {
      profiles: catalogProfiles,
      source: "scenario_catalog",
      revision: status?.tool_catalog?.revision?.trim() || "현재 catalog",
    };
  }
  const fallback = (fallbackProfiles.profiles as unknown[])
    .map(normalizedProfile)
    .filter((profile): profile is DebugToolHandoverOption => profile !== null);
  return {
    profiles: fallback,
    source: "debug_fallback",
    revision: "Debug fallback",
  };
}

