import type { TtsPlaybackStatus } from "../types";
import {
  isBoundedRosPayload,
  MAX_ROS_PAYLOAD_STRING_CHARS,
} from "./rosMessageBounds";

export const TTS_PLAYBACK_STATUS_TOPIC = "/tts/playback_status";

const STATES = new Set<TtsPlaybackStatus["state"]>([
  "queued",
  "waiting_function_accepted",
  "waiting_function_completed",
  "playing",
  "played",
  "duplicate_suppressed",
  "failed",
]);
const TIMINGS = new Set<TtsPlaybackStatus["timing"]>([
  "immediate",
  "on_function_accepted",
  "on_function_completed",
]);
const MAX_TEXT_CHARS = 4_096;
const MAX_ID_CHARS = 256;

function boundedText(value: unknown, maxChars = MAX_ID_CHARS): string | null {
  if (typeof value !== "string" || value.length > maxChars) return null;
  return value;
}

function nonNegativeNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

export function normalizeTtsPlaybackStatus(
  message: unknown,
  receivedAt = Date.now(),
): TtsPlaybackStatus | null {
  if (!isBoundedRosPayload(message) || !message || typeof message !== "object") return null;
  const value = message as Record<string, unknown>;
  const stamp = value.stamp as { sec?: unknown; nanosec?: unknown } | undefined;
  const stampSecPart = nonNegativeNumber(stamp?.sec);
  const stampNanosec = nonNegativeNumber(stamp?.nanosec);
  const sequence = nonNegativeNumber(value.sequence);
  const state = boundedText(value.state);
  const timing = boundedText(value.timing);
  const text = boundedText(value.text, MAX_TEXT_CHARS);
  const messageText = boundedText(value.message, MAX_ROS_PAYLOAD_STRING_CHARS);
  const synthLatencyMs = nonNegativeNumber(value.synth_latency_ms);
  const audioDurationSec = nonNegativeNumber(value.audio_duration_sec);
  const playbackLatencyMs = nonNegativeNumber(value.playback_latency_ms);
  if (
    stampSecPart === null || !Number.isSafeInteger(stampSecPart) ||
    stampNanosec === null || !Number.isSafeInteger(stampNanosec) || stampNanosec >= 1_000_000_000 ||
    sequence === null || !Number.isSafeInteger(sequence) || !state || !STATES.has(state as TtsPlaybackStatus["state"]) ||
    !timing || !TIMINGS.has(timing as TtsPlaybackStatus["timing"]) || text === null ||
    messageText === null || synthLatencyMs === null || audioDurationSec === null ||
    playbackLatencyMs === null || typeof value.terminal !== "boolean" ||
    typeof value.success !== "boolean"
  ) return null;

  const replyId = boundedText(value.reply_id);
  const turnId = boundedText(value.turn_id);
  const utteranceId = boundedText(value.utterance_id);
  const procedureRunId = boundedText(value.procedure_run_id);
  const voiceId = boundedText(value.voice_id);
  const outputDevice = boundedText(value.output_device);
  const errorCode = boundedText(value.error_code);
  if ([replyId, turnId, utteranceId, procedureRunId, voiceId, outputDevice, errorCode].some((item) => item === null)) {
    return null;
  }

  return {
    stampSec: stampSecPart + stampNanosec / 1_000_000_000,
    sequence,
    replyId: replyId as string,
    turnId: turnId as string,
    utteranceId: utteranceId as string,
    procedureRunId: procedureRunId as string,
    state: state as TtsPlaybackStatus["state"],
    timing: timing as TtsPlaybackStatus["timing"],
    text,
    voiceId: voiceId as string,
    outputDevice: outputDevice as string,
    synthLatencyMs,
    audioDurationSec,
    playbackLatencyMs,
    terminal: value.terminal,
    success: value.success,
    errorCode: errorCode as string,
    message: messageText,
    receivedAt,
  };
}
