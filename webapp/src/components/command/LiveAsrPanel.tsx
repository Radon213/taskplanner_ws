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

type LiveAsrOperation = "refresh_devices" | "set_route_policy" | "start" | "stop" | "restart_node";

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

function formatNodeStartedAt(value: number, language: Language): string {
  if (!Number.isFinite(value) || value <= 0) {
    return language === "ko" ? "시작 시각 대기" : "Start time pending";
  }
  return new Date(value * 1_000).toLocaleString(language === "ko" ? "ko-KR" : "en-US", {
    hour12: false,
    month: "2-digit",
    day: "2-digit",
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

function routePolicyLabel(policy: LiveAsrStatus["route_policy"], language: Language): string {
  if (language !== "ko") {
    if (policy === "lan") return "LAN only";
    if (policy === "auto") return "Auto · prefer LAN";
    return "Cloud only";
  }
  if (policy === "lan") return "LAN만";
  if (policy === "auto") return "자동 · LAN 우선";
  return "클라우드만";
}

function lanHealthSummary(status: LiveAsrStatus, language: Language): string {
  const health = status.lan_health;
  const age = health.age_ms === null ? "" : ` · ${(health.age_ms / 1000).toFixed(1)}${language === "ko" ? "초 전" : "s ago"}`;
  if (health.state === "READY") {
    const latency = health.latency_ms === null ? "" : ` · ${health.latency_ms.toFixed(1)} ms`;
    return language === "ko" ? `LAN 준비됨${latency}${age}` : `LAN ready${latency}${age}`;
  }
  if (health.state === "CHECKING") return language === "ko" ? "LAN 상태 확인 중" : "Checking LAN";
  if (health.state === "UNAVAILABLE") return language === "ko" ? `LAN 미준비${age}` : `LAN unavailable${age}`;
  if (health.state === "STALE") return language === "ko" ? "LAN 상태가 오래되었습니다" : "LAN status is stale";
  return language === "ko" ? "LAN 상태 대기 중" : "Waiting for LAN status";
}

function routeSelectionSummary(status: LiveAsrStatus, language: Language): string {
  const endpoint = status.endpoint_id === "lan" ? "LAN" : language === "ko" ? "클라우드" : "Cloud";
  const active = ["STARTING", "LISTENING", "STOPPING"].includes(status.state);
  if (status.route_policy === "auto" && status.endpoint_id === "cloud") {
    return language === "ko" ? "LAN 미준비 → 클라우드 대체" : "LAN unavailable → Cloud fallback";
  }
  if (status.route_policy === "lan" && status.lan_health.state !== "READY") {
    return language === "ko" ? "LAN 미준비 · ASR 시작 차단" : "LAN unavailable · ASR start blocked";
  }
  if (active) return language === "ko" ? `현재 세션: ${endpoint}` : `Current session: ${endpoint}`;
  return language === "ko" ? `다음 세션: ${endpoint}` : `Next session: ${endpoint}`;
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
  onControl: (
    operation: LiveAsrOperation,
    deviceId?: number,
    routePolicy?: LiveAsrStatus["route_policy"],
  ) => Promise<LiveAsrControlResult>;
}) {
  const preferredDeviceId = status.device_id
    ?? status.devices.find((device) => device.default)?.id
    ?? status.devices[0]?.id
    ?? -1;
  const [selectedDeviceId, setSelectedDeviceId] = useState(preferredDeviceId);
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    if (statusReceivedAt === null) return;

    // The panel only needs one repaint: when the last heartbeat crosses the
    // five-second freshness boundary. Avoid a permanent 1 Hz rerender loop
    // while the panel is mounted but no ASR status has ever arrived.
    const now = Date.now();
    setNowMs(now);
    const untilStale = Math.max(0, 5_000 - (now - statusReceivedAt));
    const timer = window.setTimeout(() => setNowMs(Date.now()), untilStale + 1);
    return () => window.clearTimeout(timer);
  }, [statusReceivedAt]);

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
  const statusStale = statusReceivedAt !== null && !statusFresh;
  const statusAwaiting = statusReceivedAt === null;
  const lanOnlyUnavailable = status.route_policy === "lan" && status.lan_health.state !== "READY";
  const startDisabled = !connected
    || !statusFresh
    || !status.available
    || !selectedDevice
    || lanOnlyUnavailable
    || asrActive
    || Boolean(pendingOperation);
  const stopDisabled = !connected || !asrActive || Boolean(pendingOperation);
  const refreshDisabled = !connected || !statusFresh || asrActive || Boolean(pendingOperation);
  const selectorDisabled = !connected || !statusFresh || asrActive || Boolean(pendingOperation);
  const routePolicyDisabled = !connected || !statusFresh || asrActive || Boolean(pendingOperation);
  const restartDisabled = !connected || Boolean(pendingOperation);
  const restartPending = pendingOperation === "restart_node";
  const restartFeedbackIsError = controlMessage.startsWith("ASR 노드 새로 시작 실패:");
  const restartFeedbackIsSuccess = controlMessage.startsWith("ASR 노드 새로 시작 완료");
  const restartSuccessIsCurrent = restartFeedbackIsSuccess && statusFresh;
  const levelPercent = Math.max(0, Math.min(100, ((status.audio_level_dbfs + 60) / 60) * 100));
  const startBlockerMessage = statusStale
    ? language === "ko"
      ? "ASR sidecar 상태가 5초 이상 갱신되지 않았습니다. /input/asr/control을 호출하지 않습니다. sidecar가 정상 상태를 다시 발행한 뒤 재시도하세요."
      : "The ASR sidecar status is more than five seconds old. /input/asr/control will not be called until it publishes a fresh status."
    : !connected
      ? language === "ko"
        ? "Live ROS bridge 연결을 기다리는 중입니다. 연결되면 ASR 상태와 제어 Service를 다시 확인합니다."
        : "Waiting for the Live ROS bridge. ASR status and control will be checked after it connects."
      : statusAwaiting
        ? language === "ko"
          ? "로컬 ASR sidecar의 상태·제어 Service를 기다리는 중입니다. taskplanner-asr가 준비되면 시작 버튼이 활성화됩니다."
          : "Waiting for the local ASR sidecar status and control service. Start will enable when taskplanner-asr is ready."
        : !status.available
          ? language === "ko"
            ? `ASR sidecar를 사용할 수 없습니다.${status.dependency_error ? ` ${status.dependency_error}` : ""}`
            : `The ASR sidecar is unavailable.${status.dependency_error ? ` ${status.dependency_error}` : ""}`
          : !selectedDevice
            ? formatDeviceMessage(status, language)
            : lanOnlyUnavailable
              ? language === "ko"
                ? "LAN ASR이 아직 준비되지 않았습니다. 자동 또는 클라우드 경로를 선택하거나 LAN 상태가 준비된 뒤 시작하세요."
                : "LAN ASR is not ready. Choose Auto or Cloud, or wait for LAN readiness before starting."
              : asrActive
                ? language === "ko"
                  ? "ASR 세션 전환이 완료될 때까지 기다리세요."
                  : "Wait for the ASR session transition to finish."
                : pendingOperation
                  ? language === "ko"
                    ? "ASR 요청을 처리하고 있습니다."
                    : "Applying the ASR request."
                  : "";
  const restartPendingMessage = language === "ko"
    ? "ASR 노드 다시 시작 중… 최신 Python 소스를 다시 읽고 새 heartbeat를 확인하고 있습니다."
    : "Restarting the ASR node, reloading the latest Python source, and verifying its new heartbeat."
  const panelMessage = restartPending
    ? restartPendingMessage
    : restartFeedbackIsError || restartSuccessIsCurrent
      ? controlMessage
      : status.last_error || startBlockerMessage || controlMessage;
  const panelMessageIsError = restartFeedbackIsError
    || (!restartSuccessIsCurrent && Boolean(status.last_error || statusStale));
  const statusLabel = language === "ko"
    ? statusStale
      ? "상태 지연"
      : statusAwaiting
        ? "상태 대기"
        : listening
          ? "마이크 캡처 중"
          : asrActive
            ? "ASR 전환 중"
            : status.state === "ERROR"
              ? "ASR 오류"
              : "ASR 정지"
    : statusStale
      ? "Status stale"
      : statusAwaiting
        ? "Waiting for status"
        : listening
          ? "Microphone capturing"
          : asrActive
            ? "ASR transitioning"
            : status.state === "ERROR"
              ? "ASR error"
              : "ASR stopped";
  const stateTone = statusStale ? "stale" : listening ? "active" : "idle";

  return (
    <section className={`live-asr-panel ${listening ? "capturing" : ""}`} data-slot="live-asr-panel" aria-labelledby="live-asr-title">
      <div className="live-asr-header">
        <div>
          <p className="section-kicker">USB ASR</p>
          <h3 id="live-asr-title">{language === "ko" ? "수술실 음성 입력" : "Operating-room speech"}</h3>
        </div>
        <div
          aria-atomic="true"
          aria-live="polite"
          className={`live-asr-state ${stateTone}`}
          data-status-fresh={statusFresh}
        >
          {pendingOperation ? <LoaderCircle className="live-asr-spinner" size={17} aria-hidden="true" /> : listening ? <Mic size={17} aria-hidden="true" /> : <MicOff size={17} aria-hidden="true" />}
          <span>{restartPending
            ? (language === "ko" ? "ASR 노드 다시 시작 중" : "Restarting ASR node")
            : pendingOperation
              ? (language === "ko" ? "요청 처리 중" : "Applying request")
              : statusLabel}</span>
        </div>
      </div>

      <fieldset className="live-asr-route-policy" data-slot="live-asr-route-policy" disabled={routePolicyDisabled}>
        <legend>{language === "ko" ? "ASR 전송 경로" : "ASR transport route"}</legend>
        <div>
          {(["cloud", "lan", "auto"] as const).map((policy) => (
            <label className={status.route_policy === policy ? "selected" : ""} key={policy}>
              <input
                checked={status.route_policy === policy}
                name="live-asr-route-policy"
                onChange={() => void onControl("set_route_policy", -1, policy)}
                type="radio"
                value={policy}
              />
              <span>
                <strong>{routePolicyLabel(policy, language)}</strong>
                <small>{policy === "cloud" ? "worker-02 · TLS" : policy === "lan" ? "192.168.1.5:1196" : language === "ko" ? "LAN 장애 시 클라우드" : "Cloud if LAN is unavailable"}</small>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <div className={`live-asr-route-summary ${status.lan_health.state.toLowerCase()}`} data-slot="live-asr-route-summary">
        <Server size={15} aria-hidden="true" />
        <div>
          <strong>{routeSelectionSummary(status, language)}</strong>
          <span>{lanHealthSummary(status, language)}</span>
        </div>
      </div>
      {status.route_policy !== "cloud" ? (
        <p className="live-asr-route-warning">
          {language === "ko" ? "LAN route는 평문 ws://입니다. 신뢰된 유선망에서만 사용하세요." : "The LAN route uses plaintext ws://. Use it only on a trusted wired network."}
        </p>
      ) : null}

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
          <button
            aria-describedby={startBlockerMessage ? "live-asr-start-help" : undefined}
            className="button button-primary"
            disabled={startDisabled}
            onClick={() => void onControl("start", selectedDeviceId)}
            title={startDisabled ? startBlockerMessage : undefined}
            type="button"
          >
            {pendingOperation === "start" ? <LoaderCircle className="live-asr-spinner" size={16} aria-hidden="true" /> : <Mic size={16} aria-hidden="true" />}
            {language === "ko" ? "ASR 시작" : "Start ASR"}
          </button>
          <button className="button button-secondary" disabled={stopDisabled} onClick={() => void onControl("stop", selectedDeviceId)} type="button">
            {pendingOperation === "stop" ? <LoaderCircle className="live-asr-spinner" size={16} aria-hidden="true" /> : <CircleStop size={16} aria-hidden="true" />}
            {language === "ko" ? "ASR 중지" : "Stop ASR"}
          </button>
        </div>
        <div className="live-asr-node-restart" data-slot="live-asr-node-restart">
          <button
            aria-busy={restartPending}
            aria-describedby="live-asr-restart-help"
            className="button button-quiet"
            disabled={restartDisabled}
            onClick={() => void onControl("restart_node")}
            type="button"
          >
            {restartPending
              ? <LoaderCircle className="live-asr-spinner" size={16} aria-hidden="true" />
              : <RefreshCw size={16} aria-hidden="true" />}
            {language === "ko" ? "ASR 노드 새로 시작" : "Restart ASR node"}
          </button>
          <p id="live-asr-restart-help">
            {language === "ko"
              ? "ASR Python 소스를 다시 읽습니다. 시나리오·VLM·카메라·UI는 재시작하지 않습니다."
              : "Reloads ASR Python source without restarting the scenario, VLM, cameras, or UI."}
          </p>
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
        <code>
          {status.output_topic || status.topic || (language === "ko" ? "출력 토픽 대기" : "Waiting for output topic")}
          {" · "}
          {status.output_mode === "typed_utterance"
            ? "surgical_msgs/msg/SpeechUtterance"
            : status.output_mode === "sentence_text" ? "std_msgs/msg/String" : (language === "ko" ? "출력 형식 대기" : "Waiting for output type")}
        </code>
        <code
          className="live-asr-runtime-revision"
          title={status.node_instance_id
            ? `${status.source_revision || "revision pending"} · ${status.node_instance_id}`
            : status.source_revision || undefined}
        >
          {language === "ko" ? "코드" : "Code"} {status.source_revision ? status.source_revision.slice(0, 8) : "--------"}
          {" · "}{formatNodeStartedAt(status.node_started_at_sec, language)}
        </code>
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

      {panelMessage ? (
        <p
          className={`live-asr-message ${panelMessageIsError ? "error" : "normal"}`}
          id="live-asr-start-help"
          role={panelMessageIsError ? "alert" : "status"}
        >
          {panelMessage}
        </p>
      ) : null}
      <p className="sr-only" aria-live="polite" aria-atomic="true">
        {statusLabel}. {latestFinal ? `${language === "ko" ? "최근 확정 문장" : "Latest final"}: ${latestFinal.text}. ${formatLatency(latestFinal.response_latency_ms, language)}.` : ""}
      </p>
    </section>
  );
}
