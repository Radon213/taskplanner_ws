import { useEffect, useMemo, useState } from "react";
import {
  AudioLines,
  CircleStop,
  LoaderCircle,
  Mic,
  MicOff,
  RefreshCw,
  Server,
} from "lucide-react";

import type { LiveAsrControlResult, LiveAsrStatus } from "../../types";
import type { Language } from "../../utils/display";

type LiveAsrOperation = "refresh_devices" | "start" | "stop";

function formatLatency(value: number | null | undefined, language: Language): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${value.toFixed(1)} ms`
    : language === "ko" ? "측정 전" : "Not measured";
}

function formatEventTime(value: string, language: Language): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "--:--:--";
  return parsed.toLocaleTimeString(language === "ko" ? "ko-KR" : "en-US", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatDeviceMessage(status: LiveAsrStatus, language: Language): string {
  if (language !== "ko") {
    return status.device_message || "Check the Ubuntu input setting, then refresh.";
  }
  if (status.device_status === "NO_INPUT") {
    return "현재 Ubuntu에 선택 가능한 마이크 입력이 없습니다. USB 마이크를 연결하고 Ubuntu 입력 장치로 선택한 뒤 새로고침하세요.";
  }
  if (status.device_status === "HOST_AUDIO_UNAVAILABLE") {
    return "Ubuntu 오디오 서비스에 연결할 수 없습니다. PipeWire 연결과 컨테이너 오디오 소켓을 확인하세요.";
  }
  if (status.device_status === "BRIDGE_ERROR") {
    return "마이크 연결 계층 오류입니다. Ubuntu 입력 설정과 PipeWire 브리지를 확인한 뒤 새로고침하세요.";
  }
  return "Ubuntu 마이크 입력 상태를 확인한 뒤 새로고침하세요.";
}

export function LiveAsrPanel({
  status,
  statusReceivedAt,
  connected,
  pendingOperation,
  controlMessage,
  language,
  onControl,
}: {
  status: LiveAsrStatus;
  statusReceivedAt: number | null;
  connected: boolean;
  pendingOperation: string;
  controlMessage: string;
  language: Language;
  onControl: (operation: LiveAsrOperation, deviceId?: number) => Promise<LiveAsrControlResult>;
}) {
  const preferredDeviceId = status.device_id
    ?? status.devices.find((device) => device.default)?.id
    ?? status.devices[0]?.id
    ?? -1;
  const [selectedDeviceId, setSelectedDeviceId] = useState(preferredDeviceId);
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (status.devices.some((device) => device.id === selectedDeviceId)) return;
    setSelectedDeviceId(preferredDeviceId);
  }, [preferredDeviceId, selectedDeviceId, status.devices]);

  const selectedDevice = status.devices.find((device) => device.id === selectedDeviceId);
  const recentFinals = useMemo(() => [...status.finals].reverse().slice(0, 3), [status.finals]);
  const latestFinal = recentFinals[0];
  const asrActive = ["STARTING", "LISTENING", "STOPPING"].includes(status.state);
  const listening = status.state === "LISTENING";
  const statusFresh = statusReceivedAt !== null && nowMs - statusReceivedAt <= 5000;
  const startDisabled = !connected
    || !statusFresh
    || !status.available
    || !selectedDevice
    || asrActive
    || Boolean(pendingOperation);
  const stopDisabled = !connected || !asrActive || Boolean(pendingOperation);
  const refreshDisabled = !connected || !statusFresh || asrActive || Boolean(pendingOperation);
  const selectorDisabled = !connected || !statusFresh || asrActive || Boolean(pendingOperation);
  const levelPercent = Math.max(0, Math.min(100, ((status.audio_level_dbfs + 60) / 60) * 100));
  const statusLabel = language === "ko"
    ? listening ? "마이크 캡처 중" : asrActive ? "ASR 전환 중" : status.state === "ERROR" ? "ASR 오류" : "ASR 정지"
    : listening ? "Microphone capturing" : asrActive ? "ASR transitioning" : status.state === "ERROR" ? "ASR error" : "ASR stopped";

  return (
    <section className={`live-asr-panel ${listening ? "capturing" : ""}`} data-slot="live-asr-panel" aria-labelledby="live-asr-title">
      <div className="live-asr-header">
        <div>
          <p className="section-kicker">USB ASR</p>
          <h3 id="live-asr-title">{language === "ko" ? "수술실 음성 입력" : "Operating-room speech"}</h3>
        </div>
        <div className={`live-asr-state ${listening ? "active" : "idle"}`} aria-live="polite" aria-atomic="true">
          {pendingOperation ? <LoaderCircle className="live-asr-spinner" size={17} aria-hidden="true" /> : listening ? <Mic size={17} aria-hidden="true" /> : <MicOff size={17} aria-hidden="true" />}
          <span>{pendingOperation ? (language === "ko" ? "요청 처리 중" : "Applying request") : statusLabel}</span>
        </div>
      </div>

      <div className="live-asr-controls">
        <label className="field live-asr-device-field" htmlFor="live-asr-device">
          <span>{language === "ko" ? "Ubuntu 현재 USB 입력" : "Current Ubuntu USB input"}</span>
          <select
            id="live-asr-device"
            value={selectedDeviceId}
            disabled={selectorDisabled}
            aria-describedby="live-asr-device-help"
            onChange={(event) => setSelectedDeviceId(Number(event.target.value))}
          >
            {!status.devices.length ? <option value={-1}>{language === "ko" ? "사용 가능한 입력 장치 없음" : "No input device available"}</option> : null}
            {status.devices.map((device) => <option value={device.id} key={device.id}>{device.name}</option>)}
          </select>
          <small id="live-asr-device-help">
            {selectedDevice
              ? `${selectedDevice.input_channels} ch · ${selectedDevice.default_samplerate.toLocaleString()} Hz`
              : formatDeviceMessage(status, language)}
          </small>
        </label>
        <div className="live-asr-actions" aria-label={language === "ko" ? "음성 인식 제어" : "Speech recognition controls"}>
          <button className="button button-quiet" disabled={refreshDisabled} onClick={() => void onControl("refresh_devices", selectedDeviceId)} type="button">
            {pendingOperation === "refresh_devices" ? <LoaderCircle className="live-asr-spinner" size={16} aria-hidden="true" /> : <RefreshCw size={16} aria-hidden="true" />}
            {language === "ko" ? "장치 새로고침" : "Refresh devices"}
          </button>
          <button className="button button-primary" disabled={startDisabled} onClick={() => void onControl("start", selectedDeviceId)} type="button">
            {pendingOperation === "start" ? <LoaderCircle className="live-asr-spinner" size={16} aria-hidden="true" /> : <Mic size={16} aria-hidden="true" />}
            {language === "ko" ? "ASR 시작" : "Start ASR"}
          </button>
          <button className="button button-secondary" disabled={stopDisabled} onClick={() => void onControl("stop", selectedDeviceId)} type="button">
            {pendingOperation === "stop" ? <LoaderCircle className="live-asr-spinner" size={16} aria-hidden="true" /> : <CircleStop size={16} aria-hidden="true" />}
            {language === "ko" ? "ASR 중지" : "Stop ASR"}
          </button>
        </div>
      </div>

      <div className="live-asr-live" aria-live="polite">
        <div className="live-asr-meter-copy">
          <span><AudioLines size={15} aria-hidden="true" />{language === "ko" ? "입력 레벨" : "Input level"}</span>
          <strong>{status.audio_level_dbfs.toFixed(1)} dBFS</strong>
        </div>
        <div className="live-asr-meter" role="meter" aria-label={language === "ko" ? "마이크 입력 레벨" : "Microphone input level"} aria-valuemin={-60} aria-valuemax={0} aria-valuenow={Math.max(-60, Math.min(0, status.audio_level_dbfs))}>
          <span style={{ width: `${levelPercent}%` }} />
        </div>
        <p><span>{language === "ko" ? "부분 인식" : "Partial"}</span><strong>{status.partial_text || (listening ? (language === "ko" ? "음성 대기 중…" : "Waiting for speech…") : (language === "ko" ? "ASR 시작 전" : "ASR not started"))}</strong></p>
      </div>

      <div className="live-asr-facts">
        <span><Server size={14} aria-hidden="true" />{status.connected ? (language === "ko" ? "ASR 서버 연결됨" : "ASR server connected") : (language === "ko" ? "ASR 서버 미연결" : "ASR server disconnected")}</span>
        <code title={status.server_url}>{status.server_url || (language === "ko" ? "서버 주소 대기" : "Waiting for server URL")}</code>
        <code>{status.topic || "/sensors/surgeon/sentence"} · std_msgs/msg/String</code>
      </div>

      <div className="live-asr-finals">
        <div>
          <span>{language === "ko" ? "최근 확정 문장" : "Recent finalized speech"}</span>
          <small title={language === "ko" ? "마지막 오디오 청크 송신 완료부터 final 응답 수신까지의 참고 간격입니다." : "Reference interval from the latest audio chunk send completion to final response receipt."}>
            {language === "ko" ? "참고 latency" : "Reference latency"} {formatLatency(latestFinal?.response_latency_ms, language)}
          </small>
        </div>
        {recentFinals.length ? (
          <ol>{recentFinals.map((row, index) => (
            <li key={`${row.stamp}-${index}`}>
              <time dateTime={row.stamp}>{formatEventTime(row.stamp, language)}</time>
              <span>{row.text}</span>
              <data value={row.response_latency_ms ?? undefined}>{formatLatency(row.response_latency_ms, language)}</data>
            </li>
          ))}</ol>
        ) : <p>{language === "ko" ? "아직 확정된 문장이 없습니다." : "No finalized speech yet."}</p>}
      </div>

      {controlMessage || status.last_error || !statusFresh ? (
        <p className={`live-asr-message ${status.last_error || !statusFresh ? "error" : "normal"}`} role={status.last_error ? "alert" : "status"}>
          {status.last_error || (!statusFresh ? (language === "ko" ? "ASR 상태 토픽을 기다리는 중입니다." : "Waiting for the ASR status topic.") : controlMessage)}
        </p>
      ) : null}
      <p className="sr-only" aria-live="polite" aria-atomic="true">
        {statusLabel}. {latestFinal ? `${language === "ko" ? "최근 확정 문장" : "Latest final"}: ${latestFinal.text}. ${formatLatency(latestFinal.response_latency_ms, language)}.` : ""}
      </p>
    </section>
  );
}
