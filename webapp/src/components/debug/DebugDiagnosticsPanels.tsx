import { useEffect, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Cable,
  CheckCircle2,
  LoaderCircle,
  MicOff,
  Radio,
  RotateCcw,
  ShieldAlert,
  XCircle,
} from "lucide-react";

import {
  type DebugCommandResponse,
  type IntegrationDebugStatus,
} from "../../hooks/useIntegrationDebugBridge";
import { SafetyConfirmationDialog } from "../common/SafetyConfirmationDialog";

type DiagnosticTab = "endpoints" | "logs";

type RunDebugCommand = (
  operation: string,
  payload?: Record<string, unknown>,
) => Promise<DebugCommandResponse>;

function stateTone(state: string): "ok" | "warn" | "error" | "idle" {
  if (["READY", "LISTENING", "SUCCEEDED", "completed", "succeeded", "accepted", "ARMED"].includes(state)) return "ok";
  if (["TYPE_MISMATCH", "FAULT_LOCKED", "UNAVAILABLE", "ERROR", "FAILED", "failed", "rejected"].includes(state)) return "error";
  if (["LOW_RATE", "STALE", "BUSY", "STARTING", "STOPPING", "SUBMITTING", "REMOTE_STATE_UNKNOWN"].includes(state)) return "warn";
  return "idle";
}

function StatusBadge({ state, label }: { state: string; label?: string }) {
  const tone = stateTone(state);
  const Icon = tone === "ok" ? CheckCircle2 : tone === "error" ? XCircle : tone === "warn" ? AlertTriangle : Radio;
  return <span className={`debug-status-badge ${tone}`} data-slot="debug-status-badge"><Icon aria-hidden="true" size={14} />{label ?? state}</span>;
}

function formatLatency(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(1)} ms` : "측정 전";
}

function formatEventTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return date.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

function eventSummary(event: { event_type: string; payload: Record<string, unknown> }): string {
  const values = Object.entries(event.payload)
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`);
  return values.join(" · ") || "추가 정보 없음";
}

export function ForceRetractionIdleControl({
  blockedReason,
  disabled,
  internalState,
  internalStateLabel,
  pending,
  onReset,
}: {
  blockedReason: "" | "idle" | "recovery" | "busy" | "unavailable";
  disabled: boolean;
  internalState: string;
  internalStateLabel: string;
  pending: boolean;
  onReset: () => void;
}) {
  const [confirmationOpen, setConfirmationOpen] = useState(false);
  const hint = blockedReason === "idle"
    ? "현재 Debug 로컬 상태가 이미 IDLE입니다."
    : blockedReason === "recovery"
      ? "먼저 요청 접수 불확실성 복구 절차를 완료해야 합니다."
      : blockedReason === "busy"
        ? "진행 중인 Action 또는 Service 응답이 끝난 뒤 초기화할 수 있습니다."
        : blockedReason === "unavailable"
          ? "현재 연결 또는 제어 전환이 끝난 뒤 초기화할 수 있습니다."
      : "로봇 명령은 보내지 않고 Debug 로컬 상태만 IDLE로 바꾸며 수동 제어를 해제합니다.";

  useEffect(() => {
    if (internalState === "idle") setConfirmationOpen(false);
  }, [internalState]);

  return (
    <>
      <div className="debug-state-message warning" data-slot="debug-force-retraction-idle" role="note">
        <RotateCcw size={18} aria-hidden="true" />
        <div>
          <strong>Debug 로컬 상태 강제 초기화</strong>
          <span id="debug-force-retraction-idle-description">{hint}</span>
        </div>
        <button
          aria-describedby="debug-force-retraction-idle-description"
          aria-expanded={confirmationOpen}
          aria-haspopup="dialog"
          className="button button-secondary"
          disabled={disabled}
          onClick={() => setConfirmationOpen(true)}
          type="button"
        >
          {pending
            ? <LoaderCircle className="debug-spinner" size={16} aria-hidden="true" />
            : <RotateCcw size={16} aria-hidden="true" />}
          {pending ? "초기화 중" : "IDLE로 강제 초기화"}
        </button>
      </div>
      <SafetyConfirmationDialog
        closeLabel="취소"
        confirmLabel="IDLE로 강제 초기화"
        description={`현재 ${internalStateLabel} 상태를 지우고 Debug 로컬 상태를 IDLE로 되돌립니다.`}
        note="로봇이나 외부 Service에는 Stop 또는 다른 명령을 보내지 않습니다. 상대 로봇이 이미 정지했거나 가상 서버를 사용 중임을 확인한 경우에만 실행하세요. 수동 제어는 자동 해제됩니다."
        onClose={() => setConfirmationOpen(false)}
        onConfirm={onReset}
        open={confirmationOpen}
        title="Debug 상태를 IDLE로 초기화할까요?"
      />
    </>
  );
}

export function DebugIntegrationPipeline({ status, kind }: {
  status: IntegrationDebugStatus;
  kind: "tool_voice" | "retractor";
}) {
  const sentence = status.inputs.find((row) => row.topic === "/sensors/surgeon/sentence");
  const observed = status.inputs.find((row) => row.topic === "/surgery/audio/observed_utterance");
  const selectedSource = status.virtual_robot?.selected_source === "virtual" ? "virtual" : "external";
  const endpointReady = kind === "tool_voice"
    ? status.virtual_robot?.tool_handover_ready ?? status.endpoints.find((row) => row.name === "tool_handover")?.ready ?? false
    : status.virtual_robot?.retraction_service_ready ?? status.endpoints.find((row) => row.name === "retraction_service")?.ready ?? false;
  const sttReady = sentence?.state === "READY" || status.asr.state === "LISTENING" || Boolean(status.voice.last_sentence);
  // The observer owns no speech adapter and never parses or re-dispatches
  // voice.  The CommandRouter relay is an event stream: no message before a
  // final utterance is normal, while no publisher means the router is absent.
  const observedPublisherPresent = (observed?.publisher_count || 0) > 0;
  const observedOutputReceived = (observed?.message_count || 0) > 0;
  const observedStageClass = observedOutputReceived ? "success" : observedPublisherPresent ? "pending" : "idle";
  const observedStageState = observedOutputReceived ? "OBSERVED" : observedPublisherPresent ? "PENDING_FINAL" : "WAITING_ROUTER";
  const observedDetail = observedOutputReceived
    ? "router relay 관측됨"
    : observedPublisherPresent ? "final utterance 대기" : "CommandRouter publisher 대기";
  const scenario = kind === "tool_voice" ? "음성 도구전달" : "리트랙터 Service";
  const virtualUnavailable = selectedSource === "virtual" && status.virtual_robot?.enabled !== true;
  const endpointLabel = virtualUnavailable
    ? "가상 owner 미시작"
    : endpointReady ? "발견" : "미발견";
  return (
    <article className="debug-section-card debug-pipeline-card" data-slot="debug-integration-pipeline">
      <div className="debug-section-heading"><div><p>ACTUAL DEBUG PIPELINE</p><h2>{scenario} 통합 경로</h2><span>음성 명령은 CommandRouter가 단독 소유하고 Debug는 relay와 endpoint 상태만 관찰합니다.</span></div><StatusBadge state={virtualUnavailable ? "UNAVAILABLE" : endpointReady ? "READY" : "WAITING"} label={virtualUnavailable ? "가상 owner 미시작" : `${selectedSource === "virtual" ? "가상" : "외부"} 서버 선택`} /></div>
      <ol className="debug-flow-strip four" aria-label={`${scenario} 실제 처리 단계`}>
        <li className={sttReady ? "success" : sentence && sentence.state !== "READY" ? "error" : "idle"}><span>1</span><div><strong>USB·ASR 입력</strong><small>/sensors/surgeon/sentence · {sttReady ? "final 준비" : sentence?.state || "대기"}</small></div></li>
        <li className={observedStageClass} data-slot="debug-observed-utterance-stage" data-stage-state={observedStageState}><span>2</span><div><strong>Observed utterance</strong><small>/surgery/audio/observed_utterance · {observedDetail}</small></div></li>
        <li className={observedOutputReceived ? "success" : "idle"}><span>3</span><div><strong>CommandRouter catalog</strong><small>{observedOutputReceived ? "실행 경로 소유자 relay 확인" : "router final 관측 대기"}</small></div></li>
        <li className={virtualUnavailable ? "idle" : endpointReady ? "success" : "error"}><span>4</span><div><strong>{selectedSource === "virtual" ? "가상 진단 서버" : "외부 실제 서버"}</strong><small>{kind === "tool_voice" ? "Tool Handover Action" : "Retraction Service"} · {endpointLabel}</small></div></li>
      </ol>
      <div className={`debug-state-message ${selectedSource === "virtual" || virtualUnavailable ? "warning" : "success"}`} role="status">{selectedSource === "virtual" || virtualUnavailable ? <ShieldAlert aria-hidden="true" size={20} /> : <CheckCircle2 aria-hidden="true" size={20} />}<div><strong>{virtualUnavailable ? "가상 서버 owner가 시작되지 않았습니다" : selectedSource === "virtual" ? "가상 admission-only 요청 경로" : "외부 실제 서버 요청 경로"}</strong><span>{virtualUnavailable ? "가상 endpoint는 자동 생성되지 않습니다. Debug virtual owner를 별도로 시작한 뒤 다시 확인하세요." : selectedSource === "virtual" ? "물리 로봇 없이 가상 endpoint 접수만 확인하며 외부로 자동 fallback하지 않습니다." : "수동 제어·상태 게이트를 통과한 직접 UI 요청만 외부 서버에 전달될 수 있습니다."}</span></div></div>
    </article>
  );
}

function EndpointDiagnosticsPanel({
  status,
  connected,
  readiness,
  runCommand,
}: {
  status: IntegrationDebugStatus;
  connected: boolean;
  readiness: Record<string, unknown> | null;
  runCommand: RunDebugCommand;
}) {
  const [sourcePending, setSourcePending] = useState(false);
  const readyEndpoints = status.endpoints.filter((endpoint) => endpoint.ready).length;
  const virtual = status.virtual_robot;
  const selectedSource = virtual?.selected_source === "virtual" ? "virtual" : "external";
  const virtualUnavailable = Boolean(virtual) && virtual?.enabled !== true;
  const sourceSwitchLocked = status.session.armed || !status.action.terminal;

  async function configureEndpointSource(source: "external" | "virtual") {
    setSourcePending(true);
    try { await runCommand("configure_robot_endpoint_source", { source }); } finally { setSourcePending(false); }
  }

  return (
    <section className="debug-panel-stack" data-slot="debug-endpoint-panel">
      <div className="debug-diagnostic-grid">
        <article className="debug-section-card debug-endpoint-card">
          <div className="debug-section-heading">
            <div><p>CAPABILITY DISCOVERY</p><h2>Service·Action 종단</h2><span>종단 탐색과 타입만 확인하며 명령은 전송하지 않습니다.</span></div>
            <StatusBadge state={Boolean(readiness?.ready) ? "READY" : readyEndpoints ? "WAITING" : "UNAVAILABLE"} label={Boolean(readiness?.ready) ? "전체 Preflight 정상" : `${readyEndpoints}/${status.endpoints.length} 발견`} />
          </div>
          {status.endpoints.length ? (
            <div className="debug-endpoint-grid" role="list" aria-label="Service와 Action 종단 탐색 상태">
              {status.endpoints.map((endpoint) => <div className="debug-endpoint-row" key={endpoint.endpoint} role="listitem"><StatusBadge state={endpoint.ready ? "READY" : "WAITING"} label={endpoint.ready ? "발견" : "대기"} /><div><strong>{endpoint.name}</strong><code>{endpoint.endpoint}</code></div><small>{endpoint.kind}</small></div>)}
            </div>
          ) : <div className="debug-empty-state" data-slot="debug-endpoint-empty"><Cable aria-hidden="true" size={28} /><p>등록된 종단 상태가 아직 없습니다. ‘ROS 연결’에서 Domain과 discovery를 먼저 확인하세요.</p></div>}
        </article>

        <article className="debug-section-card debug-endpoint-source-card" aria-busy={sourcePending}>
          <div className="debug-section-heading"><div><p>ROBOT ENDPOINT SOURCE</p><h2>외부·가상 서버 선택</h2><span>자동 fallback이나 두 서버 혼합 없이 한 소스만 사용합니다.</span></div><StatusBadge state={virtualUnavailable ? "UNAVAILABLE" : selectedSource === "virtual" ? "WAITING" : "READY"} label={virtualUnavailable ? "가상 owner 미시작" : selectedSource === "virtual" ? "가상 진단 서버" : "외부 실제 서버"} /></div>
          <div className="debug-segmented-control" aria-label="로봇 endpoint 소스" data-slot="debug-robot-endpoint-source" role="group">
            <button aria-pressed={selectedSource === "external"} className={selectedSource === "external" ? "active" : ""} disabled={!connected || !virtual || sourcePending || sourceSwitchLocked} onClick={() => void configureEndpointSource("external")} type="button">외부 실제 서버<small>external</small></button>
            <button aria-pressed={selectedSource === "virtual"} className={selectedSource === "virtual" ? "active" : ""} disabled={!connected || !virtual?.enabled || sourcePending || sourceSwitchLocked} onClick={() => void configureEndpointSource("virtual")} type="button">가상 진단 서버<small>virtual · admission-only</small></button>
          </div>
          {sourceSwitchLocked ? <div className="debug-state-message warning" data-slot="debug-endpoint-source-locked" role="status"><ShieldAlert aria-hidden="true" size={20} /><div><strong>Endpoint 소스 전환 잠김</strong><span>{status.session.armed ? "수동 제어를 해제" : "진행 중 명령을 종료"}한 뒤 외부·가상 서버를 전환하세요.</span></div></div>
            : !virtual ? <div className="debug-state-message empty" data-slot="debug-virtual-robot-unavailable" role="status"><Cable aria-hidden="true" size={20} /><div><strong>Endpoint 소스 선택 상태 대기</strong><span>selector 상태를 아직 발행하지 않아 외부 소스로만 표시합니다.</span></div></div>
              : virtualUnavailable ? <div className="debug-state-message warning" data-slot="debug-virtual-owner-not-started" role="status"><ShieldAlert aria-hidden="true" size={20} /><div><strong>가상 Debug owner가 시작되지 않았습니다</strong><span>가상 endpoint는 자동으로 대체·생성하지 않습니다. debug-virtual owner를 시작하면 가상 선택이 열립니다.</span></div></div>
              : selectedSource === "virtual" ? <div className="debug-state-message warning" role="status"><ShieldAlert aria-hidden="true" size={20} /><div><strong>가상 진단 서버가 명시적으로 선택됨</strong><span>전용 가상 종단의 접수만 시험하며 외부 서버로 자동 전환하지 않습니다.</span></div></div>
                : <div className="debug-state-message success" role="status"><CheckCircle2 aria-hidden="true" size={20} /><div><strong>외부 실제 서버가 명시적으로 선택됨</strong><span>통합 시나리오의 요청이 외부 종단으로 전달될 수 있습니다.</span></div></div>}
          {virtual ? <dl className="debug-runtime-facts"><div><dt>Tool Action</dt><dd>{virtual.tool_handover_ready ? "발견" : "대기"}</dd></div><div><dt>현재 Retraction</dt><dd>{virtual.retraction_service_ready ? "발견" : "대기"}</dd></div><div><dt>외부 Retraction</dt><dd>{virtual.external_retraction_service_ready ? "발견" : "대기"}</dd></div><div><dt>가상 Retraction</dt><dd>{virtual.virtual_retraction_service_ready ? "발견" : "대기"}</dd></div><div><dt>Arm 상태</dt><dd>{virtual.bed_status_ready ? "수신" : "대기"}</dd></div><div><dt>가상 프로파일</dt><dd>{virtual.profile_id || "미지정"}</dd></div></dl> : null}
        </article>
      </div>
      <article className="debug-section-card debug-action-card" aria-live="polite">
        <div className="debug-section-heading"><div><p>LAST COMMAND LIFECYCLE</p><h2>최근 명령 진단</h2><span>Action 결과 또는 Service admission만 표시합니다.</span></div><Activity aria-hidden="true" size={19} /></div>
        {status.action.route || status.action.command_id ? <><div className="debug-action-summary"><StatusBadge state={status.action.state} /><strong>{status.action.command || status.action.route}</strong><code>{status.action.command_id}</code></div><div className="debug-action-meta"><span>종단: {status.action.route || "—"}</span><span>사유: {status.action.reason_code || "없음"}</span><span>{status.action.response_semantics === "admission" ? "접수 의미" : "Action 의미"}</span></div></> : <div className="debug-empty-state"><Activity aria-hidden="true" size={28} /><p>아직 진단할 명령 이력이 없습니다.</p></div>}
      </article>
    </section>
  );
}

function LogsPanel({ status }: { status: IntegrationDebugStatus }) {
  const asrFinals = status.asr.finals ?? [];
  const finals = [...asrFinals].reverse().slice(0, 30);
  const renderEvents = (events: typeof status.recent_events) => events.map((event, index) => <li key={`${event.stamp}-${event.event_type}-${index}`}><time dateTime={event.stamp}>{formatEventTime(event.stamp)}</time><strong>{event.event_type}</strong><span className="debug-event-summary">{eventSummary(event)}</span><details className="debug-event-raw"><summary>원문</summary><code>{JSON.stringify(event.payload, null, 2)}</code></details></li>);
  return (
    <section className="debug-panel-stack" data-slot="debug-logs-panel">
      <div className="debug-log-grid">
        <article className="debug-section-card debug-event-card"><div className="debug-section-heading"><div><p>STT FINAL LOG</p><h2>확정 문장 관측</h2><span>캡처 제어 없이 ASR final만 표시합니다.</span></div><span className="debug-meta-pill">{asrFinals.length}건</span></div><div className="debug-asr-transcript">{finals.length ? <ol>{finals.map((row, index) => <li key={`${row.stamp}-${index}`}><time dateTime={row.stamp}>{formatEventTime(row.stamp)}</time><span>{row.text}</span><data value={row.response_latency_ms ?? undefined}>{formatLatency(row.response_latency_ms)}</data></li>)}</ol> : <div className="debug-empty-state"><MicOff aria-hidden="true" size={28} /><p>아직 확정된 STT 문장이 없습니다.</p></div>}</div></article>
      </div>
      <article className="debug-section-card debug-event-card"><div className="debug-section-heading"><div><p>SESSION EVENT LOG</p><h2>전체 검증 이벤트</h2><span title={status.session.event_log_path}>{status.session.event_log_path}</span></div><span className="debug-meta-pill">최근 {Math.min(status.recent_events.length, 50)}건</span></div>{status.recent_events.length ? <ol className="debug-event-list">{renderEvents([...status.recent_events].reverse().slice(0, 50))}</ol> : <div className="debug-empty-state"><Activity aria-hidden="true" size={28} /><p>아직 기록된 검증 이벤트가 없습니다.</p></div>}</article>
    </section>
  );
}

export default function DebugDiagnosticsPanels({ tab, status, connected, readiness, runCommand }: {
  tab: DiagnosticTab;
  status: IntegrationDebugStatus;
  connected: boolean;
  readiness: Record<string, unknown> | null;
  runCommand: RunDebugCommand;
}) {
  if (tab === "endpoints") return <EndpointDiagnosticsPanel connected={connected} readiness={readiness} runCommand={runCommand} status={status} />;
  return <LogsPanel status={status} />;
}
