import { AnimatePresence } from "framer-motion";
import * as m from "framer-motion/m";
import {
  Film,
  Gauge,
  MessageSquareText,
} from "lucide-react";

import type { useDigitalTwinViewModel } from "../../hooks/useDigitalTwinViewModel";
import type {
  ShadowGroundTruthState,
  ShadowReplayState,
  SpeechUtterance,
} from "../../types";
import type {
  ShadowReplayMode,
} from "../../hooks/useRosBridge";
import { type Language } from "../../utils/display";
import { MOTION_DURATION, SILK_EASE } from "../../motion-system";
import {
  PublicSurgeonGestureStatus,
  type PublicSurgeonGesture,
} from "./PublicSurgeonGestureStatus";

type ViewModel = ReturnType<typeof useDigitalTwinViewModel>;
const SHADOW_CASE_IDS = Array.from(
  { length: 12 },
  (_, index) => `0704_${index + 6}`,
);

function durationLabel(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "00:00.0";
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${remainder
    .toFixed(1)
    .padStart(4, "0")}`;
}

function sourceTime(utterance: SpeechUtterance): number {
  return (
    Number(utterance.start_stamp?.sec ?? 0) +
    Number(utterance.start_stamp?.nanosec ?? 0) / 1_000_000_000
  );
}

function replayStateLabel(
  state: ShadowReplayState,
  language: Language,
): string {
  const normalizedState = state.state.trim().toLowerCase();
  const drainTimedOut = state.hold_reason === "drain_timeout";

  if (normalizedState === "timed_out" || drainTimedOut) {
    return language === "ko" ? "실패 · 정리 시간 초과" : "Failed · Drain timeout";
  }
  if (state.completed || normalizedState === "completed") {
    return language === "ko" ? "완료" : "Complete";
  }
  if (normalizedState === "paused" || state.paused) {
    return language === "ko" ? "사용자 일시정지" : "Operator paused";
  }
  if (normalizedState === "held") {
    return language === "ko" ? "재생 대기" : "Playback waiting";
  }
  if (normalizedState === "draining") {
    return language === "ko" ? "마무리 중" : "Finishing";
  }
  if (
    (normalizedState === "running" || state.running) &&
    state.playback_rate > 0 &&
    state.playback_rate < 0.99
  ) {
    return language === "ko" ? "재생 중" : "Playing";
  }

  const ko: Record<string, string> = {
    ready: "준비",
    running: "재생 중",
    stopped: "정지",
    blocked: "차단됨",
    timed_out: "시간 초과",
    error: "오류",
    loading: "불러오는 중",
  };
  const en: Record<string, string> = {
    ready: "Ready",
    running: "Playing",
    stopped: "Stopped",
    blocked: "Blocked",
    timed_out: "Timed out",
    error: "Error",
    loading: "Loading",
  };
  return (
    (language === "ko" ? ko : en)[normalizedState] ||
    state.state ||
    (language === "ko" ? "대기" : "Idle")
  );
}

function playbackRateLabel(
  state: ShadowReplayState,
): string {
  const rate = Number.isFinite(state.playback_rate)
    ? Math.max(0, state.playback_rate)
    : 0;
  return `${rate.toFixed(1)}x`;
}

export function ShadowReplayDock({
  vm,
  language,
  state,
  transcript,
  connected,
  actionPending,
  groundTruth,
  onCaseChange,
  onConfigure,
}: {
  vm: ViewModel;
  language: Language;
  state: ShadowReplayState;
  transcript: SpeechUtterance[];
  connected: boolean;
  actionPending: string;
  groundTruth: ShadowGroundTruthState;
  onCaseChange: (caseId: string) => void;
  onConfigure: (mode: ShadowReplayMode, playbackRate: number) => void;
}) {
  const disabled =
    !connected || Boolean(actionPending) || state.running || state.paused;
  const mode =
    state.mode === "realtime_1x" ? "realtime_1x" : "elastic_demo";
  const latestTranscript = transcript[0]?.utterance_id ?? "";
  const selectedCaseId = state.case_id || SHADOW_CASE_IDS[0];
  const caseOptions = SHADOW_CASE_IDS.includes(selectedCaseId)
    ? SHADOW_CASE_IDS
    : [selectedCaseId, ...SHADOW_CASE_IDS];
  const phaseLabel =
    groundTruth.phase.active && groundTruth.phase.phaseId
      ? vm.displayPhaseName(groundTruth.phase.phaseId)
      : vm.ui.none;
  const phaseInterval =
    groundTruth.phase.active && groundTruth.phase.phaseId
      ? `${durationLabel(groundTruth.phase.startSec)} – ${durationLabel(
          groundTruth.phase.endSec,
        )}`
      : language === "ko"
        ? "활성 정답 구간 없음"
        : "No active ground-truth interval";
  const groundTruthSurgeonGesture: PublicSurgeonGesture = {
    eventType: groundTruth.active ? "implicit_tool_request" : "",
    handPose: groundTruth.active ? "hand_extending" : "",
    confidence: groundTruth.active ? 1 : 0,
    requestedTool: "",
  };

  return (
    <aside className="dock surgeon-dock llm-surgeon-dock shadow-replay-dock">
      <div className="dock-header">
        <div>
          <p className="section-kicker">
            {language === "ko" ? "실제 수술 재생" : "Recorded surgery"}
          </p>
          <h2>{replayStateLabel(state, language)}</h2>
        </div>
        <Film size={18} />
      </div>

      <label className="shadow-case-picker">
        <span>{language === "ko" ? "재생 케이스" : "Replay case"}</span>
        <select
          aria-label={language === "ko" ? "재생 케이스 선택" : "Select replay case"}
          disabled={disabled}
          onChange={(event) => onCaseChange(event.target.value)}
          value={selectedCaseId}
        >
          {caseOptions.map((caseId) => (
            <option key={caseId} value={caseId}>
              {caseId}
            </option>
          ))}
        </select>
      </label>

      <div className="shadow-replay-mode-row">
        <span>{language === "ko" ? "재생 정책" : "Playback policy"}</span>
        <div className="shadow-mode-control" role="group">
          <button
            className={mode === "elastic_demo" ? "active" : ""}
            disabled={disabled}
            onClick={() => onConfigure("elastic_demo", state.playback_rate || 1)}
            type="button"
          >
            {language === "ko" ? "동기화" : "Elastic"}
          </button>
          <button
            className={mode === "realtime_1x" ? "active" : ""}
            disabled={disabled}
            onClick={() => onConfigure("realtime_1x", state.playback_rate || 1)}
            type="button"
          >
            {language === "ko" ? "실시간" : "Realtime"}
          </button>
        </div>
      </div>

      <div className="intent-state llm-intent-state shadow-replay-stats">
        <article>
          <span>{language === "ko" ? "영상 시각" : "Source time"}</span>
          <strong>
            {durationLabel(state.source_time_sec)} / {durationLabel(state.duration_sec)}
          </strong>
        </article>
        <article>
          <span>{language === "ko" ? "실행 경과" : "Wall time"}</span>
          <strong>{durationLabel(state.wall_elapsed_sec)}</strong>
        </article>
      </div>

      <section
        className="shadow-ground-truth"
        aria-label={language === "ko" ? "정답 어노테이션" : "Ground-truth annotations"}
      >
        <div className="shadow-ground-truth-header">
          <span>{language === "ko" ? "정답 어노테이션" : "Ground truth"}</span>
          <small>
            {groundTruth.available
              ? language === "ko"
                ? "현재 어노테이션"
                : "Current annotation"
              : language === "ko"
                ? "대기"
                : "Waiting"}
          </small>
        </div>
        <article className="shadow-ground-truth-phase">
          <span>
            {language === "ko"
              ? "정답 단계(현재 어노테이션)"
              : "Annotated phase (current)"}
          </span>
          <strong>{phaseLabel}</strong>
          <small>{phaseInterval}</small>
        </article>
        <PublicSurgeonGestureStatus
          evidence={groundTruthSurgeonGesture}
          language={language}
          toolLabel=""
          label={
            language === "ko"
              ? "정답 이벤트 · 암묵적 도구 요청"
              : "Ground truth · implicit request"
          }
        />
      </section>

      <div className="llm-speech-log shadow-transcript-log">
        <div className="llm-speech-log-header">
          <span>
            <MessageSquareText size={15} />
            {language === "ko" ? "타임스탬프 음성 전사" : "Timestamped transcript"}
          </span>
          <small>{transcript.length || vm.ui.none}</small>
        </div>
        <div className="llm-speech-log-list" aria-live="polite">
          <AnimatePresence initial={false}>
            {transcript.map((item) => (
              <m.article
                key={item.utterance_id}
                className="llm-speech-log-item shadow-transcript-item"
                layout
                initial={{ opacity: 0, x: -12 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 8 }}
                transition={{
                  opacity: { duration: MOTION_DURATION.normal },
                  x: { duration: MOTION_DURATION.normal, ease: SILK_EASE },
                  layout: { duration: MOTION_DURATION.moderate, ease: SILK_EASE },
                }}
                data-latest={
                  item.utterance_id === latestTranscript ? "true" : "false"
                }
              >
                <time>{durationLabel(sourceTime(item))}</time>
                <div>
                  <strong>{item.text}</strong>
                  <small>{item.speaker_role || "surgeon"}</small>
                </div>
              </m.article>
            ))}
          </AnimatePresence>
          {transcript.length === 0 ? (
            <div className="llm-speech-log-empty">
              {language === "ko"
                ? "재생된 발화가 없습니다"
                : "No utterance has been replayed"}
            </div>
          ) : null}
        </div>
      </div>

      <div className="llm-meta-row shadow-replay-meta">
        <span>
          <Gauge size={14} />
          {playbackRateLabel(state)}
        </span>
      </div>
      {state.last_error ? (
        <div className="llm-reason error">{state.last_error}</div>
      ) : null}
    </aside>
  );
}
