import {
  ArrowRight,
  AlertTriangle,
  CheckCircle2,
  LoaderCircle,
  Play,
  RefreshCw,
  ServerCog,
  Square,
} from "lucide-react";

import { useSurgiMateControl } from "../../hooks/useSurgiMateControl";
import { surgimateUrl } from "../../runtimeModes";
import "./SurgiMateSidecarPanel.css";

function StateIcon({ tone }: { tone: "success" | "error" | "info" }) {
  if (tone === "success") return <CheckCircle2 size={15} aria-hidden="true" />;
  if (tone === "error") return <AlertTriangle size={15} aria-hidden="true" />;
  return <LoaderCircle className="debug-spinner" size={15} aria-hidden="true" />;
}

function statusLabel(state: string | undefined): string {
  if (state === "running") return "실행 중";
  if (state === "created" || state === "exited") return "중지됨";
  if (state === "not-deployed") return "미배포";
  if (state === "unavailable") return "사용 불가";
  if (state === "conflict") return "상태 충돌";
  return state || "확인 중";
}

/** A narrow Debug-workspace control for the independent, read-only SurgiMate server. */
export function SurgiMateSidecarPanel() {
  const { notice, pendingAction, refresh, request, status } = useSurgiMateControl();
  const ownerState = status.owner?.state;
  const standaloneDebugActive = status.phase === "ready" && status.activeMode === "debug";
  const runtimeRestartAvailable = standaloneDebugActive || (
    status.phase === "ready" && status.activeMode === "live"
  );
  const running = ownerState === "running";
  const busy = pendingAction !== null;
  const canOpen = status.phase === "ready" && running;

  const openSurgiMate = () => {
    window.location.assign(surgimateUrl());
  };

  return (
    <section
      aria-labelledby="surgimate-sidecar-heading"
      className="debug-section-card surgimate-sidecar-card"
      data-slot="surgimate-sidecar-panel"
      data-state={ownerState || status.phase}
    >
      <header className="surgimate-sidecar-heading">
        <span className="surgimate-sidecar-icon" aria-hidden="true"><ServerCog size={18} /></span>
        <div>
          <span>SURGIMATE · INDEPENDENT APP</span>
          <h2 id="surgimate-sidecar-heading">독립 SurgiMate 앱</h2>
          <small>ARPA-H/modules/surgimate_0820 · static sidecar</small>
        </div>
        <span className={`surgimate-sidecar-state ${status.phase}`}>
          {status.phase === "loading" || busy ? <LoaderCircle className="debug-spinner" size={14} aria-hidden="true" /> : null}
          {status.phase === "ready" ? statusLabel(ownerState) : status.phase === "loading" ? "상태 확인 중" : "연결 불가"}
        </span>
      </header>

      <div className="surgimate-sidecar-content">
        <dl className="surgimate-sidecar-facts">
          <div><dt>SERVER</dt><dd>{status.owner?.service || "상태 대기"}</dd></div>
          <div><dt>RUNTIME</dt><dd>{runtimeRestartAvailable ? "제어 가능" : status.activeMode || "비활성"}</dd></div>
          <div><dt>URL</dt><dd title={surgimateUrl()}>{surgimateUrl()}</dd></div>
        </dl>
        <p className="surgimate-sidecar-note">
          {status.message} 정적 서버 실행은 9092 공개 Bridge 또는 토픽 수신을 보장하지 않습니다. 이 로컬 URL은 이 호스트에서만 엽니다.
        </p>
        <div className="surgimate-sidecar-actions">
          <button
            className="button button-secondary"
            disabled={!canOpen}
            onClick={openSurgiMate}
            type="button"
          >
            <ArrowRight size={15} aria-hidden="true" />SurgiMate로 이동
          </button>
          {standaloneDebugActive ? (
            <>
              <button
                className="button button-secondary"
                disabled={busy || running}
                onClick={() => void request("start")}
                type="button"
              >
                {pendingAction === "start" ? <LoaderCircle className="debug-spinner" size={15} aria-hidden="true" /> : <Play size={15} aria-hidden="true" />}
                시작
              </button>
              <button
                className="button button-secondary"
                disabled={busy || !running}
                onClick={() => void request("stop")}
                type="button"
              >
                {pendingAction === "stop" ? <LoaderCircle className="debug-spinner" size={15} aria-hidden="true" /> : <Square size={14} aria-hidden="true" />}
                중지
              </button>
            </>
          ) : null}
          <button
            className="button button-secondary"
            disabled={!runtimeRestartAvailable || busy}
            onClick={() => void request("restart")}
            type="button"
          >
            {pendingAction === "restart" ? <LoaderCircle className="debug-spinner" size={15} aria-hidden="true" /> : <RefreshCw size={15} aria-hidden="true" />}
            재시작
          </button>
          <button
            aria-label="SurgiMate sidecar 상태 새로고침"
            className="button button-quiet surgimate-sidecar-refresh"
            disabled={busy || status.phase === "loading"}
            onClick={() => void refresh()}
            type="button"
          >
            <RefreshCw size={15} aria-hidden="true" />
          </button>
        </div>
        {notice ? (
          <p aria-live="polite" className={`surgimate-sidecar-notice ${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>
            <StateIcon tone={notice.tone} />
            {notice.message}
          </p>
        ) : null}
      </div>
    </section>
  );
}

export default SurgiMateSidecarPanel;
