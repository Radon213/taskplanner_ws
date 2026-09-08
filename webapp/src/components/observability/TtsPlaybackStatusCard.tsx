import { AlertTriangle, CheckCircle2, Clock3, Volume2 } from "lucide-react";

import type { TtsPlaybackStatus } from "../../types";
import type { Language } from "../../utils/display";
import "./tts-playback-status-card.css";

function stateLabel(status: TtsPlaybackStatus, language: Language): string {
  if (language === "en") return status.state.replace(/_/g, " ");
  if (status.state === "queued") return "대기열";
  if (status.state === "waiting_function_accepted") return "요청 승인 대기";
  if (status.state === "waiting_function_completed") return "요청 완료 대기";
  if (status.state === "playing") return "재생 중";
  if (status.state === "played") return "재생 완료";
  if (status.state === "duplicate_suppressed") return "중복 억제";
  return "재생 실패";
}

export function TtsPlaybackStatusCard({
  status,
  language,
}: {
  status: TtsPlaybackStatus | null;
  language: Language;
}) {
  const tone = !status
    ? "waiting"
    : status.state === "failed"
      ? "error"
      : status.state === "played" || status.state === "duplicate_suppressed"
        ? "success"
        : "active";
  const StateIcon = tone === "error"
    ? AlertTriangle
    : tone === "success"
      ? CheckCircle2
      : Clock3;

  return (
    <section
      aria-atomic="true"
      aria-live="polite"
      className={`tts-playback-status ${tone}`}
      data-slot="tts-playback-status"
      data-tts-state={status?.state ?? "waiting"}
      role="status"
    >
      <header>
        <span><Volume2 aria-hidden="true" size={15} />TTS</span>
        <strong>{status ? stateLabel(status, language) : language === "ko" ? "재생 상태 대기" : "Waiting for playback"}</strong>
        <StateIcon aria-hidden="true" size={15} />
      </header>
      {status ? (
        <>
          <p title={status.text}>{status.text || status.message || (language === "ko" ? "출력 문장 없음" : "No playback text")}</p>
          <dl>
            <div><dt>{language === "ko" ? "음성" : "Voice"}</dt><dd>{status.voiceId || "-"}</dd></div>
            <div><dt>{language === "ko" ? "합성" : "Synthesis"}</dt><dd>{Math.round(status.synthLatencyMs)} ms</dd></div>
            <div><dt>{language === "ko" ? "재생" : "Playback"}</dt><dd>{Math.round(status.playbackLatencyMs)} ms</dd></div>
          </dl>
          {status.errorCode ? <small>{status.errorCode} · {status.message}</small> : null}
        </>
      ) : (
        <p>{language === "ko" ? "실제 /tts/playback_status 수신 전입니다." : "No /tts/playback_status event received yet."}</p>
      )}
    </section>
  );
}
