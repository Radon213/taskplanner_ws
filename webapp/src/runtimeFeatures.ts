import type { TaskplannerRuntimeMode } from "./runtimeModes";

function configuredBoolean(value: string | undefined): boolean | null {
  const normalized = value?.trim().toLowerCase();
  if (!normalized) return null;
  if (["1", "true", "yes", "on"].includes(normalized)) return true;
  if (["0", "false", "no", "off"].includes(normalized)) return false;
  return null;
}

const configuredDefaultMode = import.meta.env.VITE_DEFAULT_RUNTIME_MODE?.trim();
const explicitOptionalUi = configuredBoolean(
  import.meta.env.VITE_ENABLE_OPTIONAL_UI,
);

/**
 * This build-time capability allows a dedicated lab build to request optional
 * modes from Live. It is not the runtime-authority signal: the same persistent
 * Production bundle must also reveal the matching lazy UI after the trusted
 * host runtime reports that LLM, Replay, or Debug is actually active.
 */
export const OPTIONAL_OPERATIONS_UI_ENABLED =
  explicitOptionalUi ?? Boolean(configuredDefaultMode && configuredDefaultMode !== "live");

export function runtimeModeIsAvailable(mode: TaskplannerRuntimeMode): boolean {
  return mode === "live" || OPTIONAL_OPERATIONS_UI_ENABLED;
}

export function optionalOperationsUiEnabled(
  authoritativeMode: TaskplannerRuntimeMode | null,
): boolean {
  return OPTIONAL_OPERATIONS_UI_ENABLED
    || (authoritativeMode !== null && authoritativeMode !== "live");
}

export type MissionObservationProfile = "live-core" | "extended";

export function missionObservationProfile(
  authoritativeMode: TaskplannerRuntimeMode | null,
): MissionObservationProfile {
  return optionalOperationsUiEnabled(authoritativeMode) ? "extended" : "live-core";
}
