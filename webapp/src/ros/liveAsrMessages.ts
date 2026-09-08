import type {
  LiveAsrDevice,
  LiveAsrFinal,
  LiveAsrLanHealth,
  LiveAsrRoutePolicy,
  LiveAsrStatus,
} from "../types";
import {
  finiteNumber,
  isBoundedRosPayload,
  optionalFiniteNumber,
} from "./rosMessageBounds";

const MAX_LIVE_ASR_STATUS_JSON_CHARS = 256 * 1024;
const MAX_LIVE_ASR_DEVICES = 64;
const MAX_LIVE_ASR_FINALS = 48;
const MAX_LIVE_ASR_DISPLAY_TEXT_CHARS = 4_096;

type RosString = {
  data?: string;
};

type RosTime = {
  sec?: number;
  nanosec?: number;
};

type SpeechUtteranceWire = {
  stamp?: RosTime;
  utterance_id?: string;
  text?: string;
  is_final?: boolean;
};

/** Stable empty projection used while the ASR status topic is unavailable. */
export const DEFAULT_LIVE_ASR_STATUS: LiveAsrStatus = {
  schema: "taskplanner.asr.status.v1",
  stamp_sec: 0,
  node_instance_id: "",
  node_started_at_sec: 0,
  source_revision: "",
  available: false,
  dependency_error: "",
  state: "UNAVAILABLE",
  server_url: "",
  topic: "",
  output_mode: "",
  output_topic: "",
  device_id: null,
  device_name: "",
  devices: [],
  device_status: "NO_INPUT",
  device_message: "ASR 상태를 기다리는 중입니다.",
  connected: false,
  recording_active: false,
  audio_level_dbfs: -99,
  peak_level_dbfs: -99,
  elapsed_sec: 0,
  partial_text: "",
  local_onset_to_first_partial_ms: null,
  local_onset_basis: "",
  local_onset_dbfs: null,
  local_onset_threshold_dbfs: null,
  finals: [],
  last_error: "",
  sample_rate: 16000,
  channels: 1,
  sample_width_bits: 16,
  endpoint_id: "",
  route_policy: "cloud",
  selection_reason: "",
  lan_health: {
    enabled: false,
    state: "UNKNOWN",
    method: "websocket_handshake",
    age_ms: null,
    latency_ms: null,
    consecutive_failures: 0,
    last_error: "",
  },
};

function normalizeLiveAsrRoutePolicy(value: unknown): LiveAsrRoutePolicy | null {
  if (typeof value !== "string") return null;
  const policy = value;
  return policy === "lan" || policy === "auto" || policy === "cloud"
    ? policy
    : null;
}

function normalizeLiveAsrLanHealth(value: unknown): LiveAsrLanHealth {
  const health = value && typeof value === "object"
    ? value as Record<string, unknown>
    : {};
  const age = optionalFiniteNumber(health.age_ms);
  const latency = optionalFiniteNumber(health.latency_ms);
  return {
    enabled: Boolean(health.enabled),
    state: String(health.state ?? "UNKNOWN").toUpperCase().slice(0, 64),
    method: String(health.method ?? "websocket_handshake").slice(0, 128),
    age_ms: age === null ? null : Math.max(0, age),
    latency_ms: latency === null ? null : Math.max(0, latency),
    consecutive_failures: Math.max(0, Math.trunc(finiteNumber(health.consecutive_failures))),
    last_error: String(health.last_error ?? "").slice(0, MAX_LIVE_ASR_DISPLAY_TEXT_CHARS),
  };
}

function normalizeLiveAsrDevice(value: unknown): LiveAsrDevice | null {
  if (!value || typeof value !== "object") return null;
  const device = value as Record<string, unknown>;
  const id = Number(device.id);
  if (!Number.isInteger(id)) return null;
  return {
    id,
    name: String(device.name ?? `Input ${id}`).slice(0, 512),
    input_channels: Math.max(0, Math.trunc(finiteNumber(device.input_channels))),
    default_samplerate: Math.max(0, finiteNumber(device.default_samplerate)),
    default: Boolean(device.default),
  };
}

function normalizeLiveAsrFinal(value: unknown): LiveAsrFinal | null {
  if (!value || typeof value !== "object") return null;
  const final = value as Record<string, unknown>;
  const text = String(final.text ?? "").trim().slice(0, MAX_LIVE_ASR_DISPLAY_TEXT_CHARS);
  if (!text) return null;
  const latencyMissing = final.response_latency_ms === null
    || final.response_latency_ms === undefined
    || final.response_latency_ms === "";
  const rawLatency = latencyMissing ? Number.NaN : Number(final.response_latency_ms);
  return {
    stamp: String(final.stamp ?? "").slice(0, 128),
    text,
    response_latency_ms: Number.isFinite(rawLatency) && rawLatency >= 0 ? rawLatency : null,
    latency_basis: String(final.latency_basis ?? "unavailable").slice(0, 128),
    latency_correlated: Boolean(final.latency_correlated),
  };
}

function rosTimeIso(value: RosTime | undefined): string {
  const seconds = finiteNumber(value?.sec);
  const nanoseconds = finiteNumber(value?.nanosec);
  const milliseconds = seconds * 1_000 + nanoseconds / 1_000_000;
  return milliseconds > 0 && Number.isFinite(milliseconds)
    ? new Date(milliseconds).toISOString()
    : "";
}

/** Normalize the non-executable partial transcript emitted by the adapter. */
export function normalizeExternalAsrPartial(message: unknown): string | null {
  if (!isBoundedRosPayload(message) || !message || typeof message !== "object") {
    return null;
  }
  const utterance = message as SpeechUtteranceWire;
  if (utterance.is_final !== false) return null;
  const text = String(utterance.text ?? "").trim();
  if (!text || text.length > MAX_LIVE_ASR_DISPLAY_TEXT_CHARS) return null;
  return text;
}

/** Normalize one CommandRouter-observed final for the recent-final list. */
export function normalizeExternalAsrFinal(message: unknown): LiveAsrFinal | null {
  if (!isBoundedRosPayload(message) || !message || typeof message !== "object") {
    return null;
  }
  const utterance = message as SpeechUtteranceWire;
  if (utterance.is_final !== true || !String(utterance.utterance_id ?? "").trim()) {
    return null;
  }
  const text = String(utterance.text ?? "").trim();
  if (!text || text.length > MAX_LIVE_ASR_DISPLAY_TEXT_CHARS) return null;
  return {
    stamp: rosTimeIso(utterance.stamp),
    text,
    response_latency_ms: null,
    latency_basis: "external_ros_topic",
    latency_correlated: false,
  };
}

/** Overlay external ROS transcripts without losing the ASR runtime heartbeat. */
export function mergeExternalAsrTranscripts(
  status: LiveAsrStatus,
  partialText: string | null,
  externalFinals: readonly LiveAsrFinal[],
): LiveAsrStatus {
  const finals = [...status.finals];
  for (const externalFinal of externalFinals) {
    const duplicateIndex = finals.findIndex((candidate) => (
      candidate.stamp === externalFinal.stamp
      && candidate.text === externalFinal.text
    ));
    if (duplicateIndex >= 0) finals.splice(duplicateIndex, 1);
    finals.push(externalFinal);
  }
  return {
    ...status,
    partial_text: partialText === null ? status.partial_text : partialText,
    finals: finals.slice(-MAX_LIVE_ASR_FINALS),
  };
}

/**
 * Validate the versioned JSON envelope published on the read-only ASR status
 * topic. Malformed or oversized messages are rejected instead of being mixed
 * with the last authoritative snapshot.
 */
export function normalizeLiveAsrStatus(message: unknown): LiveAsrStatus | null {
  const raw = String((message as RosString | null)?.data ?? "");
  if (raw.length > MAX_LIVE_ASR_STATUS_JSON_CHARS) return null;
  try {
    const envelope = JSON.parse(raw) as Record<string, unknown>;
    if (!isBoundedRosPayload(envelope)) return null;
    if (envelope.schema !== "taskplanner.asr.status.v1") return null;
    const snapshot = envelope.asr && typeof envelope.asr === "object"
      ? envelope.asr as Record<string, unknown>
      : {};
    const rawDevices = Array.isArray(snapshot.devices)
      ? snapshot.devices.slice(0, MAX_LIVE_ASR_DEVICES)
      : [];
    const rawFinals = Array.isArray(snapshot.finals)
      ? snapshot.finals.slice(-MAX_LIVE_ASR_FINALS)
      : [];
    const rawDeviceId = snapshot.device_id;
    const routePolicy = normalizeLiveAsrRoutePolicy(snapshot.route_policy);
    if (
      typeof snapshot.available !== "boolean"
      || typeof snapshot.connected !== "boolean"
      || typeof snapshot.recording_active !== "boolean"
      || routePolicy === null
    ) return null;
    return {
      ...DEFAULT_LIVE_ASR_STATUS,
      schema: "taskplanner.asr.status.v1",
      stamp_sec: finiteNumber(envelope.stamp_sec),
      node_instance_id: /^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i.test(
        String(envelope.node_instance_id ?? ""),
      ) ? String(envelope.node_instance_id) : "",
      node_started_at_sec: Math.max(0, finiteNumber(envelope.node_started_at_sec)),
      source_revision: /^[a-f0-9]{64}$/.test(String(envelope.source_revision ?? ""))
        ? String(envelope.source_revision)
        : "",
      available: snapshot.available,
      dependency_error: String(snapshot.dependency_error ?? "").slice(0, MAX_LIVE_ASR_DISPLAY_TEXT_CHARS),
      state: String(snapshot.state ?? "UNAVAILABLE").toUpperCase().slice(0, 64),
      server_url: String(snapshot.server_url ?? "").slice(0, 2048),
      topic: String(snapshot.topic ?? "").slice(0, 512),
      output_mode: String(snapshot.output_mode ?? "").trim().slice(0, 64),
      output_topic: String(snapshot.output_topic ?? "").slice(0, 512),
      device_id: rawDeviceId === null || rawDeviceId === undefined || rawDeviceId === ""
        ? null
        : Number.isInteger(Number(rawDeviceId))
          ? Number(rawDeviceId)
          : null,
      device_name: String(snapshot.device_name ?? "").slice(0, 512),
      devices: rawDevices.map(normalizeLiveAsrDevice).filter((value): value is LiveAsrDevice => value !== null),
      device_status: String(snapshot.device_status ?? "NO_INPUT").slice(0, 64),
      device_message: String(snapshot.device_message ?? "").slice(0, MAX_LIVE_ASR_DISPLAY_TEXT_CHARS),
      connected: snapshot.connected,
      recording_active: snapshot.recording_active,
      audio_level_dbfs: finiteNumber(snapshot.audio_level_dbfs, -99),
      peak_level_dbfs: finiteNumber(snapshot.peak_level_dbfs, -99),
      elapsed_sec: Math.max(0, finiteNumber(snapshot.elapsed_sec)),
      partial_text: String(snapshot.partial_text ?? "").slice(0, MAX_LIVE_ASR_DISPLAY_TEXT_CHARS),
      local_onset_to_first_partial_ms: (() => {
        const value = optionalFiniteNumber(snapshot.local_onset_to_first_partial_ms);
        return value === null ? null : Math.max(0, value);
      })(),
      local_onset_basis: String(snapshot.local_onset_basis ?? "").slice(0, 256),
      local_onset_dbfs: optionalFiniteNumber(snapshot.local_onset_dbfs),
      local_onset_threshold_dbfs: optionalFiniteNumber(snapshot.local_onset_threshold_dbfs),
      finals: rawFinals.map(normalizeLiveAsrFinal).filter((value): value is LiveAsrFinal => value !== null),
      last_error: String(snapshot.last_error ?? "").slice(0, MAX_LIVE_ASR_DISPLAY_TEXT_CHARS),
      sample_rate: Math.max(0, finiteNumber(snapshot.sample_rate, 16000)),
      channels: Math.max(0, Math.trunc(finiteNumber(snapshot.channels, 1))),
      sample_width_bits: Math.max(0, Math.trunc(finiteNumber(snapshot.sample_width_bits, 16))),
      endpoint_id: String(snapshot.endpoint_id ?? "").slice(0, 64),
      route_policy: routePolicy,
      selection_reason: String(snapshot.selection_reason ?? "").slice(0, MAX_LIVE_ASR_DISPLAY_TEXT_CHARS),
      lan_health: normalizeLiveAsrLanHealth(snapshot.lan_health),
    };
  } catch {
    return null;
  }
}
