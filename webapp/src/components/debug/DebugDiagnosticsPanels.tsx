import { useEffect, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Bug,
  Cable,
  CheckCircle2,
  LoaderCircle,
  MicOff,
  Play,
  Radio,
  RefreshCw,
  RotateCcw,
  Shield,
  ShieldAlert,
  XCircle,
} from "lucide-react";

import {
  type DebugCommandResponse,
  type IntegrationDebugStatus,
} from "../../hooks/useIntegrationDebugBridge";
import { SafetyConfirmationDialog } from "../common/SafetyConfirmationDialog";

type DiagnosticTab = "vlm" | "endpoints" | "logs";

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

function formatAge(value: number | null | undefined): string {
  if (typeof value !== "number") return "수신 전";
  return value < 1 ? `${Math.round(value * 1000)} ms 전` : `${value.toFixed(1)} s 전`;
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

function interpretationLabel(interpretation: IntegrationDebugStatus["voice"]["retraction"] extends infer R
  ? R extends { last_interpretation: infer I } ? I : never
  : never): string {
  if (!interpretation?.command) return interpretation?.reason || "수신 전";
  if (interpretation.command !== "adjust_retraction") return interpretation.command;
  const side = interpretation.target_side === "left" ? "왼쪽" : interpretation.target_side === "right" ? "오른쪽" : "대상 없음";
  return `Retraction 더 · ${side} ${(interpretation.distance_m * 100).toFixed(0)} cm`;
}

function sourceLabel(source: string | undefined, invoked: boolean | undefined): string {
  if (!source) return "해석기 정보 없음";
  if (source === "text_vlm") return "Text VLM · 원문 근거 재검증 완료";
  if (source === "deterministic_fallback") return invoked ? "Text VLM 호출 후 결정론 폴백" : "결정론 폴백 · VLM 미호출";
  return invoked ? `${source} · VLM 호출됨` : source;
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
          : "로봇 명령은 보내지 않고 Debug 로컬 상태만 IDLE로 바꾸며, 수동 제어와 리트랙터 음성 전송은 자동 해제합니다.";

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
        note="로봇이나 외부 Service에는 Stop 또는 다른 명령을 보내지 않습니다. 상대 로봇이 이미 정지했거나 가상 서버를 사용 중임을 확인한 경우에만 실행하세요. 수동 제어와 리트랙터 음성 전송은 자동 해제됩니다."
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
  const adapted = status.inputs.find((row) => row.topic === "/surgery/audio/request_text");
  const retraction = status.voice.retraction;
  const parse = status.voice.last_parse;
  const selectedSource = status.virtual_robot?.selected_source === "virtual" ? "virtual" : "external";
  const endpointReady = kind === "tool_voice"
    ? status.virtual_robot?.tool_handover_ready ?? status.endpoints.find((row) => row.name === "tool_handover")?.ready ?? false
    : status.virtual_robot?.retraction_service_ready ?? status.endpoints.find((row) => row.name === "retraction_service")?.ready ?? false;
  const pending = kind === "retractor" && retraction?.interpreter_pending === true;
  const normalized = kind === "tool_voice" ? parse.matched === true && parse.operation === "tool_handover" : Boolean(retraction?.last_interpretation?.command);
  const rejected = kind === "tool_voice" ? parse.ambiguous === true : Boolean(retraction?.last_rejection_reason) && !pending;
  const sttReady = sentence?.state === "READY" || status.asr.state === "LISTENING" || Boolean(status.voice.last_sentence);
  const adapterReady = adapted?.state === "READY";
  const scenario = kind === "tool_voice" ? "음성 도구전달" : "리트랙터 음성";
  return (
    <article className="debug-section-card debug-pipeline-card" data-slot="debug-integration-pipeline">
      <div className="debug-section-heading"><div><p>ACTUAL DEBUG PIPELINE</p><h2>{scenario} 통합 경로</h2><span>micro-test와 달리 조건 충족 시 선택된 서버에 실제 Debug 요청을 보냅니다.</span></div><StatusBadge state={endpointReady && adapterReady ? "READY" : "WAITING"} label={`${selectedSource === "virtual" ? "가상" : "외부"} 서버 선택`} /></div>
      <ol className="debug-flow-strip four" aria-label={`${scenario} 실제 처리 단계`}>
        <li className={sttReady ? "success" : sentence && sentence.state !== "READY" ? "error" : "idle"}><span>1</span><div><strong>USB·수동 STT</strong><small>/sensors/surgeon/sentence · {sttReady ? "final 준비" : sentence?.state || "대기"}</small></div></li>
        <li className={adapterReady ? "success" : adapted && adapted.state !== "READY" ? "error" : "idle"}><span>2</span><div><strong>Speech adapter</strong><small>/surgery/audio/request_text · {adapted?.state || "상태 대기"}</small></div></li>
        <li className={pending ? "pending" : normalized ? "success" : rejected ? "error" : "idle"}><span>3</span><div><strong>{kind === "tool_voice" ? "결정론 도구 해석" : "Text VLM·결정론"}</strong><small>{pending ? "해석 중" : normalized ? "정규화 완료" : rejected ? "거부됨" : "문장 대기"}</small></div></li>
        <li className={endpointReady ? "success" : "error"}><span>4</span><div><strong>{selectedSource === "virtual" ? "가상 진단 서버" : "외부 실제 서버"}</strong><small>{kind === "tool_voice" ? "Tool Handover Action" : "Retraction Service"} · {endpointReady ? "발견" : "미발견"}</small></div></li>
      </ol>
      <div className={`debug-state-message ${selectedSource === "virtual" ? "warning" : "success"}`} role="status">{selectedSource === "virtual" ? <ShieldAlert aria-hidden="true" size={20} /> : <CheckCircle2 aria-hidden="true" size={20} />}<div><strong>{selectedSource === "virtual" ? "가상 admission-only 요청 경로" : "외부 실제 서버 요청 경로"}</strong><span>{selectedSource === "virtual" ? "물리 로봇 없이 가상 endpoint 접수만 확인하며 외부로 자동 fallback하지 않습니다." : "수동 제어·상태 게이트를 통과한 요청이 외부 서버에 전달될 수 있습니다."}</span></div></div>
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
          <div className="debug-section-heading"><div><p>ROBOT ENDPOINT SOURCE</p><h2>외부·가상 서버 선택</h2><span>자동 fallback이나 두 서버 혼합 없이 한 소스만 사용합니다.</span></div><StatusBadge state={selectedSource === "virtual" ? "WAITING" : "READY"} label={selectedSource === "virtual" ? "가상 진단 서버" : "외부 실제 서버"} /></div>
          <div className="debug-segmented-control" aria-label="로봇 endpoint 소스" data-slot="debug-robot-endpoint-source" role="group">
            <button aria-pressed={selectedSource === "external"} className={selectedSource === "external" ? "active" : ""} disabled={!connected || !virtual || sourcePending || sourceSwitchLocked} onClick={() => void configureEndpointSource("external")} type="button">외부 실제 서버<small>external</small></button>
            <button aria-pressed={selectedSource === "virtual"} className={selectedSource === "virtual" ? "active" : ""} disabled={!connected || !virtual?.enabled || sourcePending || sourceSwitchLocked} onClick={() => void configureEndpointSource("virtual")} type="button">가상 진단 서버<small>virtual · admission-only</small></button>
          </div>
          {sourceSwitchLocked ? <div className="debug-state-message warning" data-slot="debug-endpoint-source-locked" role="status"><ShieldAlert aria-hidden="true" size={20} /><div><strong>Endpoint 소스 전환 잠김</strong><span>{status.session.armed ? "수동 제어를 해제" : "진행 중 명령을 종료"}한 뒤 외부·가상 서버를 전환하세요.</span></div></div>
            : !virtual ? <div className="debug-state-message empty" data-slot="debug-virtual-robot-unavailable" role="status"><Cable aria-hidden="true" size={20} /><div><strong>Endpoint 소스 선택 상태 대기</strong><span>selector 상태를 아직 발행하지 않아 외부 소스로만 표시합니다.</span></div></div>
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

function TextVlmPanel({ status, connected, runCommand, openStt }: {
  status: IntegrationDebugStatus;
  connected: boolean;
  runCommand: RunDebugCommand;
  openStt: () => void;
}) {
  const retraction = status.voice.retraction;
  const latest = retraction?.last_interpretation;
  const vlm = status.vlm;
  const [text, setText] = useState(vlm?.micro_test?.transcript || latest?.transcript || "");
  const [state, setState] = useState(retraction?.internal_state || "idle");
  const [pending, setPending] = useState<"" | "vlm_refresh" | "vlm_load" | "vlm_interpret">("");
  const [microResult, setMicroResult] = useState<Record<string, unknown> | null>(null);
  const [microCommandError, setMicroCommandError] = useState("");

  useEffect(() => {
    if (text.trim()) return;
    const next = vlm?.micro_test?.transcript || latest?.transcript || "";
    if (next) setText(next);
  }, [latest?.transcript, text, vlm?.micro_test?.transcript]);

  async function execute(operation: "vlm_refresh" | "vlm_load") {
    setPending(operation);
    try { await runCommand(operation); } finally { setPending(""); }
  }

  async function interpretOnly() {
    const transcript = text.trim();
    if (!transcript) return;
    setPending("vlm_interpret");
    setMicroResult(null);
    setMicroCommandError("");
    try {
      const response = await runCommand("vlm_interpret", { text: transcript, state });
      if (response.accepted) setMicroResult(response.result);
      else setMicroCommandError(response.message || "Text VLM 해석 요청이 거부되었습니다.");
    } finally { setPending(""); }
  }

  const backendMicroState = String(vlm?.micro_test?.state || "IDLE");
  const microPending = pending === "vlm_interpret" || ["PENDING", "RUNNING", "SUBMITTING", "LOADING"].includes(backendMicroState.toUpperCase());
  const interpretation = microResult ?? vlm?.micro_test?.interpretation ?? null;
  const microError = microCommandError || vlm?.micro_test?.error || "";
  const runtimeReady = vlm?.available === true && (vlm.loaded ?? true);
  const runtimeLoading = pending === "vlm_load" || vlm?.probe_pending === true || vlm?.load_state?.toLowerCase() === "loading";
  const runtimeLoadError = vlm?.load_state?.toLowerCase() === "error" || vlm?.detail?.startsWith("load_failed:");
  const loadable = vlm?.runtime_managed === true && vlm.available === true;
  const outputAvailable = interpretation !== null && interpretation !== undefined && !(typeof interpretation === "object" && !Array.isArray(interpretation) && !Object.keys(interpretation).length);
  const resultState = String(microResult?.state ?? backendMicroState);
  const resultLatency = typeof microResult?.latency_ms === "number" ? microResult.latency_ms : vlm?.micro_test?.latency_ms;
  const resultTranscript = String(microResult?.transcript ?? vlm?.micro_test?.transcript ?? text);
  const resultDispatch = microResult?.dispatch_performed === true ? "발생" : "없음";
  const resultSource = String(microResult?.interpreter_source ?? (typeof interpretation === "object" && interpretation ? interpretation.interpreter_source ?? "" : ""));
  const resultVlmInvoked = microResult?.vlm_invoked ?? (typeof interpretation === "object" && interpretation ? interpretation.vlm_invoked : undefined);
  const fallbackResult = outputAvailable && (resultVlmInvoked === false || resultSource.includes("fallback"));

  return (
    <section className="debug-panel-stack" data-slot="debug-vlm-panel">
      <div className="debug-diagnostic-grid">
        <article className="debug-section-card debug-vlm-runtime-card">
          <div className="debug-section-heading"><div><p>TEXT-ONLY VLM RUNTIME</p><h2>모델·endpoint 상태</h2><span>시각 schema-v4 경로와 분리된 음성 명령 정규화 진단입니다.</span></div><StatusBadge state={!vlm ? "WAITING" : runtimeLoading ? "SUBMITTING" : runtimeLoadError ? "ERROR" : runtimeReady ? "READY" : vlm.load_state || "UNAVAILABLE"} label={!vlm ? "상태 대기" : runtimeLoading ? "모델 준비 중" : runtimeLoadError ? "로드 실패" : runtimeReady ? "해석 가능" : vlm.load_state || "준비 안 됨"} /></div>
          {!vlm ? <div className="debug-state-message empty" data-slot="debug-vlm-status-empty" role="status"><Bug aria-hidden="true" size={20} /><div><strong>VLM 진단 상태를 기다리고 있습니다</strong><span>status.vlm이 발행되면 runtime 상태가 표시됩니다.</span></div></div> : <dl className="debug-runtime-facts"><div><dt>MODEL</dt><dd title={vlm.model_id}>{vlm.model_id || "미지정"}</dd></div><div><dt>LOAD</dt><dd>{vlm.loaded ? "loaded" : vlm.load_state || "not loaded"}</dd></div><div><dt>MANAGER</dt><dd>{vlm.manager_reachable ? "연결" : "대기"}</dd></div><div><dt>CATALOG</dt><dd>{vlm.catalog_reachable ? "연결" : "대기"}</dd></div><div><dt>ENDPOINT</dt><dd title={vlm.base_url}>{vlm.base_url || "미지정"}</dd></div><div><dt>LAST PROBE</dt><dd>{formatAge(vlm.last_probe_age_sec)}</dd></div></dl>}
          {vlm?.detail ? <code className="debug-topic-code">{vlm.detail}</code> : null}
          {runtimeLoadError ? <div className="debug-state-message error" role="alert"><XCircle aria-hidden="true" size={20} /><div><strong>구성 모델 로드에 실패했습니다</strong><span>{vlm?.detail || "상태를 새로고침한 뒤 다시 시도하세요."}</span></div></div> : null}
          <div className="debug-vlm-runtime-actions">
            <button className="button button-secondary" disabled={!connected || Boolean(pending)} onClick={() => void execute("vlm_refresh")} type="button"><RefreshCw aria-hidden="true" size={16} />{pending === "vlm_refresh" ? "Worker micro-test 중" : "상태 + 실제 micro-test"}</button>
            <button className="button button-primary" disabled={!connected || !vlm || !loadable || vlm.loaded === true || runtimeLoading || Boolean(pending)} onClick={() => void execute("vlm_load")} type="button">{runtimeLoading ? <LoaderCircle aria-hidden="true" className="debug-spinner" size={16} /> : vlm?.loaded ? <CheckCircle2 aria-hidden="true" size={16} /> : <Play aria-hidden="true" size={16} />}{runtimeLoading ? "구성 모델 로드 중" : vlm?.loaded ? "구성 모델 로드됨" : "구성 모델 로드"}</button>
          </div>
          {vlm && !vlm.loaded && !loadable && !runtimeLoadError ? <p className="debug-inline-warning">launch에 고정된 모델이 manager의 load 대상이 아닙니다. 모델·URL을 화면에서 임의로 변경할 수 없습니다.</p> : null}
        </article>

        <article className="debug-section-card debug-control-card" aria-busy={microPending}>
          <div className="debug-section-heading"><div><p>ISOLATED MICRO-TEST</p><h2>Text VLM 입력</h2><span>해석 결과만 만들고 Service·Action은 호출하지 않습니다.</span></div><Shield aria-hidden="true" size={19} /></div>
          <label className="debug-field" htmlFor="debug-vlm-text"><span>확정 STT 문장</span><textarea id="debug-vlm-text" onChange={(event) => setText(event.target.value)} placeholder="예: 리트랙션 오른쪽 5센티 더" rows={3} value={text} /></label>
          <label className="debug-field" htmlFor="debug-vlm-state"><span>가정할 내부 상태</span><select id="debug-vlm-state" onChange={(event) => setState(event.target.value)} value={state}><option value="idle">idle</option><option value="direct_teaching">direct_teaching</option><option value="taught_ready">taught_ready</option><option value="retraction_active">retraction_active</option><option value="unknown">unknown</option></select><small>통합 상태와 분리된 micro-test 입력입니다.</small></label>
          <button className="button button-primary full" disabled={!connected || !runtimeReady || !text.trim() || Boolean(pending) || microPending} onClick={() => void interpretOnly()} type="button"><Bug aria-hidden="true" size={16} />{microPending ? "Text VLM 해석 중" : "해석만 실행 · Service 전송 안 함"}</button>
          {!runtimeReady ? <p className="debug-inline-warning">모델 상태가 ‘해석 가능’이 된 뒤 실행할 수 있습니다.</p> : null}
        </article>
      </div>

      <article className="debug-section-card debug-vlm-output-card" aria-busy={microPending} aria-live="polite">
        <div className="debug-section-heading"><div><p>MICRO-TEST OUTPUT</p><h2>Text VLM 출력</h2><span>ROS 명령 승인이나 로봇 동작 상태가 아닙니다.</span></div><StatusBadge state={microPending ? "SUBMITTING" : microError ? "ERROR" : outputAvailable ? "READY" : "WAITING"} label={microPending ? "해석 중" : microError ? "실패" : outputAvailable ? "결과 수신" : "입력 대기"} /></div>
        {microPending ? <div className="debug-vlm-skeleton" data-slot="debug-vlm-output-pending"><span /><span /><span /><p className="sr-only">Text VLM 결과를 기다리고 있습니다.</p></div>
          : microError ? <div className="debug-state-message error" data-slot="debug-vlm-output-error" role="alert"><XCircle aria-hidden="true" size={20} /><div><strong>Text VLM 해석에 실패했습니다</strong><span>{microError}</span></div><button className="button button-secondary" disabled={!connected || !runtimeReady || !text.trim()} onClick={() => void interpretOnly()} type="button">다시 해석</button></div>
            : outputAvailable ? <div className="debug-vlm-output" data-slot="debug-vlm-output-success"><dl className="debug-runtime-facts"><div><dt>STATE</dt><dd>{resultState}</dd></div><div><dt>LATENCY</dt><dd>{formatLatency(resultLatency)}</dd></div><div><dt>INPUT</dt><dd title={resultTranscript}>{resultTranscript}</dd></div><div><dt>DISPATCH</dt><dd>{resultDispatch}</dd></div><div><dt>SOURCE</dt><dd>{resultSource || "미제공"}</dd></div><div><dt>VLM INVOKED</dt><dd>{resultVlmInvoked === true ? "yes" : resultVlmInvoked === false ? "no" : "미제공"}</dd></div></dl>{fallbackResult ? <div className="debug-state-message warning" data-slot="debug-vlm-fallback-result" role="status"><ShieldAlert aria-hidden="true" size={20} /><div><strong>모델 출력이 아닌 fallback 결과</strong><span>{resultSource || "결정론 정규화기"} · VLM invoked {resultVlmInvoked === true ? "yes" : "no"}. 모델 성공으로 해석하지 마세요.</span></div></div> : null}<code>{typeof interpretation === "string" ? interpretation : JSON.stringify(interpretation, null, 2)}</code></div>
              : <div className="debug-empty-state" data-slot="debug-vlm-output-empty"><Bug aria-hidden="true" size={28} /><p>아직 micro-test 결과가 없습니다.</p><button className="button button-secondary" onClick={openStt} type="button">STT 입력 열기</button></div>}
      </article>

      <article className="debug-section-card">
        <div className="debug-section-heading"><div><p>INTEGRATED PATH SNAPSHOT</p><h2>최근 통합 해석</h2><span>통합 시나리오의 read-only snapshot입니다.</span></div><StatusBadge state={retraction?.interpreter_pending ? "SUBMITTING" : latest?.command ? "READY" : "WAITING"} label={retraction?.interpreter_pending ? "해석 중" : latest?.command ? "해석됨" : "수신 전"} /></div>
        {latest?.transcript ? <div className="debug-parse-preview"><span>입력</span><strong>{latest.transcript}</strong><span>출력</span><strong>{interpretationLabel(latest)}</strong><span>해석기</span><strong>{sourceLabel(latest.interpreter_source, latest.vlm_invoked)}</strong><span>상세</span><strong>{latest.detail || "상세 정보 없음"}</strong><span>Service 상태</span><strong>{retraction?.in_flight ? "접수 응답 대기" : "snapshot만으로 전송 여부를 판단하지 않음"}</strong></div> : <div className="debug-empty-state"><Bug aria-hidden="true" size={28} /><p>통합 시나리오에서 아직 해석된 STT 문장이 없습니다.</p></div>}
      </article>
    </section>
  );
}

function LogsPanel({ status }: { status: IntegrationDebugStatus }) {
  const asrFinals = status.asr.finals ?? [];
  const finals = [...asrFinals].reverse().slice(0, 30);
  const vlmEvents = [...status.recent_events].reverse().filter((event) => event.event_type.includes("retraction_voice") || event.event_type.includes("vlm")).slice(0, 20);
  const renderEvents = (events: typeof status.recent_events) => events.map((event, index) => <li key={`${event.stamp}-${event.event_type}-${index}`}><time dateTime={event.stamp}>{formatEventTime(event.stamp)}</time><strong>{event.event_type}</strong><span className="debug-event-summary">{eventSummary(event)}</span><details className="debug-event-raw"><summary>원문</summary><code>{JSON.stringify(event.payload, null, 2)}</code></details></li>);
  return (
    <section className="debug-panel-stack" data-slot="debug-logs-panel">
      <div className="debug-log-grid">
        <article className="debug-section-card debug-event-card"><div className="debug-section-heading"><div><p>STT FINAL LOG</p><h2>확정 문장 관측</h2><span>캡처 제어 없이 ASR final만 표시합니다.</span></div><span className="debug-meta-pill">{asrFinals.length}건</span></div><div className="debug-asr-transcript">{finals.length ? <ol>{finals.map((row, index) => <li key={`${row.stamp}-${index}`}><time dateTime={row.stamp}>{formatEventTime(row.stamp)}</time><span>{row.text}</span><data value={row.response_latency_ms ?? undefined}>{formatLatency(row.response_latency_ms)}</data></li>)}</ol> : <div className="debug-empty-state"><MicOff aria-hidden="true" size={28} /><p>아직 확정된 STT 문장이 없습니다.</p></div>}</div></article>
        <article className="debug-section-card debug-event-card"><div className="debug-section-heading"><div><p>TEXT VLM LOG</p><h2>해석 이벤트</h2><span>VLM 요청과 fallback provenance를 봅니다.</span></div><span className="debug-meta-pill">최근 {vlmEvents.length}건</span></div>{vlmEvents.length ? <ol className="debug-event-list">{renderEvents(vlmEvents)}</ol> : <div className="debug-empty-state"><Bug aria-hidden="true" size={28} /><p>아직 Text VLM 해석 이벤트가 없습니다.</p></div>}</article>
      </div>
      <article className="debug-section-card debug-event-card"><div className="debug-section-heading"><div><p>SESSION EVENT LOG</p><h2>전체 검증 이벤트</h2><span title={status.session.event_log_path}>{status.session.event_log_path}</span></div><span className="debug-meta-pill">최근 {Math.min(status.recent_events.length, 50)}건</span></div>{status.recent_events.length ? <ol className="debug-event-list">{renderEvents([...status.recent_events].reverse().slice(0, 50))}</ol> : <div className="debug-empty-state"><Activity aria-hidden="true" size={28} /><p>아직 기록된 검증 이벤트가 없습니다.</p></div>}</article>
    </section>
  );
}

export default function DebugDiagnosticsPanels({ tab, status, connected, readiness, runCommand, openStt }: {
  tab: DiagnosticTab;
  status: IntegrationDebugStatus;
  connected: boolean;
  readiness: Record<string, unknown> | null;
  runCommand: RunDebugCommand;
  openStt: () => void;
}) {
  if (tab === "vlm") return <TextVlmPanel connected={connected} openStt={openStt} runCommand={runCommand} status={status} />;
  if (tab === "endpoints") return <EndpointDiagnosticsPanel connected={connected} readiness={readiness} runCommand={runCommand} status={status} />;
  return <LogsPanel status={status} />;
}
