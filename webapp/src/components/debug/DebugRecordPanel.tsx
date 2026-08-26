import { type FormEvent, useEffect, useState } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Download,
  FileText,
  LoaderCircle,
  Radio,
  RefreshCw,
  Server,
  ShieldAlert,
  Trash2,
  XCircle,
} from "lucide-react";

import type {
  DebugCommandResponse,
  DebugSurgeryRecordResult,
  IntegrationDebugStatus,
} from "../../hooks/useIntegrationDebugBridge";

type Notice = {
  tone: "success" | "error" | "warning" | "info";
  text: string;
};

type RunDebugCommand = (
  operation: string,
  payload?: Record<string, unknown>,
  options?: { silent?: boolean },
) => Promise<DebugCommandResponse>;

const SURGERY_RECORD_CASE_IDS = Array.from({ length: 12 }, (_, index) => `0704_${index + 6}`);

function todayIsoDate(): string {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

function isValidHttpsEndpoint(value: string): boolean {
  try {
    const endpoint = new URL(value);
    return endpoint.protocol === "https:"
      && !endpoint.username
      && !endpoint.password
      && Boolean(endpoint.hostname)
      && endpoint.pathname !== "/";
  } catch {
    return false;
  }
}

function formatBytes(value: number): string {
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(2)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function downloadJsonArtifact(value: unknown, filename: string) {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: "application/json;charset=utf-8" });
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}

function formatEventTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return date.toLocaleTimeString("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function stateTone(state: string): "ok" | "warn" | "error" | "idle" {
  if (["READY", "LISTENING", "SUCCEEDED", "completed", "succeeded", "accepted", "ARMED"].includes(state)) return "ok";
  if (["TYPE_MISMATCH", "FAULT_LOCKED", "UNAVAILABLE", "ERROR", "FAILED", "failed", "rejected", "cancel_rejected"].includes(state)) return "error";
  if (["LOW_RATE", "STALE", "BUSY", "STARTING", "STOPPING", "SUBMITTING", "REMOTE_STATE_UNKNOWN", "remote_state_unknown", "cancel_requested", "cancel_accepted"].includes(state)) return "warn";
  return "idle";
}

function displayState(state: string): string {
  return state.toLowerCase() === "remote_state_unknown" ? "REMOTE_STATE_UNKNOWN" : state;
}

function StatusBadge({ state, label }: { state: string; label?: string }) {
  const tone = stateTone(state);
  const Icon = tone === "ok" ? CheckCircle2 : tone === "error" ? XCircle : tone === "warn" ? AlertTriangle : Radio;
  return (
    <span className={"debug-status-badge " + tone} data-slot="debug-status-badge">
      <Icon size={14} aria-hidden="true" />
      {label ?? displayState(state)}
    </span>
  );
}

export function DebugRecordPanel({
  status,
  connected,
  runCommand,
  notify,
}: {
  status: IntegrationDebugStatus;
  connected: boolean;
  runCommand: RunDebugCommand;
  notify: (notice: Notice) => void;
}) {
  const record = status.surgery_record;
  const initialCaseId = record.examples.find((example) => example.valid_for_api)?.case_id
    ?? SURGERY_RECORD_CASE_IDS[0];
  const [caseId, setCaseId] = useState(initialCaseId);
  const [endpoint, setEndpoint] = useState(record.default_endpoint);
  const [roomName, setRoomName] = useState("Preclinical Center");
  const [surgeryCode, setSurgeryCode] = useState(initialCaseId);
  const [surgeryDate, setSurgeryDate] = useState(todayIsoDate);
  const [pendingCommand, setPendingCommand] = useState("");

  useEffect(() => {
    if (record.examples.some((example) => example.case_id === caseId && example.valid_for_api)) return;
    const nextCase = record.examples.find((example) => example.valid_for_api)?.case_id;
    if (!nextCase) return;
    setCaseId(nextCase);
    setSurgeryCode((current) => current === caseId ? nextCase : current);
  }, [caseId, record.examples]);

  const selectedExample = record.examples.find((example) => example.case_id === caseId);
  const endpointAllowed = !record.contract.allowed_endpoints?.length
    || record.contract.allowed_endpoints.includes(endpoint.trim());
  const endpointValid = isValidHttpsEndpoint(endpoint) && endpointAllowed;
  const codeValid = /^[A-Za-z0-9_-]{1,50}$/.test(surgeryCode.trim());
  const submitting = record.state === "SUBMITTING";
  const formValid = endpointValid
    && record.api_key_configured
    && Boolean(roomName.trim())
    && codeValid
    && Boolean(surgeryDate)
    && Boolean(selectedExample?.valid_for_api);
  const lastResult = Object.keys(record.last_result).length ? record.last_result : null;
  const lastResultState = lastResult?.state
    ?? (lastResult?.success === true
      ? "SUCCEEDED"
      : lastResult?.success === false
        ? "FAILED"
      : submitting
        ? "SUBMITTING"
        : "REMOTE_STATE_UNKNOWN");
  const lastResultLabel = lastResultState === "SUCCEEDED"
    ? "성공"
    : lastResultState === "FAILED"
      ? "실패"
      : lastResultState === "SUBMITTING"
        ? "응답 대기"
        : "상태 불명";

  async function invoke(operation: string, payload: Record<string, unknown> = {}) {
    setPendingCommand(operation);
    try {
      return await runCommand(operation, payload);
    } finally {
      setPendingCommand("");
    }
  }

  function selectCase(nextCaseId: string) {
    setCaseId(nextCaseId);
    if (surgeryCode === caseId) setSurgeryCode(nextCaseId);
  }

  async function submitRecord(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!formValid || submitting) return;
    await invoke("record_submit", {
      endpoint: endpoint.trim(),
      case_id: caseId,
      room_name: roomName.trim(),
      surgery_code: surgeryCode.trim(),
      date: surgeryDate,
    });
  }

  function downloadReceipt(result: DebugSurgeryRecordResult) {
    const safeCaseId = result.case_id || "surgery-record";
    downloadJsonArtifact(result, `${safeCaseId}-api-receipt.json`);
    notify({ tone: "success", text: "API 검증 영수증 JSON을 다운로드했습니다." });
  }

  return (
    <section className="debug-panel-stack" data-slot="debug-record-panel">
      <div className="debug-record-workspace">
        <form aria-busy={submitting || Boolean(pendingCommand)} className="debug-section-card debug-record-form" onSubmit={(event) => void submitRecord(event)}>
          <div className="debug-section-heading">
            <div><p>POST-OPERATIVE API</p><h2>수술기록 TXT 제출</h2><span>서버에 마운트된 0704_6–0704_17 예제를 API 계약으로 검증합니다.</span></div>
            <StatusBadge state={record.state} label={record.state} />
          </div>

          <label className="debug-field" htmlFor="debug-record-endpoint">
            <span>API endpoint</span>
            <input
              aria-describedby="debug-record-endpoint-help"
              aria-invalid={!endpointValid}
              id="debug-record-endpoint"
              inputMode="url"
              spellCheck={false}
              type="url"
              value={endpoint}
              onChange={(event) => setEndpoint(event.target.value)}
              list="debug-record-endpoint-options"
            />
            <datalist id="debug-record-endpoint-options">
              {(record.contract.allowed_endpoints ?? [record.default_endpoint]).map((candidate) => <option key={candidate} value={candidate} />)}
            </datalist>
            <small id="debug-record-endpoint-help">허용된 계약 endpoint만 전송합니다. 기본값: {record.default_endpoint}</small>
          </label>

          <div className="debug-record-field-grid">
            <label className="debug-field" htmlFor="debug-record-case">
              <span>TXT 예제</span>
              <select id="debug-record-case" value={caseId} onChange={(event) => selectCase(event.target.value)}>
                {SURGERY_RECORD_CASE_IDS.map((candidate) => {
                  const example = record.examples.find((row) => row.case_id === candidate);
                  return <option disabled={!example?.valid_for_api} key={candidate} value={candidate}>{candidate}{example ? ` · ${formatBytes(example.bytes)}` : " · TXT 없음"}</option>;
                })}
              </select>
              <small>{selectedExample ? `${selectedExample.lines.toLocaleString()}줄 · ${selectedExample.characters.toLocaleString()}자 · SHA-256 ${selectedExample.sha256.slice(0, 10)}…` : "새로고침하여 서버 TXT를 확인하세요."}</small>
            </label>
            <label className="debug-field" htmlFor="debug-record-room">
              <span>수술실 roomName</span>
              <input aria-describedby="debug-record-room-help" id="debug-record-room" maxLength={100} required value={roomName} onChange={(event) => setRoomName(event.target.value)} />
              <small id="debug-record-room-help">전임상센터의 계약용 영문명입니다.</small>
            </label>
            <label className="debug-field" htmlFor="debug-record-code">
              <span>수술 코드 surgeryCode</span>
              <input aria-describedby="debug-record-code-help" aria-invalid={!codeValid} id="debug-record-code" maxLength={50} pattern="[A-Za-z0-9_-]+" required value={surgeryCode} onChange={(event) => setSurgeryCode(event.target.value)} />
              <small id="debug-record-code-help">영문, 숫자, 밑줄, 하이픈만 허용됩니다.</small>
            </label>
            <label className="debug-field" htmlFor="debug-record-date">
              <span>수술 날짜</span>
              <input aria-describedby="debug-record-date-help" id="debug-record-date" required type="date" value={surgeryDate} onChange={(event) => setSurgeryDate(event.target.value)} />
              <small id="debug-record-date-help">오늘을 편의상 기본값으로 채웠습니다. 제출 전 실제 수술일과 반드시 대조하세요.</small>
            </label>
          </div>

          <div
            aria-atomic="true"
            className={["debug-record-credential-status", record.api_key_configured ? "is-configured" : "is-missing"].join(" ")}
            role="status"
          >
            {record.api_key_configured ? <CheckCircle2 size={16} aria-hidden="true" /> : <XCircle size={16} aria-hidden="true" />}
            <span><strong>X-API-Key</strong>{record.api_key_configured ? "서버 API 키 설정됨 · 값은 브라우저에 전송되지 않음" : "서버 API 키 미설정 · 제출 비활성화"}</span>
          </div>

          {record.last_error ? <p className="debug-field-error" role="alert"><XCircle size={15} aria-hidden="true" />{record.last_error}</p> : null}
          <div className="debug-record-submit-row">
            <button className="button button-quiet" disabled={!connected || submitting || Boolean(pendingCommand)} onClick={() => void invoke("record_refresh_cases")} type="button">
              {pendingCommand === "record_refresh_cases" ? <LoaderCircle className="debug-spinner" size={16} aria-hidden="true" /> : <RefreshCw size={16} aria-hidden="true" />}TXT 새로고침
            </button>
            <button className="button button-primary" disabled={!connected || !formValid || submitting || Boolean(pendingCommand)} type="submit">
              {submitting || pendingCommand === "record_submit" ? <LoaderCircle className="debug-spinner" size={16} aria-hidden="true" /> : <Server size={16} aria-hidden="true" />}{submitting ? "API 응답 대기 중" : "TXT 제출 시험"}
            </button>
          </div>
          <p className="debug-record-contract-line">{record.contract.method} · {record.contract.content_type} · 최대 {record.contract.max_text_characters.toLocaleString()}자 / {formatBytes(record.contract.max_body_bytes)} · 서버 제한 {record.contract.server_timeout_sec}s</p>
        </form>

        <div className="debug-record-results">
          <article className="debug-section-card debug-record-result-card">
            <div className="debug-section-heading">
              <div><p>LATEST RESULT</p><h2>최근 API 결과</h2><span>{record.active_request_id || lastResult?.request_id || "요청 전"}</span></div>
              {lastResult ? <StatusBadge state={lastResultState} label={lastResultLabel} /> : <StatusBadge state="IDLE" label="대기" />}
            </div>
            <p aria-atomic="true" aria-live="polite" className="sr-only">수술기록 API 상태: {lastResult ? lastResultLabel : "대기"}{lastResult?.http_status ? `. HTTP ${lastResult.http_status}` : ""}</p>
            {lastResult ? (
              <>
                <dl className="debug-record-result-grid">
                  <div><dt>HTTP</dt><dd>{lastResult.http_status || (submitting ? "대기" : "—")}</dd></div>
                  <div><dt>CASE</dt><dd>{lastResult.case_id || "—"}</dd></div>
                  <div><dt>RECEIPT</dt><dd title={lastResult.receipt_id}>{lastResult.receipt_id || "—"}</dd></div>
                  <div><dt>DURATION</dt><dd>{lastResult.duration_sec === undefined ? "—" : `${lastResult.duration_sec.toFixed(3)} s`}</dd></div>
                </dl>
                {lastResult.error_message || lastResult.transport_error ? <p className="debug-field-error" role="alert"><XCircle size={15} aria-hidden="true" />{lastResult.error_message || lastResult.transport_error}</p> : null}
                <div className="debug-record-response-preview">
                  <span>응답 요약</span>
                  <code>{JSON.stringify(lastResult.response_json ?? (lastResult.response_text ? { text: lastResult.response_text } : { state: record.state }), null, 2)}</code>
                </div>
                <button className="button button-secondary full" onClick={() => downloadReceipt(lastResult)} type="button"><Download size={16} aria-hidden="true" />검증 영수증 JSON 다운로드</button>
              </>
            ) : (
              <div className="debug-empty-state"><FileText size={28} aria-hidden="true" /><p>제출 후 HTTP 상태와 안전한 응답 메타데이터가 표시됩니다.</p></div>
            )}
            {!record.contract.result_lookup_defined || !record.contract.generated_record_body_returned ? (
              <p className="debug-result-boundary"><ShieldAlert size={15} aria-hidden="true" />현재 외부 계약은 생성된 수술기록 본문 조회·다운로드 endpoint를 정의하지 않습니다. 위 다운로드는 API 응답 검증 영수증이며 임상 기록 결과물이 아닙니다.</p>
            ) : null}
          </article>
        </div>
      </div>

      <article className="debug-section-card debug-record-history-card">
        <div className="debug-section-heading">
          <div><p>BOUNDED HISTORY</p><h2>제출 시험 이력</h2><span>API 키와 TXT 본문은 이력에 포함되지 않습니다.</span></div>
          <div className="debug-heading-actions">
            <span className="debug-meta-pill">{record.history.length}/20건</span>
            <button className="button button-quiet" disabled={!connected || submitting || !record.history.length || Boolean(pendingCommand)} onClick={() => void invoke("record_clear_history")} type="button"><Trash2 size={16} aria-hidden="true" />이력 지우기</button>
          </div>
        </div>
        {record.history.length ? (
          <div className="debug-table-scroll">
            <table className="debug-table debug-record-history-table">
              <caption className="sr-only">수술기록 API 제출 시험 이력</caption>
              <thead><tr><th>완료 시각</th><th>Case · 수술실</th><th>HTTP</th><th>Receipt · 오류</th><th>다운로드</th></tr></thead>
              <tbody>{[...record.history].reverse().map((result, index) => {
                const resultState = result.state ?? (result.success === true ? "SUCCEEDED" : result.success === false ? "FAILED" : "REMOTE_STATE_UNKNOWN");
                const resultLabel = resultState === "SUCCEEDED" ? "성공" : resultState === "FAILED" ? "실패" : "상태 불명";
                return (
                  <tr key={`${result.request_id || "record"}-${index}`}>
                    <td><strong>{result.completed_at ? formatEventTime(result.completed_at) : "—"}</strong><small>{result.duration_sec === undefined ? "" : `${result.duration_sec.toFixed(3)} s`}</small></td>
                    <td><strong>{result.case_id || "—"}</strong><small>{result.room_name || "수술실 미지정"} · {result.surgery_code || "코드 없음"}</small></td>
                    <td><StatusBadge state={resultState} label={`${result.http_status || "—"} · ${resultLabel}`} /></td>
                    <td><code>{result.receipt_id || result.error_code || "receipt 없음"}</code><small>{result.error_message || result.transport_error || (result.success === undefined ? "결과 상태 미확정" : "오류 없음")}</small></td>
                    <td><button aria-label={`${result.case_id || "수술기록"} 검증 영수증 다운로드`} className="button button-quiet" onClick={() => downloadReceipt(result)} type="button"><Download size={15} aria-hidden="true" />JSON</button></td>
                  </tr>
                );
              })}</tbody>
            </table>
          </div>
        ) : <div className="debug-empty-state"><Activity size={28} aria-hidden="true" /><p>아직 완료된 API 시험이 없습니다.</p></div>}
      </article>
    </section>
  );
}
