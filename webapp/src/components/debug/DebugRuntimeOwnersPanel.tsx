import {
  AlertTriangle,
  CheckCircle2,
  Cpu,
  LoaderCircle,
  Power,
  RefreshCw,
  ServerCog,
  Volume2,
} from "lucide-react";
import { useEffect, useState } from "react";

import {
  type RuntimeOwnerMode,
  type RuntimeOwnerSnapshotPhase,
  useRuntimeOwnerControl,
} from "../../hooks/useRuntimeOwnerControl";
import {
  type RuntimeLifecycleOperation,
  useRuntimeLifecycleControl,
} from "../../hooks/useRuntimeLifecycleControl";
import type { DebugReadOnlyRosSession } from "../../hooks/useIntegrationDebugBridge";
import { normalizeTtsPlaybackStatus, TTS_PLAYBACK_STATUS_TOPIC } from "../../ros/ttsPlaybackMessages";
import type { TtsPlaybackStatus } from "../../types";
import { TtsPlaybackStatusCard } from "../observability/TtsPlaybackStatusCard";
import "./DebugRuntimeOwnersPanel.css";

function modeLabel(mode: RuntimeOwnerMode | null): string {
  if (mode === "live") return "Live";
  if (mode === "llm-surgeon") return "LLM Surgeon";
  if (mode === "replay") return "Replay";
  return "Standalone Debug";
}

function phaseLabel(phase: RuntimeOwnerSnapshotPhase, ownerCount: number): string {
  if (phase === "ready") return `${ownerCount}개 owner 관측 중`;
  if (phase === "loading") return "상태 확인 중";
  if (phase === "unavailable") return "호스트 상태 연결 불가";
  return "독립 관찰 모드";
}

function NoticeIcon({ tone }: { tone: "success" | "error" | "info" }) {
  if (tone === "success") return <CheckCircle2 size={15} aria-hidden="true" />;
  if (tone === "error") return <AlertTriangle size={15} aria-hidden="true" />;
  return <LoaderCircle className="debug-spinner" size={15} aria-hidden="true" />;
}

function hasConfiguredService(owner: { service: string }): boolean {
  return owner.service.trim().length > 0;
}

export function DebugRuntimeOwnersPanel({
  mode,
  readOnlySession,
}: {
  mode: RuntimeOwnerMode | null;
  readOnlySession: DebugReadOnlyRosSession;
}) {
  const [expanded, setExpanded] = useState(true);
  const [ttsPlaybackStatus, setTtsPlaybackStatus] = useState<TtsPlaybackStatus | null>(null);
  useEffect(() => {
    setTtsPlaybackStatus(null);
    if (!readOnlySession.transportConnected) return;
    return readOnlySession.subscribeTopic({
      name: TTS_PLAYBACK_STATUS_TOPIC,
      messageType: "surgical_msgs/msg/TTSPlaybackStatus",
      queueLength: 1,
    }, (message) => {
      const status = normalizeTtsPlaybackStatus(message);
      if (!status) return;
      setTtsPlaybackStatus((previous) => previous && previous.stampSec > status.stampSec ? previous : status);
    });
  }, [readOnlySession]);
  const { notice, pendingOwner, refresh, restart, snapshot } = useRuntimeOwnerControl(mode);
  const lifecycle = useRuntimeLifecycleControl();
  const canObserveOwners = snapshot.phase !== "not_applicable";
  const ownerRestartPending = pendingOwner !== null;
  const refreshDisabled = snapshot.phase === "loading" || lifecycle.pending || ownerRestartPending;
  // The host registry deliberately returns its full inventory, including
  // owners unavailable in the selected mode.  Only a service-backed row is a
  // scoped restart target; do not surface mode-not-applicable placeholders as
  // misleading "service 미지정" controls.
  const configuredOwners = snapshot.owners.filter(hasConfiguredService);
  const ttsOwner = configuredOwners.find((owner) => owner.owner === "tts" && owner.mode === mode);
  const commandOwner = configuredOwners.find((owner) => owner.owner === "command" && owner.mode === mode);
  const genericOwners = configuredOwners.filter((owner) => owner.owner !== "tts" && owner.owner !== "command");
  const ttsPending = pendingOwner === "tts";
  const commandPending = pendingOwner === "command";
  const ttsAvailable = snapshot.phase === "ready" && mode === "live" && Boolean(ttsOwner?.service);
  const commandAvailable = snapshot.phase === "ready" && Boolean(commandOwner?.service);
  const modelState = lifecycle.status.ninfer.model_state;
  const modelTransitioning = modelState === "loading" || modelState === "unloading";
  const qwenIsLoaded = modelState === "loaded";
  const qwenOperation: RuntimeLifecycleOperation = qwenIsLoaded ? "qwen_unload" : "qwen_load";
  const lifecycleActionsDisabled = lifecycle.pending || ownerRestartPending;
  const qwenActionDisabled = lifecycleActionsDisabled || !lifecycle.status.ninfer.available || modelTransitioning;
  const lifecycleStatusText = lifecycle.status.ninfer.available
    ? `${lifecycle.status.ninfer.model_id || "Qwen 미지정"} · ${modelState}`
    : "NInfer manager 대기";

  const refreshAll = () => {
    if (canObserveOwners) void refresh();
    void lifecycle.refresh();
  };

  return (
    <details
      className="debug-section-card debug-runtime-owners-card"
      data-mode={mode ?? "debug"}
      data-phase={snapshot.phase}
      data-slot="debug-runtime-owner-panel"
      onToggle={(event) => setExpanded(event.currentTarget.open)}
      open={expanded}
    >
      <summary className="debug-runtime-owners-summary">
        <span className="debug-runtime-owners-icon" aria-hidden="true"><ServerCog size={18} /></span>
        <span className="debug-runtime-owners-summary-copy">
          <span>RUNTIME OWNERS</span>
          <strong>런타임·owner 제어</strong>
          <small>{modeLabel(mode)} · {lifecycleStatusText}</small>
        </span>
        <span className={`debug-runtime-owners-phase ${snapshot.phase}`}>
          {snapshot.phase === "loading" ? <LoaderCircle className="debug-spinner" size={15} aria-hidden="true" /> : null}
          {phaseLabel(snapshot.phase, snapshot.owners.length)}
        </span>
      </summary>

      <div className="debug-runtime-owners-content">
        <div className="debug-runtime-owners-toolbar">
          <p>
            NInfer와 재시작은 host runtime-control이 실행합니다. Debug는 상태를 읽고 고정된 요청만 전달합니다.
          </p>
          <button
            aria-label="runtime owner 상태 새로고침"
            className="button button-secondary debug-runtime-owners-refresh"
            disabled={refreshDisabled}
            onClick={refreshAll}
            type="button"
          >
            <RefreshCw className={snapshot.phase === "loading" ? "debug-spinner" : ""} size={16} aria-hidden="true" />
            새로고침
          </button>
        </div>

        <section aria-label="전체 런타임 재시작" className="debug-runtime-global-controls">
          <div className="debug-runtime-global-controls-heading">
            <span className="debug-runtime-global-controls-icon" aria-hidden="true"><ServerCog size={17} /></span>
            <div>
              <span>RUNTIME LIFECYCLE</span>
              <strong>전체 런타임 재시작</strong>
              <small>{lifecycle.status.message}</small>
            </div>
            <span className={`debug-runtime-lifecycle-state ${lifecycle.status.phase}`}>
              {lifecycle.pending ? <LoaderCircle className="debug-spinner" size={14} aria-hidden="true" /> : null}
              {lifecycle.pending ? "처리 중" : lifecycle.status.active_mode || "대기"}
            </span>
          </div>
          <div className="debug-runtime-global-controls-actions">
            <button
              className="button button-secondary"
              disabled={lifecycleActionsDisabled || !lifecycle.status.active_mode}
              onClick={() => void lifecycle.request("warm_restart")}
              type="button"
            >
              <RefreshCw size={15} aria-hidden="true" />Warm 재시작
            </button>
            <button
              className="button button-secondary debug-runtime-clean-restart"
              disabled={lifecycleActionsDisabled || !lifecycle.status.active_mode}
              onClick={() => void lifecycle.request("clean_restart")}
              type="button"
            >
              <Power size={15} aria-hidden="true" />Clean 재시작
            </button>
          </div>
          {lifecycle.notice ? (
            <p aria-live="polite" className={`debug-runtime-owners-notice ${lifecycle.notice.tone}`} role={lifecycle.notice.tone === "error" ? "alert" : "status"}>
              <NoticeIcon tone={lifecycle.notice.tone} />
              {lifecycle.notice.message}
            </p>
          ) : null}
        </section>

        <section aria-label="TTS owner 제어" className="debug-runtime-lifecycle debug-runtime-tts">
          <div className="debug-runtime-lifecycle-heading">
            <span className="debug-runtime-lifecycle-icon" aria-hidden="true"><Volume2 size={17} /></span>
            <div>
              <span>AUDIO · TTS</span>
              <strong>TTS 음성 출력</strong>
              <small>{ttsOwner?.detail || "Live TTS owner 상태 대기"}</small>
            </div>
            <span className={`debug-runtime-lifecycle-state ${ttsOwner?.state === "running" ? "succeeded" : "idle"}`}>
              {ttsPending ? <LoaderCircle className="debug-spinner" size={14} aria-hidden="true" /> : null}
              {ttsPending ? "재시작 중" : ttsOwner?.state || "대기"}
            </span>
          </div>
          <div className="debug-runtime-lifecycle-actions debug-runtime-tts-actions">
            <button
              aria-label="TTS owner만 재시작"
              className="button button-secondary"
              disabled={!ttsAvailable || lifecycle.pending || ownerRestartPending}
              onClick={() => void restart("tts")}
              type="button"
            >
              {ttsPending ? <LoaderCircle className="debug-spinner" size={15} aria-hidden="true" /> : <RefreshCw size={15} aria-hidden="true" />}
              {ttsPending ? "TTS 재시작 중" : "TTS 재시작"}
            </button>
          </div>
          <TtsPlaybackStatusCard language="ko" status={ttsPlaybackStatus} />
        </section>

        {commandOwner ? (
          <section
            aria-label="Command owner 제어"
            className="debug-runtime-lifecycle debug-runtime-command"
            data-slot="debug-command-owner-control"
          >
            <div className="debug-runtime-lifecycle-heading">
              <span className="debug-runtime-lifecycle-icon" aria-hidden="true"><ServerCog size={17} /></span>
              <div>
                <span>COMMAND · ROUTER</span>
                <strong>명령·음성 router</strong>
                <small>{commandOwner.service} · {commandOwner.detail || "Command owner 상태 대기"}</small>
              </div>
              <span className={`debug-runtime-lifecycle-state ${commandOwner.state === "running" ? "succeeded" : "idle"}`}>
                {commandPending ? <LoaderCircle className="debug-spinner" size={14} aria-hidden="true" /> : null}
                {commandPending ? "재시작 중" : commandOwner.state || "대기"}
              </span>
            </div>
            <div className="debug-runtime-lifecycle-actions debug-runtime-command-actions">
              <button
                aria-label="Command owner만 재시작"
                className="button button-secondary"
                disabled={!commandAvailable || lifecycle.pending || ownerRestartPending}
                onClick={() => void restart("command")}
                type="button"
              >
                {commandPending ? <LoaderCircle className="debug-spinner" size={15} aria-hidden="true" /> : <RefreshCw size={15} aria-hidden="true" />}
                {commandPending ? "Command 재시작 중" : "Command 재시작"}
              </button>
            </div>
          </section>
        ) : null}

        <section aria-label="NInfer 및 Qwen 모델 제어" className="debug-runtime-lifecycle">
          <div className="debug-runtime-lifecycle-heading">
            <span className="debug-runtime-lifecycle-icon" aria-hidden="true"><Cpu size={17} /></span>
            <div>
              <span>NINFER · QWEN</span>
              <strong>Qwen 모델 제어</strong>
              <small>{lifecycle.status.ninfer.detail}</small>
            </div>
            <span className={`debug-runtime-lifecycle-state ${lifecycle.status.phase}`}>
              {lifecycle.pending ? <LoaderCircle className="debug-spinner" size={14} aria-hidden="true" /> : null}
              {lifecycle.pending ? "처리 중" : lifecycle.status.ninfer.available ? modelState : "대기"}
            </span>
          </div>
          <dl className="debug-runtime-lifecycle-facts">
            <div><dt>QWEN</dt><dd title={lifecycle.status.ninfer.model_id || undefined}>{lifecycle.status.ninfer.model_id || "미발견"}</dd></div>
            <div><dt>MODEL</dt><dd>{modelState}</dd></div>
            <div><dt>NINFER</dt><dd>{lifecycle.status.ninfer.available ? "ready" : "waiting"}</dd></div>
          </dl>
          <div className="debug-runtime-lifecycle-actions">
            <button
              className="button button-secondary"
              disabled={lifecycleActionsDisabled || !lifecycle.status.ninfer.available}
              onClick={() => void lifecycle.request("ninfer_restart")}
              type="button"
            >
              <RefreshCw size={15} aria-hidden="true" />NInfer 재시작
            </button>
            <button
              className="button button-secondary"
              disabled={qwenActionDisabled}
              onClick={() => void lifecycle.request(qwenOperation)}
              type="button"
            >
              <Power size={15} aria-hidden="true" />{qwenIsLoaded ? "Qwen 언로드" : "Qwen 로드"}
            </button>
          </div>
        </section>

        {snapshot.phase === "not_applicable" ? (
          <div className="debug-runtime-owners-empty" role="status">
            <ServerCog size={20} aria-hidden="true" />
            <div>
              <strong>독립 Debug에는 별도 ROS owner 재시작 대상이 없습니다.</strong>
              <span>위 NInfer·Warm·Clean 제어는 host runtime-control을 통해 계속 사용할 수 있습니다.</span>
            </div>
          </div>
        ) : null}

        {snapshot.phase === "loading" ? (
          <div className="debug-runtime-owners-empty" role="status">
            <LoaderCircle className="debug-spinner" size={20} aria-hidden="true" />
            <div><strong>runtime owner 상태를 확인하고 있습니다.</strong><span>호스트 응답을 기다리는 동안 Debug 관찰은 계속 사용할 수 있습니다.</span></div>
          </div>
        ) : null}

        {snapshot.phase === "unavailable" ? (
          <div className="debug-runtime-owners-empty error" role="status">
            <AlertTriangle size={20} aria-hidden="true" />
            <div><strong>host runtime-control에서 owner 상태를 읽지 못했습니다.</strong><span>Debug ROS 관찰은 계속 사용할 수 있습니다. host runtime-control과 Vite proxy를 확인한 뒤 새로고침하세요.</span></div>
          </div>
        ) : null}

        {snapshot.phase === "ready" && genericOwners.length === 0 && !ttsOwner && !commandOwner ? (
          <div className="debug-runtime-owners-empty" role="status">
            <ServerCog size={20} aria-hidden="true" />
            <div><strong>등록된 runtime owner가 없습니다.</strong><span>host registry가 이 모드에서 반환한 owner가 없습니다.</span></div>
          </div>
        ) : null}

        {snapshot.phase === "ready" && genericOwners.length > 0 ? (
          <ul aria-label="runtime owner 상태" className="debug-runtime-owner-list">
            {genericOwners.map((owner) => {
              const pending = pendingOwner === owner.owner;
              const detailsId = `debug-runtime-owner-${owner.mode}-${owner.owner}`;
              return (
                <li data-owner={owner.owner} data-owner-state={owner.state} key={`${owner.mode}:${owner.owner}`}>
                  <div className="debug-runtime-owner-copy">
                    <div>
                      <strong>{owner.owner}</strong>
                      <span className="debug-runtime-owner-state">{owner.state || "상태 미지정"}</span>
                    </div>
                    <code>{owner.service}</code>
                    <p id={detailsId}>{owner.detail || "호스트가 추가 상세를 제공하지 않았습니다."}</p>
                  </div>
                  <button
                    aria-describedby={detailsId}
                    className="button button-secondary debug-runtime-owner-restart"
                    disabled={lifecycle.pending || pendingOwner !== null}
                    onClick={() => void restart(owner.owner)}
                    type="button"
                  >
                    {pending ? <LoaderCircle className="debug-spinner" size={16} aria-hidden="true" /> : <RefreshCw size={16} aria-hidden="true" />}
                    {pending ? "요청 중" : "재시작 요청"}
                  </button>
                </li>
              );
            })}
          </ul>
        ) : null}

        {notice ? (
          <p aria-live="polite" className={`debug-runtime-owners-notice ${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>
            <NoticeIcon tone={notice.tone} />
            {notice.message}
          </p>
        ) : null}
      </div>
    </details>
  );
}
