import type { IntegrationDebugStatus } from "../hooks/useIntegrationDebugBridge";

type DebugWriteCapability = "control" | "asr" | "record" | "network";

const OPERATION_CAPABILITY: Readonly<Record<string, readonly DebugWriteCapability[]>> = {
  asr_refresh_devices: ["control", "asr"],
  asr_start: ["control", "asr"],
  asr_stop: ["control", "asr"],
  asr_recording_start: ["control", "asr"],
  asr_recording_stop: ["control", "asr"],
  record_submit: ["control", "record"],
  apply_network_settings: ["control", "network"],
  ping_host: ["control", "network"],
};

const CAPABILITY_LABEL: Readonly<Record<DebugWriteCapability, string>> = {
  control: "제어 owner",
  asr: "ASR owner",
  record: "수술기록 owner",
  network: "network owner",
};

function requiredCapabilities(operation: string): readonly DebugWriteCapability[] {
  return OPERATION_CAPABILITY[operation] ?? ["control"];
}

/**
 * Browser-side lifecycle hint only. The Debug command service remains the
 * authoritative admission point; this avoids a guaranteed timeout when the
 * public observer is running without its private control owner.
 */
export function debugCapabilityBlockReason(
  status: IntegrationDebugStatus | null,
  operation: string,
): string | null {
  if (operation === "heartbeat") return null;
  const rows = status?.capabilities;
  // Keep an older backend usable during a rolling update. Its command service
  // remains responsible for rejecting unavailable features.
  if (!rows?.length) return null;
  const enabled = new Map(rows.map((row) => [row.name, row.enabled]));
  const missing = requiredCapabilities(operation).filter(
    (capability) => enabled.get(capability) !== true,
  );
  if (!missing.length) return null;
  return `${missing.map((capability) => CAPABILITY_LABEL[capability]).join(" 및 ")}가 시작되지 않았습니다. capability owner를 시작한 뒤 다시 시도하세요.`;
}

export function debugControlCapabilityAvailable(
  status: IntegrationDebugStatus | null): boolean {
  const rows = status?.capabilities;
  return !rows?.length || rows.some(
    (capability) => capability.name === "control" && capability.enabled,
  );
}
