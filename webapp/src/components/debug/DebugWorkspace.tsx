import { FormEvent, KeyboardEvent, lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, LayoutGroup } from "framer-motion";
import * as m from "framer-motion/m";
import {
  Activity,
  AlertTriangle,
  Bug,
  Cable,
  CheckCircle2,
  ChevronDown,
  CircleStop,
  Download,
  FileText,
  Headphones,
  LoaderCircle,
  LogOut,
  Mic,
  MicOff,
  Network,
  Play,
  Radio,
  RefreshCw,
  Router,
  RotateCcw,
  ScanLine,
  Send,
  Server,
  Shield,
  ShieldAlert,
  Square,
  ToggleLeft,
  ToggleRight,
  Trash2,
  Usb,
  Wrench,
  XCircle,
} from "lucide-react";

import {
  type DebugCommandResponse,
  type DebugInputStatus,
  type DebugNetworkStatus,
  type DebugOutputStatus,
  type DebugSurgeryRecordResult,
  DEBUG_STATUS_MAX_AGE_MS,
  type IntegrationDebugStatus,
  useIntegrationDebugBridge,
} from "../../hooks/useIntegrationDebugBridge";
import toolHandoverProfiles from "../../config/debugToolHandoverProfiles.json";
import { runtimeBridgeUrl } from "../../runtimeModes";
import { silk, statusSwap } from "../../motion-system";
import type { Language } from "../../utils/display";

const DebugDiagnosticsPanels = lazy(() => import("./DebugDiagnosticsPanels"));
const DebugIntegrationPipeline = lazy(() => import("./DebugDiagnosticsPanels").then((module) => ({ default: module.DebugIntegrationPipeline })));
const ForceRetractionIdleControl = lazy(() => import("./DebugDiagnosticsPanels").then((module) => ({ default: module.ForceRetractionIdleControl })));
const DebugPerceptionPanel = lazy(() => import("./DebugPerceptionPanel").then((module) => ({ default: module.DebugPerceptionPanel })));
const DebugTfPanel = lazy(() => import("./DebugTfPanel").then((module) => ({ default: module.DebugTfPanel })));
const DebugMulticamOpsPanel = lazy(() => import("../multicam/MulticamOpsWorkspace"));

type DebugTab =
  | "connection"
  | "stt"
  | "vlm"
  | "perception"
  | "tf"
  | "multicam"
  | "endpoints"
  | "tool_voice"
  | "retractor"
  | "logs"
  | "output"
  | "record";

type DebugTabGroup = "individual" | "scenario" | "observability" | "utilities";

interface DebugTabItem {
  id: DebugTab;
  group: DebugTabGroup;
  label: string;
  meta: string;
  icon: typeof Radio;
}

interface Notice {
  tone: "success" | "error" | "warning" | "info";
  text: string;
}

interface DebugCommandOptions {
  silent?: boolean;
}

const SILENT_COMMAND_OPTIONS: DebugCommandOptions = { silent: true };

const DEBUG_OPERATIONAL_STOPPED_LABEL = "운영 시나리오 정지 확인";
const DEBUG_OPERATIONAL_UNKNOWN_LABEL = "운영 시나리오 실행/상태 불명";
const DEBUG_OPERATIONAL_STALE_LABEL = "운영 안전 상태 확인 대기";
const DEBUG_STANDALONE_LABEL = "Standalone Debug · 플래너 미탐색";

interface ToolHandoverOption {
  catalogId: string;
  instrumentId: string;
  instanceIds: readonly string[];
}

type RetractionCommand =
  | "start_direct_teach"
  | "finish_direct_teach"
  | "start_retraction"
  | "adjust_retraction"
  | "change_tool"
  | "stop_retraction";

const TOOL_HANDOVER_OPTIONS: readonly ToolHandoverOption[] = toolHandoverProfiles.profiles;

const DEFAULT_TOOL_HANDOVER_OPTION = TOOL_HANDOVER_OPTIONS[0];

type RunDebugCommand = (
  operation: string,
  payload?: Record<string, unknown>,
  options?: DebugCommandOptions,
) => Promise<DebugCommandResponse>;

interface DebugPingResult {
  target_ip: string;
  source_ip: string;
  sent: number;
  received: number;
  packet_loss_percent: number;
  reachable: boolean;
  error: string;
  rtt_ms: { min: number; avg: number; max: number } | null;
}

function wiredInterfaceSelected(network: DebugNetworkStatus): boolean {
  return network.interface_kind === "ethernet" || Boolean(network.preferred_interface);
}

function localAddressLabel(network: DebugNetworkStatus): string {
  if (network.primary_ipv4) return network.primary_ipv4;
  return wiredInterfaceSelected(network) ? "유선 IP 없음" : "주소 없음";
}

function localLinkLabel(network: DebugNetworkStatus): string {
  if (network.interface_present === false) return "인터페이스 없음";
  if (network.link_up === false) return "케이블 미연결";
  if (!network.primary_ipv4) return network.link_up === true ? "링크 연결 · IPv4 없음" : "IPv4 할당 대기";
  return network.interface_kind === "ethernet" ? "유선 연결" : "연결됨";
}

const SURGERY_RECORD_CASE_IDS = Array.from({ length: 12 }, (_, index) => `0704_${index + 6}`);

function todayIsoDate(): string {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

type PuzzleAsrEndpointId = "cloud" | "lan";

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

function formatHz(value: number): string {
  return `${value.toFixed(value >= 10 ? 1 : 2)} Hz`;
}

function formatAge(value: number | null): string {
  if (value === null) return "수신 전";
  return value < 1 ? `${Math.round(value * 1000)} ms 전` : `${value.toFixed(1)} s 전`;
}

function formatBandwidth(value: number): string {
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(2)} MB/s`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB/s`;
  return `${Math.round(value)} B/s`;
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

function formatAsrLatency(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${value.toFixed(1)} ms`
    : "측정 전";
}

function eventSummary(event: { event_type: string; payload: Record<string, unknown> }): string {
  const payload = event.payload;
  const stringValue = (key: string) => typeof payload[key] === "string" ? String(payload[key]) : "";
  if (event.event_type === "session_started") {
    return `Domain ${stringValue("ros_domain_id") || "-"}에서 검증 세션 시작`;
  }
  if (event.event_type === "sentence_received") {
    const parse = payload.parse && typeof payload.parse === "object"
      ? payload.parse as Record<string, unknown>
      : {};
    const parsed = typeof parse.operation === "string"
      ? parse.operation
      : typeof parse.reason === "string"
        ? parse.reason
        : "해석 대기";
    return `“${stringValue("text") || "빈 문장"}” · ${parsed}`;
  }
  if (event.event_type === "command_started") {
    return `${stringValue("route") || "명령"} 시작 · ${stringValue("source") || "manual"}`;
  }
  if (event.event_type === "command_finished") {
    return `${stringValue("route") || "명령"} · ${displayState(stringValue("final_state") || "완료")} · ${stringValue("reason_code") || "reason 없음"}`;
  }
  if (event.event_type === "ui_command") {
    const accepted = payload.accepted === true ? "수락" : "거부";
    return `${stringValue("operation") || "UI 명령"} · ${accepted} · ${stringValue("message") || "응답 메시지 없음"}`;
  }
  if (event.event_type === "voice_dispatch") {
    return `${payload.accepted === true ? "실행 요청" : "실행 거부"} · ${stringValue("message") || "응답 메시지 없음"}`;
  }
  if (event.event_type === "asr_started") {
    return `${stringValue("device_name") || "USB 마이크"} · ${stringValue("server_url") || "ASR 서버"}`;
  }
  if (event.event_type === "asr_final") {
    const latency = typeof payload.response_latency_ms === "number"
      ? ` · ${formatAsrLatency(payload.response_latency_ms)}`
      : "";
    return `확정 문장 · “${stringValue("text") || "빈 문장"}”${latency}`;
  }
  if (event.event_type === "asr_stopped") {
    return `ASR 정지 · 확정 ${String(payload.final_count ?? 0)}건`;
  }
  if (event.event_type === "record_submit_started") {
    return `${stringValue("case_id") || "수술기록"} 제출 · ${stringValue("room_name") || "수술실 미지정"}`;
  }
  if (event.event_type === "record_submit_finished") {
    return `${stringValue("case_id") || "수술기록"} · HTTP ${String(payload.http_status ?? 0)} · ${payload.success === true ? "성공" : "실패"}`;
  }
  if (event.event_type === "planner_coexistence_changed") {
    const current = Array.isArray(payload.current_blocked_nodes)
      ? payload.current_blocked_nodes.join(", ")
      : "없음";
    return `플래너 노드 변화로 수동 제어 자동 해제 · 현재 ${current || "없음"}`;
  }
  if (event.event_type === "action_recovery_required") {
    return `${stringValue("route") || "Action"} 원격 상태 확인 필요 · ${stringValue("reason_code") || "원인 미상"}`;
  }
  if (event.event_type === "service_admission_recovery_required") {
    return `${stringValue("route") || "Service"} 요청 접수 상태 확인 필요 · ${stringValue("reason_code") || "원인 미상"}`;
  }
  if (event.event_type === "retraction_service_response") {
    const command = stringValue("command") || "retraction command";
    const accepted = payload.request_accepted === true ? "요청 수락" : "요청 거부";
    return `${command} · ${accepted} · ${stringValue("reason_code") || "결과 코드 없음"}`;
  }
  if (event.event_type === "action_client_recovered" || event.event_type === "command_client_recovered") {
    return `${stringValue("route") || "명령"} 클라이언트 상태 복구 · 원격 상태 확인됨`;
  }
  if (event.event_type === "action_late_result_reconciled") {
    return `${stringValue("route") || "Action"} 지연 Result 자동 반영 · ${stringValue("final_state") || "종료"}`;
  }
  const values = Object.entries(payload)
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`);
  return values.join(" · ") || "추가 정보 없음";
}

function isValidIpv4(value: string): boolean {
  const parts = value.trim().split(".");
  return parts.length === 4 && parts.every((part) => {
    if (!/^\d{1,3}$/.test(part)) return false;
    const number = Number(part);
    return number >= 0 && number <= 255 && String(number) === part.replace(/^0+(?=\d)/, "");
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

function retractionInternalStateLabel(state: string): string {
  switch (state) {
    case "idle":
      return "대기";
    case "direct_teaching":
      return "직접 교시 중";
    case "taught_ready":
      return "교시 완료";
    case "retraction_active":
      return "리트랙션 요청 접수";
    case "unknown":
      return "접수 상태 미확정";
    default:
      return state || "대기";
  }
}

function retractionCommandLabel(command: string | null | undefined): string {
  switch (command) {
    case "start_direct_teach":
      return "직접 교시 시작";
    case "finish_direct_teach":
      return "직접 교시 종료";
    case "start_retraction":
      return "Retraction 시작";
    case "adjust_retraction":
      return "Retraction 더";
    case "change_tool":
      return "Tool change";
    case "stop_retraction":
      return "Retraction 종료";
    default:
      return "해석 안 됨";
  }
}

function retractionInterpretationLabel(interpretation: {
  command: string | null;
  target_side: string;
  distance_m: number;
  reason: string;
} | undefined): string {
  if (!interpretation?.command) return interpretation?.reason || "수신 전";
  if (interpretation.command !== "adjust_retraction") {
    return retractionCommandLabel(interpretation.command);
  }
  const distanceCm = interpretation.distance_m * 100;
  const side = interpretation.target_side === "left" ? "왼쪽" : interpretation.target_side === "right" ? "오른쪽" : "대상 없음";
  return `${retractionCommandLabel(interpretation.command)} · ${side} ${Number.isFinite(distanceCm) ? distanceCm.toFixed(distanceCm % 1 === 0 ? 0 : 1) : "?"} cm`;
}

function retractionInterpreterSourceLabel(interpretation: {
  interpreter_source?: string;
  vlm_invoked?: boolean;
} | undefined): string {
  if (!interpretation) return "수신 전";
  if (interpretation.interpreter_source === "text_vlm_pending") {
    return "Text VLM 요청 제출 · 응답 대기";
  }
  if (interpretation.interpreter_source === "text_vlm_busy") {
    return "Text VLM 이전 요청 처리 중 · 새 문장 보류";
  }
  if (interpretation.interpreter_source === "text_vlm") {
    return "Text VLM · 원문 근거 재검증 완료";
  }
  if (interpretation.interpreter_source === "deterministic_fallback") {
    return interpretation.vlm_invoked
      ? "Text VLM 호출 후 공용 정규화기로 폴백"
      : "공용 정규화기로 폴백 · VLM 미호출";
  }
  if (interpretation.interpreter_source === "shared_deterministic") {
    return interpretation.vlm_invoked
      ? "공용 결정론 정규화기 · VLM 호출됨"
      : "공용 결정론 정규화기 · VLM 미호출";
  }
  if (interpretation.interpreter_source) {
    return interpretation.vlm_invoked
      ? `${interpretation.interpreter_source} · VLM 호출됨`
      : interpretation.interpreter_source;
  }
  return "해석기 정보 없음";
}

function retractionInterpreterDetailLabel(detail: string | undefined): string {
  if (!detail) return "상세 정보 없음";
  switch (detail) {
    case "deterministic_normalizer":
      return "공용 정규화기를 직접 사용했습니다.";
    case "text_vlm_not_configured":
      return "Text VLM endpoint 또는 model이 없어 공용 정규화기를 사용했습니다.";
    case "text_vlm_runtime_unavailable":
      return "Text VLM 런타임을 준비할 수 없어 공용 정규화기를 사용했습니다.";
    case "text_vlm_request_submitted":
      return "Text VLM 요청을 제출하고 비동기 응답을 기다립니다.";
    case "previous_text_vlm_request_pending":
      return "이전 Text VLM 요청이 끝나지 않아 새 확정 문장을 보류했습니다.";
    case "text_vlm_normalized":
      return "Text VLM 결과가 폐쇄형 스키마와 원문 근거 검증을 통과했습니다.";
    default:
      if (detail.startsWith("text_vlm_unavailable:")) {
        return `Text VLM 연결 또는 응답 실패로 공용 정규화기를 사용했습니다. (${detail.split(":").slice(1).join(":")})`;
      }
      if (detail.startsWith("text_vlm_submit_error:")) {
        return `Text VLM 요청 제출 실패로 공용 정규화기를 사용했습니다. (${detail.split(":").slice(1).join(":")})`;
      }
      if (detail.startsWith("text_vlm_executor_error:")) {
        return `Text VLM 작업 처리 실패로 공용 정규화기를 사용했습니다. (${detail.split(":").slice(1).join(":")})`;
      }
      return detail;
  }
}

function retractionAllowedCommandsLabel(commands: readonly string[]): string {
  return commands.length
    ? commands.map((command) => retractionCommandLabel(command)).join(" · ")
    : "없음";
}

function manualAvailabilityLabel(status: IntegrationDebugStatus | null): string {
  if (!status) return "수동 잠금 · 상태 대기";
  const admissionOnly = status.action.response_semantics === "admission";
  if (status.action.recovery_required) return `수동 잠금 · ${admissionOnly ? "Service 요청" : "Action"} 복구 필요`;
  if (status.session.fault_locked) return "수동 잠금 · Fault";
  if (!status.action.terminal) return `수동 잠금 · ${admissionOnly ? "Service 응답 대기" : "Action 실행 중"}`;
  if (status.session.armed) return "수동 제어 활성";
  if (status.runtime.manual_control_available === true) return "수동 활성화 가능";
  if (status.runtime.operational_runtime_stopped !== true) return "수동 잠금 · 시나리오 상태";
  return "수동 잠금 · 안전 조건";
}

function actionRecoveryExplanation(reasonCode: string): string {
  switch (reasonCode) {
    case "action_server_unavailable":
      return "실행 중이던 Action 서버가 DDS에서 계속 탐색되지 않아 원격 Goal 상태를 확정할 수 없습니다.";
    case "service_server_unavailable":
      return "요청을 처리하던 Service 서버가 DDS에서 계속 탐색되지 않아 응답 상태를 확정할 수 없습니다.";
    case "goal_response_timeout":
      return "Goal 제출 후 서버의 수락 또는 거부 응답이 제한 시간 안에 도착하지 않았습니다.";
    case "service_response_timeout":
      return "Service 요청 결과가 제한 시간 안에 도착하지 않았습니다.";
    case "service_response_error":
      return "Service 응답을 읽는 중 오류가 발생해 요청 접수 여부를 확정할 수 없습니다.";
    case "response_command_id_mismatch":
      return "Service 응답의 Command ID가 현재 요청과 달라 요청 접수 여부를 확정할 수 없습니다.";
    case "response_contract_mismatch":
      return "Service 응답의 수락 표시와 결과 코드가 서로 맞지 않아 요청 접수 여부를 확정할 수 없습니다.";
    case "action_update_timeout":
      return "Action 서버의 Feedback, Cancel 응답 또는 Result가 제한 시간 동안 갱신되지 않았습니다.";
    case "action_duration_timeout":
      return "Action이 허용된 최대 관찰 시간을 넘겼습니다.";
    case "cancel_rejected":
      return "Action 서버가 취소 요청을 거부했습니다. 원격 로봇은 계속 동작 중일 수 있습니다.";
    case "cancel_response_error":
      return "취소 응답을 받는 중 연결 오류가 발생해 원격 취소 여부를 확인할 수 없습니다.";
    default:
      return "원격 명령의 종료 상태를 확정할 수 없습니다.";
  }
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

function AsrLiveMonitor({
  asr,
  dataSlot,
}: {
  asr: IntegrationDebugStatus["asr"];
  dataSlot: string;
}) {
  const levelPercent = Math.max(0, Math.min(100, ((asr.audio_level_dbfs + 60) / 60) * 100));
  const partialText = asr.partial_text || (asr.state === "LISTENING" ? "음성 대기 중…" : "ASR 시작 전");
  return (
    <div className="debug-asr-live" data-slot={dataSlot}>
      <div className="debug-asr-level">
        <span>입력 레벨</span>
        <div
          aria-label={`마이크 입력 레벨 ${asr.audio_level_dbfs.toFixed(1)} dBFS`}
          aria-valuemax={0}
          aria-valuemin={-60}
          aria-valuenow={Math.max(-60, Math.min(0, asr.audio_level_dbfs))}
          className="debug-asr-meter"
          role="meter"
        >
          <span style={{ width: `${levelPercent}%` }} />
        </div>
        <strong>{asr.audio_level_dbfs.toFixed(1)} dBFS</strong>
      </div>
      <p aria-atomic="true" aria-live="polite" className={asr.partial_text ? "active" : ""}>
        <span>부분 인식</span>
        <strong>{partialText}</strong>
      </p>
    </div>
  );
}

function DebugHeader({
  connected,
  status,
  statusReceivedAt,
  url,
  manualControlLabel,
  manualControlDisabled,
  manualControlPending,
  onManualControl,
  onExit,
}: {
  connected: boolean;
  status: IntegrationDebugStatus | null;
  statusReceivedAt: number;
  url: string;
  manualControlLabel: string;
  manualControlDisabled: boolean;
  manualControlPending: boolean;
  onManualControl: () => void;
  onExit: () => void;
}) {
  const [statusAgeSec, setStatusAgeSec] = useState<number | null>(() =>
    statusReceivedAt ? Math.max(0, (Date.now() - statusReceivedAt) / 1_000) : null,
  );
  useEffect(() => {
    const updateAge = () => setStatusAgeSec(
      statusReceivedAt ? Math.max(0, (Date.now() - statusReceivedAt) / 1_000) : null,
    );
    updateAge();
    const timer = window.setInterval(updateAge, 500);
    return () => window.clearInterval(timer);
  }, [statusReceivedAt]);
  const statusFresh = Boolean(status && connected);
  const ManualControlIcon = status?.action.recovery_required || status?.session.fault_locked
    ? status?.session.fault_locked ? RotateCcw : ShieldAlert
    : status?.session.armed ? CircleStop : Play;
  const manualControlAvailable = statusFresh && status?.runtime.manual_control_available === true;
  const operationalRuntimeStopped = statusFresh && status?.runtime.operational_runtime_stopped === true;
  const runtimeNetworkLocked = statusFresh && status?.runtime.network.locked_to_runtime === true;
  const availabilityLabel = statusFresh ? manualAvailabilityLabel(status) : "수동 잠금 · 상태 확인 대기";
  const availabilityState = !statusFresh
    ? "STALE"
    : status?.action.recovery_required || status?.session.fault_locked
    ? "FAULT_LOCKED"
    : status && !status.action.terminal
      ? "BUSY"
      : manualControlAvailable || status?.session.armed
        ? "READY"
        : "STALE";
  return (
    <header className="debug-header" data-slot="debug-header">
      <div className="debug-title-block">
        <span className="debug-title-icon"><Bug size={22} aria-hidden="true" /></span>
        <div>
          <p>INTEGRATION WORKBENCH</p>
          <h1>디버그 모드</h1>
          <span>시나리오 없이 ROS 입출력과 개별 로봇 기능을 검증합니다.</span>
        </div>
      </div>
      <div className="debug-header-status" aria-label="디버그 런타임 상태">
        <StatusBadge state={connected ? "READY" : "WAITING"} label={connected ? "ROS 연결" : "ROS 대기"} />
        <span className="debug-meta-pill" title={url}>D{status?.runtime.ros_domain_id ?? "-"} · {status?.runtime.discovery_range ?? "DISCOVERY"}</span>
        <StatusBadge state={statusAgeSec !== null && statusAgeSec <= DEBUG_STATUS_MAX_AGE_MS / 1_000 ? "READY" : "STALE"} label={statusAgeSec === null ? "상태 대기" : statusAgeSec < 1 ? "방금 갱신" : `${statusAgeSec.toFixed(1)}초 전`} />
        <StatusBadge
          state={operationalRuntimeStopped ? "READY" : "STALE"}
          label={operationalRuntimeStopped
            ? runtimeNetworkLocked ? DEBUG_OPERATIONAL_STOPPED_LABEL : DEBUG_STANDALONE_LABEL
            : statusFresh ? DEBUG_OPERATIONAL_UNKNOWN_LABEL : DEBUG_OPERATIONAL_STALE_LABEL}
        />
        <StatusBadge state={availabilityState} label={availabilityLabel} />
        <button
          aria-busy={manualControlPending}
          aria-pressed={status?.session.armed ?? false}
          className={`button ${status?.session.armed ? "button-secondary" : "button-primary"} debug-manual-toggle`}
          disabled={manualControlDisabled}
          onClick={onManualControl}
          type="button"
        >
          {manualControlPending
            ? <LoaderCircle className="debug-spinner" size={16} aria-hidden="true" />
            : <ManualControlIcon size={16} aria-hidden="true" />}
          {manualControlLabel}
        </button>
        <span aria-live="polite" className="sr-only">수동 제어 상태: {status?.session.state ?? "상태 대기"}</span>
        <button className="button button-secondary debug-exit-button" disabled={manualControlPending} onClick={onExit} type="button">
          <LogOut size={16} aria-hidden="true" />
          운영 화면으로
        </button>
      </div>
    </header>
  );
}

function ConnectionFallback({
  connected,
  reconnecting,
  error,
  url,
  onRetry,
}: {
  connected: boolean;
  reconnecting: boolean;
  error: string;
  url: string;
  onRetry: () => void;
}) {
  if (!error && (connected || reconnecting)) {
    return (
      <main className="debug-feedback-card debug-loading-card" aria-live="polite" data-slot="debug-loading-state" id="debug-fallback">
        <div className="debug-skeleton-title" />
        <div className="debug-skeleton-row" />
        <div className="debug-skeleton-row short" />
        <p>{reconnecting ? "DDS 설정을 반영하고 디버그 모드에 다시 연결하고 있습니다." : "디버그 노드의 첫 상태 메시지를 기다리고 있습니다."}</p>
      </main>
    );
  }
  return (
    <main className="debug-feedback-card" data-slot="debug-error-state" id="debug-fallback">
      <AlertTriangle size={32} aria-hidden="true" />
      <h2>디버그 ROSBridge에 연결할 수 없습니다</h2>
      <p>{error || `${url}에서 연결 응답을 기다리고 있습니다.`}</p>
      <code>scripts/taskplanner up debug --build</code>
      <button className="button button-primary" onClick={onRetry} type="button">
        <RefreshCw size={16} aria-hidden="true" />
        다시 연결
      </button>
    </main>
  );
}

function NetworkPanel({
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
  const network = status.runtime.network;
  const wiredSelected = wiredInterfaceSelected(network);
  const addressLabel = localAddressLabel(network);
  const linkLabel = localLinkLabel(network);
  const interfaceName = network.primary_interface || network.preferred_interface || "지정된 유선 인터페이스";
  const missingAddressTitle = network.interface_present === false
    ? "지정한 유선 인터페이스를 찾을 수 없습니다"
    : network.link_up === false
      ? "유선 케이블이 연결되지 않았습니다"
      : network.link_up === true
        ? "유선 링크는 연결됐지만 IPv4가 없습니다"
        : "유선 LAN 주소를 기다리고 있습니다";
  const missingAddressHint = network.interface_present === false
    ? `${interfaceName} 이름과 장치 상태를 확인해 주세요. Wi-Fi 주소는 주 주소로 대체하지 않습니다.`
    : network.link_up === false
      ? `${interfaceName}에 케이블을 연결하고 IPv4를 할당해 주세요. Wi-Fi 주소는 주 주소로 대체하지 않습니다.`
      : network.link_up === true
        ? `${interfaceName}의 물리 링크는 연결됐습니다. 공유기 WAN이 아닌 상대 PC와 같은 LAN 포트에서 DHCP 주소를 받거나 고정 IPv4를 설정해 주세요.`
        : `${interfaceName}의 링크 상태와 IPv4 설정을 확인해 주세요. Wi-Fi 주소는 주 주소로 대체하지 않습니다.`;
  const [domainId, setDomainId] = useState(status.runtime.ros_domain_id);
  const [discoveryRange, setDiscoveryRange] = useState(
    status.runtime.discovery_range === "SUBNET" ? "SUBNET" : "LOCALHOST",
  );
  const [networkPending, setNetworkPending] = useState(false);
  const [pingTarget, setPingTarget] = useState("");
  const [pingPending, setPingPending] = useState(false);
  const [pingError, setPingError] = useState("");
  const [pingResult, setPingResult] = useState<DebugPingResult | null>(null);
  const networkLocked = network.locked_to_runtime === true;

  useEffect(() => {
    setDomainId(status.runtime.ros_domain_id);
    setDiscoveryRange(status.runtime.discovery_range === "SUBNET" ? "SUBNET" : "LOCALHOST");
  }, [status.session.session_id, status.runtime.discovery_range, status.runtime.ros_domain_id]);

  const parsedDomain = Number(domainId);
  const validDomain = Number.isInteger(parsedDomain) && parsedDomain >= 0 && parsedDomain <= 232;
  const changed =
    validDomain &&
    (String(parsedDomain) !== status.runtime.ros_domain_id || discoveryRange !== status.runtime.discovery_range);
  const outputActive = status.outputs.some((row) => row.enabled);
  const changeBlocked = status.session.armed || !status.action.terminal || outputActive;
  const domainCollisionWarning = validDomain && parsedDomain >= 102 && parsedDomain <= 214;
  const activeAddresses = network.addresses.filter(
    (row) =>
      row.up &&
      !row.loopback &&
      row.interface !== "docker0" &&
      row.interface !== "tailscale0" &&
      !row.interface.startsWith("br-") &&
      !row.interface.startsWith("veth"),
  );
  const secondaryAddresses = activeAddresses.filter((row) => !row.primary);

  async function applyNetworkSettings(event: FormEvent) {
    event.preventDefault();
    if (networkLocked) {
      notify({ tone: "warning", text: "운영 런타임과 동일한 DDS 설정으로 잠겨 있습니다." });
      return;
    }
    if (!validDomain) {
      notify({ tone: "error", text: "Domain ID는 0부터 232 사이의 정수로 입력해 주세요." });
      return;
    }
    setNetworkPending(true);
    const response = await runCommand(
      "apply_network_settings",
      { domain_id: parsedDomain, discovery_range: discoveryRange },
      { silent: true },
    );
    setNetworkPending(false);
    if (!response.accepted) {
      notify({ tone: "error", text: response.message || "DDS 설정을 적용하지 못했습니다." });
      return;
    }
    notify({
      tone: response.result.restart_required ? "info" : "success",
      text: response.result.restart_required
        ? "DDS 설정을 저장했습니다. 디버그 모드가 자동으로 다시 연결됩니다."
        : "이미 같은 DDS 설정을 사용하고 있습니다.",
    });
  }

  async function runPing(event: FormEvent) {
    event.preventDefault();
    const target = pingTarget.trim();
    if (!isValidIpv4(target)) {
      setPingError("점으로 구분된 IPv4 주소를 입력해 주세요. 예: 10.125.185.91");
      setPingResult(null);
      return;
    }
    setPingPending(true);
    setPingError("");
    setPingResult(null);
    const response = await runCommand("ping_host", { target_ip: target }, { silent: true });
    setPingPending(false);
    if (!response.accepted) {
      setPingError(response.message || "핑 테스트를 실행하지 못했습니다.");
      notify({ tone: "error", text: response.message || "핑 테스트를 실행하지 못했습니다." });
      return;
    }
    const result = response.result as unknown as DebugPingResult;
    setPingResult(result);
    notify({
      tone: result.reachable ? "success" : "warning",
      text: result.reachable
        ? `${result.target_ip}에서 ICMP 응답을 받았습니다.`
        : `${result.target_ip}에서 응답을 받지 못했습니다. 상대 PC와 방화벽을 확인해 주세요.`,
    });
  }

  return (
    <details className="debug-section-card debug-network-card" data-slot="debug-network-settings">
      <summary className="debug-network-toggle">
        <div className="debug-network-toggle-title">
          <Network size={19} aria-hidden="true" />
          <div><p>DDS NETWORK</p><h2>DDS·LAN 설정</h2></div>
        </div>
        <div className="debug-network-overview" aria-label="현재 네트워크 설정">
          <span><small>LOCAL IP</small><strong title={network.primary_interface}>{addressLabel}</strong></span>
          <span><small>DOMAIN</small><strong>{status.runtime.ros_domain_id}</strong></span>
          <span><small>DISCOVERY</small><strong>{status.runtime.discovery_range || "미지정"}</strong></span>
        </div>
        <span className="debug-network-toggle-hint">설정·핑</span>
        <ChevronDown className="debug-network-chevron" size={18} aria-hidden="true" />
      </summary>

      <div className="debug-network-expanded">
        <p className="debug-network-description">{networkLocked ? "운영 런타임과 동일한 DDS 설정으로 잠겨 있습니다. 모니터링과 핑 테스트는 계속 사용할 수 있습니다." : "설정 적용 시 디버그 ROS 런타임만 재시작되며 UI가 자동으로 다시 연결됩니다."}</p>
        <div className="debug-network-layout">
        <section className="debug-network-summary" aria-labelledby="debug-local-network-title">
          <div className="debug-network-subheading">
            <Cable size={18} aria-hidden="true" />
            <div><span>LOCAL INTERFACE</span><h3 id="debug-local-network-title">현재 로컬 주소</h3></div>
          </div>
          <div className="debug-primary-address">
            <strong>{addressLabel}<small>{network.prefix_length ? `/${network.prefix_length}` : ""}</small></strong>
            <span>{network.primary_interface || "인터페이스 미확인"} · {linkLabel}</span>
          </div>
          {wiredSelected && !network.primary_ipv4 ? (
            <div className="debug-interface-state" role="status">
              <AlertTriangle size={18} aria-hidden="true" />
              <div>
                <strong>{missingAddressTitle}</strong>
                <span>{missingAddressHint}</span>
              </div>
            </div>
          ) : null}
          <dl className="debug-network-facts">
            <div><dt>Gateway</dt><dd>{network.gateway_ipv4 || "없음"}</dd></div>
            <div><dt>Multicast</dt><dd>{network.multicast_capable ? "지원" : "확인 필요"}</dd></div>
            <div><dt>RMW</dt><dd>{status.runtime.rmw_implementation || "미지정"}</dd></div>
          </dl>
          {secondaryAddresses.length ? (
            <details className="debug-address-details">
              <summary>다른 활성 IPv4 주소 {secondaryAddresses.length}개</summary>
              <ul>{secondaryAddresses.map((row) => <li key={`${row.interface}-${row.address}`}><code>{row.address}/{row.prefix_length}</code><span>{row.interface}{row.kind === "wifi" ? " · Wi-Fi" : ""}</span></li>)}</ul>
            </details>
          ) : null}
        </section>

        <form className="debug-network-settings" onSubmit={(event) => void applyNetworkSettings(event)}>
          <div className="debug-network-subheading">
            <Router size={18} aria-hidden="true" />
            <div><span>ROS 2 DISCOVERY</span><h3>DDS 설정</h3></div>
          </div>
          <fieldset className="debug-network-fieldset">
            <legend>Discovery 범위</legend>
            <div className="debug-segmented-control">
              {(["LOCALHOST", "SUBNET"] as const).map((value) => (
                <button aria-pressed={discoveryRange === value} className={discoveryRange === value ? "active" : ""} disabled={!connected || networkLocked} key={value} onClick={() => setDiscoveryRange(value)} type="button">
                  {value === "LOCALHOST" ? "이 컴퓨터만" : "같은 LAN"}
                  <small>{value}</small>
                </button>
              ))}
            </div>
          </fieldset>
          <label className="debug-field" htmlFor="debug-domain-id">
            <span>ROS Domain ID</span>
            <input aria-describedby={!validDomain ? "debug-domain-error" : "debug-domain-help"} aria-invalid={!validDomain} disabled={!connected || networkLocked} id="debug-domain-id" inputMode="numeric" max="232" min="0" step="1" type="number" value={domainId} onChange={(event) => setDomainId(event.target.value)} />
            <small id="debug-domain-help">{networkLocked ? "운영 런타임과 동일한 Domain ID를 사용합니다." : "상대 컴퓨터와 같은 값을 사용하세요. 허용 범위는 0–232입니다."}</small>
          </label>
          {!validDomain ? <p className="debug-field-error" id="debug-domain-error" role="alert"><XCircle size={15} aria-hidden="true" />0부터 232 사이의 정수를 입력해 주세요.</p> : null}
          {domainCollisionWarning ? <p className="debug-inline-warning"><AlertTriangle size={15} aria-hidden="true" />Linux 임시 포트와 겹칠 수 있는 범위입니다. 가능하면 0–101 또는 215–232를 사용하세요.</p> : null}
          {networkLocked ? <p className="debug-inline-warning"><ShieldAlert size={15} aria-hidden="true" />운영 런타임과 동일한 DDS 설정으로 잠겨 있습니다.</p> : null}
          {changeBlocked ? <p className="debug-inline-warning"><AlertTriangle size={15} aria-hidden="true" />수동 제어, 실행 중 Action, 연속 더미 발행을 모두 정지한 뒤 변경할 수 있습니다.</p> : null}
          {!network.restart_supported ? <p className="debug-inline-warning"><AlertTriangle size={15} aria-hidden="true" />현재 실행 방식에서는 자동 재시작을 사용할 수 없습니다.</p> : null}
          <button className="button button-primary full" disabled={!connected || networkLocked || !changed || !validDomain || changeBlocked || networkPending || network.restart_scheduled || !network.restart_supported} type="submit">
            {networkPending || network.restart_scheduled ? <LoaderCircle className="debug-spinner" size={16} aria-hidden="true" /> : <RefreshCw size={16} aria-hidden="true" />}
            {networkPending || network.restart_scheduled ? "적용 중" : "적용하고 재연결"}
          </button>
          {!changed && validDomain ? <p className="debug-network-note">현재 실행 중인 설정과 같습니다.</p> : null}
        </form>
        </div>

        <form className="debug-ping-form" onSubmit={(event) => void runPing(event)}>
        <div className="debug-network-subheading">
          <Radio size={18} aria-hidden="true" />
          <div><span>PARTNER REACHABILITY</span><h3>상대 컴퓨터 핑 테스트</h3></div>
        </div>
        <div className="debug-ping-controls">
          <label className="debug-field" htmlFor="debug-ping-target">
            <span>상대 컴퓨터 IPv4 주소</span>
            <input aria-describedby={pingError ? "debug-ping-error" : "debug-ping-help"} aria-invalid={Boolean(pingError)} autoComplete="off" disabled={!connected} id="debug-ping-target" inputMode="decimal" placeholder="예: 10.125.185.91" value={pingTarget} onBlur={() => pingTarget.trim() && setPingError(isValidIpv4(pingTarget) ? "" : "점으로 구분된 IPv4 주소를 입력해 주세요. 예: 10.125.185.91")} onChange={(event) => { setPingTarget(event.target.value); setPingError(""); }} />
            <small id="debug-ping-help">ICMP Echo를 3회 전송하며 DDS 설정은 변경하지 않습니다.</small>
          </label>
          <button className="button button-secondary" disabled={!connected || pingPending || !pingTarget.trim()} type="submit">
            {pingPending ? <LoaderCircle className="debug-spinner" size={16} aria-hidden="true" /> : <Send size={16} aria-hidden="true" />}
            {pingPending ? "응답 대기 중" : "핑 보내기"}
          </button>
        </div>
        {pingError ? <p className="debug-field-error" id="debug-ping-error" role="alert"><XCircle size={15} aria-hidden="true" />{pingError}</p> : null}
        <div className={"debug-ping-result " + (pingResult?.reachable ? "ok" : pingResult ? "error" : "idle")} aria-live="polite" aria-busy={pingPending}>
          {pingPending ? <LoaderCircle className="debug-spinner" size={22} aria-hidden="true" /> : pingResult?.reachable ? <CheckCircle2 size={22} aria-hidden="true" /> : pingResult ? <XCircle size={22} aria-hidden="true" /> : <Radio size={22} aria-hidden="true" />}
          <div>
            <strong>{pingPending ? "상대 컴퓨터 응답을 기다리고 있습니다" : pingResult?.reachable ? `${pingResult.target_ip} 연결 가능` : pingResult ? `${pingResult.target_ip} 응답 없음` : "상대 IP를 입력하면 LAN 도달성을 확인합니다"}</strong>
            <span>{pingResult ? `${pingResult.received}/${pingResult.sent} 응답 · 손실 ${pingResult.packet_loss_percent}%${pingResult.rtt_ms ? ` · 평균 ${pingResult.rtt_ms.avg.toFixed(2)} ms` : ""}` : "DDS discovery 전 단계의 기본 네트워크 검사입니다."}</span>
          </div>
        </div>
        </form>
      </div>
    </details>
  );
}

function InputStateRow({ row }: { row: DebugInputStatus }) {
  return (
    <tr data-slot="debug-input-row">
      <td data-label="토픽">
        <strong>{row.name}</strong>
        <code>{row.source_topic || row.topic}</code>
        {row.source_topic ? <small>상태 모니터 {row.topic}</small> : null}
      </td>
      <td data-label="상태">
        <StatusBadge state={row.state} />
        <small>{row.publisher_count} publisher</small>
      </td>
      <td data-label="실측 Hz">
        <strong>{formatHz(row.measured_hz)}</strong>
        <small>{row.expected_hz > 0 ? `기준 ${formatHz(row.expected_hz)}` : "측정 전용"}</small>
      </td>
      <td data-label="Freshness">
        <strong>{formatAge(row.last_age_sec)}</strong>
        <small>{formatBandwidth(row.bandwidth_bytes_sec)}</small>
      </td>
      <td data-label="타입·QoS">
        <code>{row.source_type || row.actual_types.join(", ") || row.expected_type}</code>
        <small>{row.source_qos || row.qos_profiles.join(", ") || row.expected_qos}</small>
      </td>
      <td data-label="최근 값">
        <span className="debug-sample" title={row.last_sample}>{row.last_sample || "아직 메시지가 없습니다."}</span>
      </td>
    </tr>
  );
}

function ConnectionPanel({
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
  const readyInputs = status.inputs.filter((row) => row.state === "READY").length;
  const readyEndpoints = status.endpoints.filter((row) => row.ready).length;
  const enabledOutputs = status.outputs.filter((row) => row.enabled).length;
  const outputSubscribers = status.outputs.reduce((total, row) => total + row.subscriber_count, 0);
  const network = status.runtime.network;
  const wiredSelected = wiredInterfaceSelected(network);
  const networkReady = Boolean(network.primary_ipv4) && network.link_up !== false;
  return (
    <section className="debug-panel-stack" data-slot="debug-connection-panel">
      <dl className="debug-health-strip" aria-label="통합 연결 요약">
        <div className={readyInputs === status.inputs.length && status.inputs.length ? "ok" : "warn"}>
          <dt>입력 토픽</dt><dd><strong>{readyInputs}</strong><span>/{status.inputs.length} 정상</span><small>실시간 메시지 수신</small></dd>
        </div>
        <div className={readyEndpoints === status.endpoints.length && status.endpoints.length ? "ok" : "warn"}>
          <dt>Action·Service</dt><dd><strong>{readyEndpoints}</strong><span>/{status.endpoints.length} 발견</span><small>외부 로봇 종단</small></dd>
        </div>
        <div className={outputSubscribers > 0 ? "ok" : enabledOutputs > 0 ? "warn" : "idle"}>
          <dt>출력 토픽</dt><dd><strong>{enabledOutputs}</strong><span> 발행 · {outputSubscribers} 구독</span><small>총 {status.outputs.length}개 계약</small></dd>
        </div>
        <div className={networkReady ? "ok debug-health-network" : "warn debug-health-network"}>
          <dt>{wiredSelected ? "유선 네트워크" : "로컬 네트워크"}</dt><dd><strong>{localAddressLabel(network)}</strong><small>{network.primary_interface || "interface 대기"} · {localLinkLabel(network)} · D{status.runtime.ros_domain_id} · {status.runtime.discovery_range}</small></dd>
        </div>
      </dl>

      <div className="debug-observability-grid">
        <div className="debug-observability-main">
          <article className="debug-section-card debug-input-card">
            <div className="debug-section-heading">
              <div><p>INPUT MONITOR</p><h2>외부 입력 토픽</h2><span>Publisher·Hz·최신성·QoS를 실측합니다.</span></div>
              <StatusBadge state={readyInputs === status.inputs.length && status.inputs.length ? "READY" : "WAITING"} label={`${readyInputs}/${status.inputs.length} 정상`} />
            </div>
            {status.inputs.length ? (
              <div className="debug-table-scroll debug-input-table-scroll">
                <table className="debug-table debug-input-table">
                  <caption className="sr-only">외부 입력 토픽의 연결, 발행률, 최신성, 타입 및 QoS 상태</caption>
                  <thead><tr><th>토픽</th><th>상태</th><th>실측 Hz</th><th>Freshness</th><th>타입·QoS</th><th>최근 값</th></tr></thead>
                  <tbody>{status.inputs.map((row) => <InputStateRow key={row.topic} row={row} />)}</tbody>
                </table>
              </div>
            ) : (
              <div className="debug-empty-state"><Radio size={28} aria-hidden="true" /><p>설정된 입력 토픽이 없습니다.</p></div>
            )}
          </article>

          <article className="debug-section-card debug-output-overview-card">
            <div className="debug-section-heading">
              <div><p>PUBLIC TOPIC OVERVIEW</p><h2>공개 출력 토픽</h2><span>상세 발행 제어는 ‘출력 검증’ 탭에서 수행합니다.</span></div>
              <StatusBadge state={enabledOutputs > 0 ? "READY" : "WAITING"} label={`${enabledOutputs}/${status.outputs.length} 발행`} />
            </div>
            <div className="debug-output-overview-grid" role="list" aria-label="공개 출력 토픽 요약">
              {status.outputs.map((row) => {
                const conflict = row.conflicting_publishers.length > 0;
                return (
                  <div className={conflict ? "error" : row.enabled ? "ok" : "idle"} key={row.topic} role="listitem">
                    <span>{conflict ? "충돌" : row.enabled ? "발행" : "정지"}</span>
                    <div><strong>{row.topic.split("/").slice(-1)[0]}</strong><code>{row.topic}</code></div>
                    <small>{formatHz(row.measured_hz)} · {row.subscriber_count} sub</small>
                  </div>
                );
              })}
            </div>
          </article>
        </div>

        <aside className="debug-observability-rail" aria-label="ROS 네트워크 상태">
          <NetworkPanel connected={connected} notify={notify} runCommand={runCommand} status={status} />
        </aside>
      </div>
    </section>
  );
}

function ManualPanel({
  status,
  connected,
  runCommand,
  coexistenceConfirmed,
  setCoexistenceConfirmed,
  manualControlPending,
  scenario,
}: {
  status: IntegrationDebugStatus;
  connected: boolean;
  runCommand: RunDebugCommand;
  coexistenceConfirmed: boolean;
  setCoexistenceConfirmed: (confirmed: boolean) => void;
  manualControlPending: boolean;
  scenario: "tool_voice" | "retractor";
}) {
  const [instrument, setInstrument] = useState(DEFAULT_TOOL_HANDOVER_OPTION.instrumentId);
  const [instance, setInstance] = useState(DEFAULT_TOOL_HANDOVER_OPTION.instanceIds[0]);
  const [transition, setTransition] = useState("tray:surgeon");
  const [retractionTargetSide, setRetractionTargetSide] = useState<"left" | "right">("left");
  const [pending, setPending] = useState("");
  const [recoveryConfirmed, setRecoveryConfirmed] = useState(false);
  const busy = !status.action.terminal;
  const recoveryRequired = status.action.recovery_required === true;
  const armed = status.session.armed;
  const statusFresh = connected;
  const blockedNodes = status.runtime.blocked_nodes ?? [];
  const detectedPlannerNodes = status.runtime.detected_planner_nodes ?? [];
  const manualControlAvailable = statusFresh && status.runtime.manual_control_available === true;
  const operationalRuntimeStopped = statusFresh && status.runtime.operational_runtime_stopped === true;
  const runtimeNetworkLocked = statusFresh && status.runtime.network.locked_to_runtime === true;
  const operationalState = status.runtime.operational_state?.trim() || "UNKNOWN";
  const operationalStateAge = typeof status.runtime.operational_state_age_sec === "number"
    ? formatAge(status.runtime.operational_state_age_sec)
    : "수신 전";
  const coexistenceAllowed = status.runtime.planner_coexistence_allowed === true;
  const coexistenceRequired = blockedNodes.length > 0 && coexistenceAllowed;
  const coexistenceActive = statusFresh && (status.session.planner_coexistence_active === true
    || Boolean(armed && status.session.acknowledged_blocked_nodes?.length));
  const selectedTool = TOOL_HANDOVER_OPTIONS.find((tool) => tool.instrumentId === instrument)
    ?? DEFAULT_TOOL_HANDOVER_OPTION;
  const endpointReady = (name: string) => status.endpoints.find((row) => row.name === name)?.ready ?? false;

  useEffect(() => {
    setRecoveryConfirmed(false);
  }, [status.action.command_id, recoveryRequired]);

  async function invoke(operation: string, payload: Record<string, unknown> = {}) {
    setPending(operation);
    try {
      return await runCommand(operation, payload);
    } finally {
      setPending("");
    }
  }

  async function submitTool(event: FormEvent) {
    event.preventDefault();
    const [source, target] = transition.split(":");
    await invoke("tool_handover", {
      instrument_id: instrument,
      instrument_instance_id: instance,
      source_location: source,
      target_location: target,
    });
  }

  function selectInstrument(instrumentId: string) {
    const nextTool = TOOL_HANDOVER_OPTIONS.find((tool) => tool.instrumentId === instrumentId);
    if (!nextTool) return;
    setInstrument(nextTool.instrumentId);
    setInstance(nextTool.instanceIds[0]);
  }

  async function submitRetractionCommand(command: RetractionCommand) {
    const adjustment = command === "adjust_retraction";
    await invoke("retraction_command", {
      command,
      target_side: adjustment ? retractionTargetSide : "none",
      distance_m: adjustment ? 0.05 : 0,
    });
  }

  async function configureRetractionVoice(enabled: boolean) {
    await invoke("configure_retraction_voice", { enabled });
  }

  async function recoverCommandClient() {
    const response = await invoke("recover_command_client", {
      expected_command_id: status.action.command_id,
      remote_motion_stopped_confirmed: recoveryConfirmed,
    });
    if (response?.accepted) setRecoveryConfirmed(false);
  }

  async function forceRetractionIdle() {
    await invoke("force_retraction_idle", {
      remote_motion_stopped_confirmed: true,
    });
  }

  const motionDisabled = !connected || !armed || busy || Boolean(pending);
  const retractionService = status.endpoints.find((row) => row.name === "retraction_service");
  const retractionServiceReady = retractionService?.ready ?? false;
  const retractionServiceEndpoint = retractionService?.endpoint ?? "/surgery/retraction/command";
  const retractionVoice = status.voice.retraction;
  const retractionVoiceMode = retractionVoice?.mode === "voice_and_buttons"
    ? "voice_and_buttons"
    : "buttons_only";
  const retractionInFlight = retractionVoice?.in_flight === true
    || (!status.action.terminal && status.action.route === "retraction_service");
  const retractionVoiceServiceReady = retractionVoice?.service_ready ?? retractionServiceReady;
  const retractionInternalState = retractionVoice?.internal_state ?? "idle";
  const retractionAllowedCommands = retractionVoice?.allowed_commands ?? [];
  const retractionInterpretation = retractionVoice?.last_interpretation;
  const retractionInterpreterPending = retractionVoice?.interpreter_pending === true;
  const retractionVoiceToggleBusy = pending === "configure_retraction_voice";
  const retractionVoiceEnableDisabled = !connected || !armed || retractionInFlight || retractionInterpreterPending || Boolean(pending);
  const retractionVoiceDisableDisabled = !connected || retractionVoiceToggleBusy || (Boolean(pending) && retractionVoiceMode === "buttons_only");
  const retractionDisabled = motionDisabled || retractionInFlight || retractionInterpreterPending || !retractionVoiceServiceReady;
  const retractionCommandDisabled = (command: RetractionCommand) =>
    retractionDisabled || !retractionAllowedCommands.includes(command);
  const forceIdleDisabled = !connected
    || busy
    || recoveryRequired
    || manualControlPending
    || Boolean(pending)
    || retractionInternalState === "idle";
  const forceIdleBlockedReason = retractionInternalState === "idle"
    ? "idle"
    : recoveryRequired
      ? "recovery"
      : busy
        ? "busy"
        : forceIdleDisabled
          ? "unavailable"
          : "";
  const admissionOnly = status.action.response_semantics === "admission";
  return (
    <section className="debug-panel-stack" data-slot={scenario === "retractor" ? "debug-retractor-scenario" : "debug-tool-scenario-controls"}>
      {scenario === "retractor" ? <Suspense fallback={<div className="debug-section-card debug-vlm-skeleton" role="status"><span /><span /><span /><p className="sr-only">리트랙터 통합 경로를 준비하고 있습니다.</p></div>}><DebugIntegrationPipeline kind="retractor" status={status} /></Suspense> : null}
      <article
        className={`debug-coexistence-card ${operationalRuntimeStopped ? "active" : "warning"}`}
        id="debug-operational-interlock"
        role="status"
      >
        <span className="debug-coexistence-icon">
          {operationalRuntimeStopped
            ? <CheckCircle2 size={20} aria-hidden="true" />
            : <ShieldAlert size={20} aria-hidden="true" />}
        </span>
        <div className="debug-coexistence-copy">
          <strong>{operationalRuntimeStopped
            ? runtimeNetworkLocked ? DEBUG_OPERATIONAL_STOPPED_LABEL : DEBUG_STANDALONE_LABEL
            : statusFresh ? DEBUG_OPERATIONAL_UNKNOWN_LABEL : DEBUG_OPERATIONAL_STALE_LABEL}</strong>
          <span>
            {runtimeNetworkLocked
              ? `/simulation/state ${operationalState} · ${operationalStateAge}${operationalRuntimeStopped
                ? " · 최신 안전 정지 상태가 확인되었습니다."
                : " · 최신 안전 정지 상태가 확인될 때까지 모든 새 수동 명령을 차단합니다."}`
              : operationalRuntimeStopped
                ? "운영 런타임과 분리된 Standalone Debug에서 차단 대상 플래너 노드가 탐색되지 않았습니다."
                : "Standalone Debug에서 차단 대상 플래너 노드가 탐색되어 모든 새 수동 명령을 차단합니다."}
          </span>
          <span>수동 제어: {manualAvailabilityLabel(status)}{operationalRuntimeStopped && !manualControlAvailable ? " · Fault 또는 진행 중 명령 등 남은 안전 조건을 확인하세요." : ""}</span>
          {detectedPlannerNodes.length ? (
            <div className="debug-coexistence-nodes" aria-label="탐색된 운영 플래너 노드">
              {detectedPlannerNodes.map((node) => <code key={node}>{node}</code>)}
            </div>
          ) : null}
        </div>
        <div className="debug-coexistence-status">
          {manualControlAvailable
            ? <CheckCircle2 size={17} aria-hidden="true" />
            : <ShieldAlert size={17} aria-hidden="true" />}
          <span>{manualAvailabilityLabel(status)}</span>
        </div>
      </article>

      {coexistenceRequired ? (
        <article className={"debug-coexistence-card " + (coexistenceActive ? "active" : "warning")} id="debug-coexistence-description" role="status">
          <span className="debug-coexistence-icon">
            {coexistenceActive ? <CheckCircle2 size={20} aria-hidden="true" /> : <AlertTriangle size={20} aria-hidden="true" />}
          </span>
          <div className="debug-coexistence-copy">
            <strong>{coexistenceActive ? `Domain ${status.runtime.ros_domain_id} 플래너 공존 승인됨` : `Domain ${status.runtime.ros_domain_id}에서 전체 플래너가 발견됐습니다`}</strong>
            <span>{coexistenceActive ? "발견된 노드 목록이 달라지면 수동 제어와 음성 즉시 실행을 자동 해제합니다." : "상대 플래너의 자동 명령을 중지한 경우에만 이번 Debug 세션에서 공존을 승인하세요."}</span>
            <div className="debug-coexistence-nodes" aria-label="발견된 전체 플래너 노드">
              {blockedNodes.map((node) => <code key={node}>{node}</code>)}
            </div>
          </div>
          {coexistenceActive ? (
            <div className="debug-coexistence-status"><CheckCircle2 size={17} aria-hidden="true" /><span>현재 세션에서만 승인됨</span></div>
          ) : coexistenceAllowed ? (
            <label className="debug-coexistence-confirmation">
              <input id="debug-coexistence-checkbox" checked={coexistenceConfirmed} disabled={!connected || busy || Boolean(pending) || manualControlPending} onChange={(event) => setCoexistenceConfirmed(event.target.checked)} type="checkbox" />
              <span>상대 플래너의 자동 명령 실행이 중지된 것을 확인했습니다.</span>
            </label>
          ) : (
            <p className="debug-coexistence-unavailable">이 실행에서는 플래너 공존 승인이 비활성화되어 있습니다.</p>
          )}
        </article>
      ) : null}

      <div className="debug-control-grid debug-manual-grid single-scenario">
        {scenario === "tool_voice" ? (
        <form className="debug-section-card debug-control-card" onSubmit={(event) => void submitTool(event)}>
          <div className="debug-section-heading"><div><p>ACTION</p><h2>도구 전달</h2></div><StatusBadge state={endpointReady("tool_handover") ? "READY" : "WAITING"} label={endpointReady("tool_handover") ? "서버 발견" : "서버 대기"} /></div>
          <label className="debug-field" htmlFor="debug-handover-instrument">
            <span>실제 도구명</span>
            <select
              aria-describedby="debug-handover-instrument-help"
              id="debug-handover-instrument"
              value={instrument}
              onChange={(event) => selectInstrument(event.target.value)}
            >
              {TOOL_HANDOVER_OPTIONS.map((tool) => (
                <option key={tool.catalogId} value={tool.instrumentId}>
                  {tool.catalogId} · {tool.instrumentId}
                </option>
              ))}
            </select>
            <small id="debug-handover-instrument-help">
              실제 로봇에 등록된 3개 프로파일만 표시합니다. Action instrument_id에는 영문명이 전송됩니다.
            </small>
          </label>
          <label className="debug-field" htmlFor="debug-handover-instance">
            <span>인스턴스 ID</span>
            <select
              aria-describedby="debug-handover-instance-help"
              id="debug-handover-instance"
              value={instance}
              onChange={(event) => setInstance(event.target.value)}
            >
              {selectedTool.instanceIds.map((instanceId) => (
                <option key={instanceId} value={instanceId}>{instanceId}</option>
              ))}
            </select>
            <small id="debug-handover-instance-help">
              {selectedTool.catalogId} 재고 {selectedTool.instanceIds.length}개 · 선택값을 instrument_instance_id로 전송합니다.
            </small>
          </label>
          <label className="debug-field"><span>전달 경로</span><select value={transition} onChange={(event) => setTransition(event.target.value)}>
            <option value="tray:robot">tray → robot</option><option value="tray:surgeon">tray → surgeon</option><option value="robot:surgeon">robot → surgeon</option><option value="robot:tray">robot → tray</option><option value="mayo:robot">mayo → robot</option><option value="mayo:tray">mayo → tray</option>
          </select></label>
          <button className="button button-primary full" disabled={motionDisabled || !endpointReady("tool_handover") || !instrument.trim()} type="submit"><Send size={16} aria-hidden="true" />도구 전달 요청</button>
          {!endpointReady("tool_handover") ? (
            <p className="debug-inline-warning">
              {armed
                ? "수동 제어는 활성화되었습니다. Action 서버가 발견될 때까지 도구 전달 요청은 잠깁니다."
                : "수동 제어와 Action 서버가 모두 준비된 뒤 도구 전달 요청을 보낼 수 있습니다."}
            </p>
          ) : null}
        </form>
        ) : null}

        {scenario === "retractor" ? (
        <article aria-busy={retractionInFlight || retractionInterpreterPending || retractionVoiceToggleBusy} className="debug-section-card debug-control-card">
          <div className="debug-section-heading"><div><p>SERVICE</p><h2>리트랙터 명령</h2><span>단일 Service · {retractionServiceEndpoint}</span></div><StatusBadge state={retractionServiceReady ? "READY" : "WAITING"} label={retractionServiceReady ? "서비스 발견" : "서비스 대기"} /></div>
          <p className="debug-inline-warning">응답은 요청 접수 여부만 뜻합니다. 실제 물리 동작의 진행·완료·상태는 이 화면에서 판정하지 않습니다.</p>
          <div aria-describedby="debug-retraction-voice-ownership" className="debug-segmented-control" aria-label="리트랙터 음성 처리 모드" data-slot="debug-retraction-voice-mode" role="group">
            <button aria-pressed={retractionVoiceMode === "buttons_only"} className={retractionVoiceMode === "buttons_only" ? "active" : ""} disabled={retractionVoiceDisableDisabled} onClick={() => void configureRetractionVoice(false)} type="button">버튼만<small>음성 해석·전송 안 함</small></button>
            <button aria-pressed={retractionVoiceMode === "voice_and_buttons"} className={retractionVoiceMode === "voice_and_buttons" ? "active" : ""} disabled={retractionVoiceEnableDisabled} onClick={() => void configureRetractionVoice(true)} type="button">음성 + 버튼<small>USB ASR 확정 문장만</small></button>
          </div>
          <div className="debug-voice-ownership-note" data-slot="debug-retraction-voice-ownership" id="debug-retraction-voice-ownership" role="note">
            <Mic size={17} aria-hidden="true" />
            <div><strong>마이크 캡처는 STT 입력·USB 캡처 기능 하나만 사용합니다</strong><span>이 모드는 그 기능이 발행한 확정 문장의 해석·전송 게이트만 바꾸며, 별도 마이크나 ASR 세션을 시작·중지하지 않습니다.</span></div>
          </div>
          <AsrLiveMonitor asr={status.asr} dataSlot="debug-retraction-asr-live" />
          <div aria-atomic="true" aria-live="polite" className="debug-parse-preview" data-slot="debug-retraction-voice-status">
            <span>음성 모드</span><StatusBadge state={retractionVoiceMode === "voice_and_buttons" ? "READY" : "WAITING"} label={retractionVoiceMode === "voice_and_buttons" ? "음성 + 버튼" : "버튼만"} />
            <span>Debug 내부 상태</span><StatusBadge state={retractionInternalState === "unknown" ? "ERROR" : retractionInternalState === "idle" ? "WAITING" : "READY"} label={retractionInternalStateLabel(retractionInternalState)} />
            <span>Service</span><StatusBadge state={retractionInFlight ? "SUBMITTING" : retractionVoiceServiceReady ? "READY" : "WAITING"} label={retractionInFlight ? "접수 응답 대기" : retractionVoiceServiceReady ? "서비스 발견" : "서비스 대기"} />
            <span>음성 해석</span><StatusBadge state={retractionInterpreterPending ? "SUBMITTING" : "READY"} label={retractionInterpreterPending ? "Text VLM 해석 중" : retractionVoice?.interpreter_mode === "vlm_with_fallback" ? "Text VLM + 폴백" : "결정론"} />
            <span>현재 허용 명령</span><strong>{retractionAllowedCommandsLabel(retractionAllowedCommands)}</strong>
            <span>최근 확정 문장</span><strong>{retractionInterpretation?.transcript || "수신 전"}</strong>
            <span>마지막 해석</span><strong>{retractionInterpretationLabel(retractionInterpretation)}</strong>
            <span>해석기</span><strong>{retractionInterpreterSourceLabel(retractionInterpretation)}</strong>
            <span>해석 상세</span><div className="debug-provenance-detail"><strong>{retractionInterpreterDetailLabel(retractionInterpretation?.detail)}</strong>{retractionInterpretation?.detail ? <code>{retractionInterpretation.detail}</code> : null}</div>
            <span>거부 이유</span><strong>{retractionVoice?.last_rejection_reason || "없음"}</strong>
          </div>
          <Suspense fallback={<div className="debug-state-message empty" role="status"><LoaderCircle className="debug-spinner" size={18} aria-hidden="true" /><div><strong>상태 초기화 제어 준비 중</strong><span>Debug 로컬 제어를 불러오고 있습니다.</span></div></div>}>
            <ForceRetractionIdleControl
              disabled={forceIdleDisabled}
              blockedReason={forceIdleBlockedReason}
              internalState={retractionInternalState}
              internalStateLabel={retractionInternalStateLabel(retractionInternalState)}
              onReset={() => void forceRetractionIdle()}
              pending={pending === "force_retraction_idle"}
            />
          </Suspense>
          <div className="debug-segmented-control" aria-label="리트랙션 조정 대상" role="group">
            <button aria-pressed={retractionTargetSide === "left"} className={retractionTargetSide === "left" ? "active" : ""} onClick={() => setRetractionTargetSide("left")} type="button">왼쪽<small>left</small></button>
            <button aria-pressed={retractionTargetSide === "right"} className={retractionTargetSide === "right" ? "active" : ""} onClick={() => setRetractionTargetSide("right")} type="button">오른쪽<small>right</small></button>
          </div>
          <div className="debug-inline-actions" aria-label="리트랙터 Service 명령" role="group">
            <button className="button button-secondary" disabled={retractionCommandDisabled("start_direct_teach")} onClick={() => void submitRetractionCommand("start_direct_teach")} type="button"><Play size={16} aria-hidden="true" />직접 교시 시작</button>
            <button className="button button-secondary" disabled={retractionCommandDisabled("finish_direct_teach")} onClick={() => void submitRetractionCommand("finish_direct_teach")} type="button"><CircleStop size={16} aria-hidden="true" />직접 교시 종료</button>
            <button className="button button-primary" disabled={retractionCommandDisabled("start_retraction")} onClick={() => void submitRetractionCommand("start_retraction")} type="button"><Play size={16} aria-hidden="true" />Retraction 시작</button>
            <button className="button button-primary" disabled={retractionCommandDisabled("adjust_retraction")} onClick={() => void submitRetractionCommand("adjust_retraction")} type="button">{retractionTargetSide === "left" ? "왼쪽" : "오른쪽"} 5 cm 더</button>
            <button className="button button-secondary" disabled={retractionCommandDisabled("change_tool")} onClick={() => void submitRetractionCommand("change_tool")} type="button"><RefreshCw size={16} aria-hidden="true" />Tool change</button>
            <button className="button button-secondary" disabled={retractionCommandDisabled("stop_retraction")} onClick={() => void submitRetractionCommand("stop_retraction")} type="button"><CircleStop size={16} aria-hidden="true" />Retraction 종료</button>
          </div>
          <p className="debug-inline-warning">방향·축·양측 조정과 arm_id·target_tool_id는 이 Service 인터페이스에 없으므로 Debug Mode에서 전송하지 않습니다.</p>
          {retractionInterpreterPending ? <p className="debug-inline-warning">확정 문장을 Text VLM이 해석하고 있습니다. 결과는 원문 근거와 현재 Debug 상태를 다시 통과해야 Service 요청이 됩니다.</p> : null}
          {retractionInFlight ? <p className="debug-inline-warning">리트랙터 Service의 요청 접수 응답을 기다리는 동안 새 버튼·음성 명령은 전송하지 않습니다.</p> : null}
          {retractionVoiceServiceReady && armed && !retractionInFlight && !retractionAllowedCommands.length ? <p className="debug-inline-warning">현재 Debug 내부 상태에서는 새 리트랙터 요청을 만들 수 없습니다. 상대 Service가 수락한 요청만 이 상태를 갱신하며, 물리 상태를 뜻하지 않습니다.</p> : null}
          {!retractionServiceReady ? (
            <p className="debug-inline-warning">
              {armed
                ? "수동 제어는 활성화되었습니다. Service 서버가 발견될 때까지 리트랙터 버튼 명령은 잠깁니다. 음성 + 버튼 모드는 설정할 수 있지만 확정 문장은 전송되지 않습니다."
                : "수동 제어와 Service 서버가 모두 준비된 뒤 리트랙터 명령을 보낼 수 있습니다."}
            </p>
          ) : null}
        </article>
        ) : null}

        <div className="debug-manual-side">
          <article className="debug-section-card debug-action-card" aria-live="polite">
            <div className="debug-section-heading"><div><p>{admissionOnly ? "SERVICE RESPONSE" : "ACTION FEEDBACK"}</p><h2>{recoveryRequired ? "원격 상태 확인 필요" : admissionOnly ? "요청 접수 결과" : "실행 상태"}</h2></div>{recoveryRequired ? <ShieldAlert size={19} aria-hidden="true" /> : <Activity size={19} aria-hidden="true" />}</div>
            <div className="debug-action-summary"><StatusBadge state={status.action.state} /><strong>{status.action.command || status.action.route || "대기"}</strong><code>{status.action.command_id || "활성 명령 없음"}</code></div>
            {admissionOnly ? (
              <>
                <div className="debug-action-meta"><span>접수: {status.action.state === "failed" ? "미확정" : status.action.request_accepted === true ? "수락" : status.action.state === "rejected" ? "거부" : status.action.request_accepted === false ? "미확정" : "응답 대기"}</span><span>결과 코드: {status.action.result_code ?? "—"}</span><span>{(status.action.elapsed_sec ?? 0).toFixed(1)} s</span></div>
                <p className="debug-inline-warning">{status.action.response_message ? `${status.action.response_message} · ` : ""}이 Service 응답은 요청 접수만 나타내며 로봇의 물리 실행·완료 상태는 표시하지 않습니다.</p>
              </>
            ) : (
              <>
                <div className="debug-progress-track" role="progressbar" aria-label="Action 진행률" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(status.action.progress * 100)} aria-valuetext={`${Math.round(status.action.progress * 100)}%, ${status.action.state}`}><span style={{ width: `${Math.round(status.action.progress * 100)}%` }} /></div>
                <div className="debug-action-meta"><span>{Math.round(status.action.progress * 100)}%</span><span>{(status.action.elapsed_sec ?? 0).toFixed(1)} s</span><span>{status.action.reason_code || "feedback 대기"}</span></div>
              </>
            )}
            {recoveryRequired ? (
              <div className="debug-action-recovery" data-slot="debug-action-recovery" id="debug-action-recovery" role="alert" tabIndex={-1}>
                <div className="debug-action-recovery-copy">
                  <AlertTriangle size={18} aria-hidden="true" />
                  <div>
                    <strong>로컬 상태만 임의로 초기화하면 안 됩니다</strong>
                    <p id="debug-action-recovery-description">{actionRecoveryExplanation(status.action.reason_code)}</p>
                  </div>
                </div>
                <dl className="debug-action-recovery-facts">
                  <div><dt>서버 탐색</dt><dd>{status.action.server_ready ? "발견됨" : "탐색 안 됨"}</dd></div>
                  <div><dt>마지막 갱신</dt><dd>{formatAge(status.action.last_update_age_sec ?? null)}</dd></div>
                </dl>
                <label className="debug-action-recovery-confirmation">
                  <input aria-describedby="debug-action-recovery-description" checked={recoveryConfirmed} disabled={!connected || Boolean(pending)} onChange={(event) => setRecoveryConfirmed(event.target.checked)} type="checkbox" />
                  <span>{admissionOnly ? "상대측에서 이 Command ID의 요청 접수 여부와 로봇 상태를 직접 확인했습니다." : "상대 로봇이 정지했거나, 상대측에서 이 Command ID의 종료 상태를 직접 확인했습니다."}</span>
                </label>
                <div className="debug-action-recovery-actions">
                  {!admissionOnly && status.action.cancel_available && status.action.server_ready ? (
                    <button className="button button-secondary" disabled={!connected || Boolean(pending)} onClick={() => void invoke("cancel_active")} type="button"><Square size={15} aria-hidden="true" />Cancel 재시도</button>
                  ) : null}
                  <button className="button button-primary" disabled={!connected || !recoveryConfirmed || Boolean(pending)} onClick={() => void recoverCommandClient()} type="button"><RotateCcw size={15} aria-hidden="true" />확인 후 클라이언트 복구</button>
                </div>
              </div>
            ) : !admissionOnly && busy && status.action.cancel_available ? <button className="button button-secondary full" disabled={!connected || Boolean(pending)} onClick={() => void invoke("cancel_active")} type="button"><Square size={15} aria-hidden="true" />현재 Action 취소</button> : null}
          </article>
        </div>
      </div>
    </section>
  );
}

function OutputRow({
  row,
  rate,
  setRate,
  connected,
  armed,
  runCommand,
}: {
  row: DebugOutputStatus;
  rate: number;
  setRate: (value: number) => void;
  connected: boolean;
  armed: boolean;
  runCommand: RunDebugCommand;
}) {
  const [pending, setPending] = useState(false);
  const validRate = Number.isFinite(rate) && rate >= 0.1 && rate <= 10;
  async function invoke(operation: string, payload: Record<string, unknown>) {
    setPending(true);
    try { await runCommand(operation, payload); } finally { setPending(false); }
  }
  return (
    <tr data-slot="debug-output-row">
      <td><strong>{row.topic.split("/").slice(-1)[0]}</strong><code>{row.topic}</code><small>{row.type}</small></td>
      <td><StatusBadge state={row.conflicting_publishers.length ? "TYPE_MISMATCH" : row.enabled ? "READY" : "WAITING"} label={row.conflicting_publishers.length ? "충돌" : row.enabled ? "발행 중" : "정지"} /><small>{row.publish_count}회 · {row.last_age_sec === null ? row.publish_count > 0 ? "현재 정지" : "발행 전" : formatAge(row.last_age_sec)}</small></td>
      <td><label className="debug-rate-field"><input aria-label={`${row.topic} 발행 Hz`} aria-invalid={!validRate} min="0.1" max="10" step="0.1" type="number" value={rate} onChange={(event) => setRate(Number(event.target.value))} /><small>{validRate ? "Hz" : "0.1–10 Hz"}</small></label><span>{formatHz(row.measured_hz)}</span></td>
      <td><strong>{row.subscriber_count}</strong><small>{row.subscribers.join(", ") || "Subscriber 대기"}</small></td>
      <td><div className="debug-row-actions"><button className="button button-quiet" disabled={!connected || !armed || pending} onClick={() => void invoke("publish_once", { topic: row.topic })} type="button">1회 발행</button><button className={row.enabled ? "button button-secondary" : "button button-primary"} disabled={!connected || pending || (!row.enabled && (!armed || !validRate))} onClick={() => void invoke("configure_output", { topic: row.topic, enabled: !row.enabled, rate_hz: row.enabled && !validRate ? row.configured_hz : rate })} type="button">{row.enabled ? "정지" : "연속 발행"}</button></div></td>
    </tr>
  );
}

function OutputPanel({
  status,
  connected,
  runCommand,
}: {
  status: IntegrationDebugStatus;
  connected: boolean;
  runCommand: RunDebugCommand;
}) {
  const [rates, setRates] = useState<Record<string, number>>({});
  const enabledCount = status.outputs.filter((row) => row.enabled).length;
  const subscriberCount = status.outputs.reduce((total, row) => total + row.subscriber_count, 0);
  return (
    <section className="debug-panel-stack" data-slot="debug-output-panel">
      <article className="debug-section-card">
        <div className="debug-section-heading">
          <div><p>PUBLIC TOPICS</p><h2>더미 출력 검증</h2><span>모든 데이터는 UNKNOWN · DEBUG_DUMMY_DATA로 명시됩니다.</span></div>
          <div className="debug-heading-actions">
            <span className="debug-meta-pill">{enabledCount}/{status.outputs.length} 발행 · {subscriberCount} 구독</span>
            <button className="button button-secondary" disabled={!connected || !enabledCount} onClick={() => void runCommand("stop_outputs")} type="button"><CircleStop size={16} aria-hidden="true" />전체 정지</button>
          </div>
        </div>
        <div className="debug-info-banner"><AlertTriangle size={17} aria-hidden="true" /><p>Subscriber 수는 DDS discovery를 증명합니다. 상대 콜백 수신은 상대 기관의 echo 또는 로컬 로그로 별도 확인해야 합니다.</p></div>
        {!status.session.armed ? <p className="debug-inline-warning">운영 시나리오 정지 상태가 확인된 뒤 상단에서 수동 제어를 활성화해야 새로운 더미 토픽을 발행할 수 있습니다. 이미 발행 중인 출력의 정지는 항상 가능합니다.</p> : null}
        <div className="debug-table-scroll">
          <table className="debug-table debug-output-table">
            <caption className="sr-only">공개 출력 토픽의 발행 상태, 발행률, 구독자 및 수동 제어</caption>
            <thead><tr><th>출력 토픽</th><th>상태</th><th>발행률</th><th>Subscriber</th><th>제어</th></tr></thead>
            <tbody>{status.outputs.map((row) => <OutputRow armed={status.session.armed} connected={connected} key={row.topic} row={row} rate={rates[row.topic] ?? row.configured_hz} setRate={(value) => setRates((current) => ({ ...current, [row.topic]: value }))} runCommand={runCommand} />)}</tbody>
          </table>
        </div>
      </article>
    </section>
  );
}

function SttPanel({
  status,
  connected,
  runCommand,
}: {
  status: IntegrationDebugStatus;
  connected: boolean;
  runCommand: RunDebugCommand;
}) {
  const [sentence, setSentence] = useState("");
  const [sentencePending, setSentencePending] = useState(false);
  const preferredDevice = status.asr.device_id
    ?? status.asr.devices.find((device) => device.default)?.id
    ?? status.asr.devices[0]?.id;
  const [selectedDeviceId, setSelectedDeviceId] = useState(
    preferredDevice === undefined ? "default" : String(preferredDevice),
  );
  const [selectedEndpointId, setSelectedEndpointId] = useState<PuzzleAsrEndpointId>(
    status.asr.endpoint_id === "lan" ? "lan" : "cloud",
  );
  const [pendingAsrCommand, setPendingAsrCommand] = useState("");

  useEffect(() => {
    if (selectedDeviceId !== "default" && status.asr.devices.some((device) => String(device.id) === selectedDeviceId)) return;
    const nextDevice = status.asr.device_id
      ?? status.asr.devices.find((device) => device.default)?.id
      ?? status.asr.devices[0]?.id;
    if (nextDevice !== undefined) setSelectedDeviceId(String(nextDevice));
  }, [selectedDeviceId, status.asr.device_id, status.asr.devices]);

  async function sendSentence(value = sentence) {
    const normalized = value.trim();
    if (!normalized) return;
    setSentencePending(true);
    try {
      const response = await runCommand("publish_voice_command", { text: normalized });
      if (response.accepted) setSentence(normalized);
    } finally {
      setSentencePending(false);
    }
  }

  async function runAsrCommand(operation: string, payload: Record<string, unknown> = {}) {
    setPendingAsrCommand(operation);
    try {
      return await runCommand(operation, payload);
    } finally {
      setPendingAsrCommand("");
    }
  }

  async function startAsr() {
    await runAsrCommand("asr_start", {
      device_id: selectedDeviceId === "default" ? "default" : Number(selectedDeviceId),
      endpoint_id: selectedEndpointId,
    });
  }

  const asrActive = ["STARTING", "LISTENING", "STOPPING"].includes(status.asr.state);
  const asrStartable = ["STOPPED", "ERROR"].includes(status.asr.state);
  const operationalAsrOwned = status.runtime.network.locked_to_runtime === true;
  const selectedDevice = status.asr.devices.find((device) => String(device.id) === selectedDeviceId);
  const asrFinals = status.asr.finals ?? [];
  const recentFinals = [...asrFinals].reverse().slice(0, 8);
  const latestFinalLatency = recentFinals[0]?.response_latency_ms;
  return (
    <section className="debug-panel-stack" data-slot="debug-stt-panel">
        <div className="debug-stt-controls">
          <article aria-busy={Boolean(pendingAsrCommand) || ["STARTING", "STOPPING"].includes(status.asr.state)} className="debug-section-card debug-asr-card">
            <div className="debug-section-heading">
              <div><p>USB · PUZZLE ASR</p><h2>마이크 런타임</h2><span>브라우저 WebSpeech가 아닌 호스트 USB 입력을 시험합니다.</span></div>
              <div className="debug-asr-heading-actions">
                <div aria-label="Puzzle ASR route" className="debug-segmented-control debug-asr-endpoint-selector" data-slot="debug-asr-endpoint-selector" role="group">
                  <button aria-pressed={selectedEndpointId === "cloud"} className={selectedEndpointId === "cloud" ? "active" : ""} disabled={asrActive || operationalAsrOwned || Boolean(pendingAsrCommand)} onClick={() => setSelectedEndpointId("cloud")} type="button">클라우드<small>worker-02 · TLS</small></button>
                  <button aria-pressed={selectedEndpointId === "lan"} className={selectedEndpointId === "lan" ? "active" : ""} disabled={asrActive || operationalAsrOwned || Boolean(pendingAsrCommand)} onClick={() => setSelectedEndpointId("lan")} type="button">LAN<small>192.168.1.5:1196</small></button>
                </div>
                <StatusBadge state={status.asr.state} label={status.asr.state} />
              </div>
            </div>
            <div className="debug-voice-ownership-note" data-slot="debug-asr-sole-owner" role="note">
              <Mic size={17} aria-hidden="true" />
              <div><strong>이 기능이 Debug 마이크 캡처를 단독 소유합니다</strong><span>두 통합 시나리오는 이 ASR의 확정 문장을 재사용하며 두 번째 오디오 스트림을 열지 않습니다.</span></div>
            </div>
            <div className="debug-asr-form-grid debug-asr-form-grid-single">
              <label className="debug-field" htmlFor="debug-asr-device">
                <span>마이크 입력 장치</span>
                <select
                  aria-describedby="debug-asr-device-help"
                  disabled={asrActive || operationalAsrOwned}
                  id="debug-asr-device"
                  value={selectedDeviceId}
                  onChange={(event) => setSelectedDeviceId(event.target.value)}
                >
                  {!status.asr.devices.length ? <option value="default">사용 가능한 입력 장치 없음</option> : null}
                  {status.asr.devices.map((device) => (
                    <option key={device.id} value={device.id}>
                      {device.default ? "기본 · " : ""}{device.name}
                    </option>
                  ))}
                </select>
                <small id="debug-asr-device-help">{selectedDevice ? `${selectedDevice.input_channels} ch · ${selectedDevice.default_samplerate.toLocaleString()} Hz · Ubuntu 현재 입력` : status.asr.device_message || "Ubuntu 설정에서 입력 장치를 선택한 뒤 새로고침하세요."}</small>
              </label>
            </div>
            <div className="debug-inline-actions">
              <button className="button button-quiet" disabled={!connected || asrActive || Boolean(pendingAsrCommand)} onClick={() => void runAsrCommand("asr_refresh_devices")} type="button">
                {pendingAsrCommand === "asr_refresh_devices" ? <LoaderCircle className="debug-spinner" size={16} aria-hidden="true" /> : <RefreshCw size={16} aria-hidden="true" />}장치 새로고침
              </button>
              <button className="button button-primary" disabled={operationalAsrOwned || !connected || !status.session.armed || !status.asr.available || !status.asr.devices.length || !asrStartable || Boolean(pendingAsrCommand)} onClick={() => void startAsr()} type="button">
                {pendingAsrCommand === "asr_start" || status.asr.state === "STARTING" ? <LoaderCircle className="debug-spinner" size={16} aria-hidden="true" /> : <Mic size={16} aria-hidden="true" />}ASR 시작
              </button>
              <button className="button button-secondary" disabled={!connected || !asrActive || status.asr.state === "STOPPING" || Boolean(pendingAsrCommand)} onClick={() => void runAsrCommand("asr_stop")} type="button">
                {pendingAsrCommand === "asr_stop" || status.asr.state === "STOPPING" ? <LoaderCircle className="debug-spinner" size={16} aria-hidden="true" /> : <MicOff size={16} aria-hidden="true" />}ASR 중지
              </button>
            </div>
            {operationalAsrOwned ? <p className="debug-inline-warning">운영 통합 중에는 이 Debug ASR이 운영 preflight를 대신하지 않도록 캡처가 잠깁니다. 운영 화면의 ‘수술실 음성 입력’에서 USB 마이크를 선택하고 ASR을 시작하세요.</p> : null}
            {selectedEndpointId === "lan" ? <p className="debug-inline-warning">LAN은 평문 <code>ws://</code>입니다. 신뢰된 유선망에서만 전송하세요.</p> : null}
            {!status.session.armed ? <p className="debug-inline-warning">마이크를 열기 전에 화면 상단에서 수동 제어를 활성화하세요. 제어가 해제되면 ASR도 자동 중지됩니다.</p> : null}
            {status.asr.device_status === "NO_INPUT" ? (
              <p className="debug-inline-warning">현재 Ubuntu에 선택 가능한 마이크 입력이 없습니다. 마이크를 연결하거나 Ubuntu 소리 설정에서 입력을 선택한 뒤 장치를 새로고침하세요.</p>
            ) : null}
            {status.asr.dependency_error || status.asr.last_error ? (
              <p className="debug-field-error" role="alert"><XCircle size={15} aria-hidden="true" />{status.asr.dependency_error || status.asr.last_error}</p>
            ) : null}
            <AsrLiveMonitor asr={status.asr} dataSlot="debug-stt-asr-live" />
            <p aria-atomic="true" aria-live="polite" className="sr-only">ASR {displayState(status.asr.state)}. {recentFinals[0] ? `최근 확정 문장: ${recentFinals[0].text}, final 응답 지연 ${formatAsrLatency(latestFinalLatency)}` : "확정 문장 없음"}</p>
            <dl className="debug-runtime-facts">
              <div><dt>서버</dt><dd>{status.asr.connected ? "연결됨" : "미연결"}</dd></div>
              <div><dt>실행 시간</dt><dd>{status.asr.elapsed_sec.toFixed(1)} s</dd></div>
              <div><dt>실제 캡처</dt><dd>{status.asr.input_sample_rate.toLocaleString()} Hz · {status.asr.input_channels} ch</dd></div>
              <div><dt>캡처 블록 크기</dt><dd>{status.asr.input_block_frames.toLocaleString()} frames</dd></div>
              <div><dt>Wire 변환</dt><dd>{status.asr.resampling ? `${status.asr.sample_rate.toLocaleString()} Hz · ${status.asr.channels} ch 변환` : "변환 없음"}</dd></div>
              <div><dt>Wire 송신</dt><dd>{status.asr.sent_chunks.toLocaleString()} chunks</dd></div>
              <div><dt>ASR 응답</dt><dd>{status.asr.responses.toLocaleString()}</dd></div>
              <div><dt>캡처 횟수</dt><dd>{status.asr.blocks_captured.toLocaleString()}</dd></div>
              <div><dt>드롭</dt><dd>{(status.asr.input_dropped + status.asr.dropped_chunks).toLocaleString()}</dd></div>
            </dl>
            <div className="debug-parse-preview" data-slot="debug-stt-latest-final">
              <span>최근 확정 문장</span><strong>{recentFinals[0]?.text || "수신 전 · 마이크를 시작하거나 수동 문장을 발행하세요."}</strong>
              <span>응답 참고 간격</span><strong>{formatAsrLatency(latestFinalLatency)}</strong>
              <span>누적 확정</span><strong>{asrFinals.length.toLocaleString()}건 · 전체 목록은 ‘관측 로그’에서 확인</strong>
            </div>
            <code className="debug-topic-code">{status.asr.topic} · std_msgs/msg/String · {status.asr.sample_rate.toLocaleString()} Hz / {status.asr.sample_width_bits} bit</code>
          </article>

          <article className="debug-section-card">
            <div className="debug-section-heading"><div><p>MANUAL SENTENCE</p><h2>수동 문장 입력</h2><span>ASR 없이 동일한 확정 문장 토픽을 재현합니다.</span></div><Headphones size={19} aria-hidden="true" /></div>
            <label className="debug-field" htmlFor="debug-manual-sentence"><span>집도의 완성 문장</span><textarea id="debug-manual-sentence" rows={3} value={sentence} onChange={(event) => setSentence(event.target.value)} placeholder="예: 켈리 주세요" /></label>
            <div className="debug-inline-actions">
              <button className="button button-primary" disabled={!connected || !status.session.armed || !sentence.trim() || sentencePending} onClick={() => void sendSentence()} type="button">{sentencePending ? <LoaderCircle className="debug-spinner" size={16} aria-hidden="true" /> : <Send size={16} aria-hidden="true" />}문장 토픽 발행</button>
            </div>
            {!status.session.armed ? <p className="debug-inline-warning">수동 제어를 활성화한 후에만 문장을 발행할 수 있습니다.</p> : null}
            <p className="debug-card-description">도구 전달 즉시 실행과 리트랙터 ‘음성 + 버튼’ 게이트가 각각 켜져 있으면, 이 수동 문장도 해당 경로의 입력으로 처리됩니다.</p>
            <code className="debug-topic-code">/sensors/surgeon/sentence · std_msgs/msg/String</code>
          </article>

        </div>
    </section>
  );
}

function ToolVoiceScenarioPanel({
  status,
  connected,
  runCommand,
  openStt,
}: {
  status: IntegrationDebugStatus;
  connected: boolean;
  runCommand: RunDebugCommand;
  openStt: () => void;
}) {
  const parse = status.voice.last_parse;
  const endpointReady = status.endpoints.find((endpoint) => endpoint.name === "tool_handover")?.ready === true;
  const hasSentence = Boolean(status.voice.last_sentence);
  const parseReady = parse.matched === true && parse.operation === "tool_handover";
  return (
    <section className="debug-panel-stack" data-slot="debug-tool-voice-scenario">
      <Suspense fallback={<div className="debug-section-card debug-vlm-skeleton" role="status"><span /><span /><span /><p className="sr-only">도구전달 통합 경로를 준비하고 있습니다.</p></div>}><DebugIntegrationPipeline kind="tool_voice" status={status} /></Suspense>

      <article className="debug-section-card debug-control-card">
        <div className="debug-section-heading"><div><p>DETERMINISTIC ROUTER</p><h2>도구전달 음성 게이트</h2><span>리트랙터 Text VLM 경로와 분리된 도구 별칭 라우터입니다.</span></div><Shield size={19} aria-hidden="true" /></div>
        <div className="debug-voice-ownership-note" role="note"><Mic size={17} aria-hidden="true" /><div><strong>마이크는 ‘STT 입력·USB 캡처’ 기능에서만 엽니다</strong><span>이 시나리오는 이미 발행된 final 문장을 재사용하며 별도 캡처를 만들지 않습니다.</span></div></div>
        <button className={status.voice.auto_execute ? "button button-secondary full" : "button button-primary full"} disabled={!connected || !status.session.armed} onClick={() => void runCommand("configure_voice", { enabled: !status.voice.auto_execute })} type="button">{status.voice.auto_execute ? <ToggleRight size={17} aria-hidden="true" /> : <ToggleLeft size={17} aria-hidden="true" />}{status.voice.auto_execute ? "음성 도구전달 해제" : "음성 도구전달 활성화"}</button>
        {!status.session.armed ? <p className="debug-inline-warning">화면 상단에서 수동 제어를 먼저 활성화해야 합니다.</p> : null}
        {!endpointReady ? <p className="debug-field-error" role="alert"><XCircle size={15} aria-hidden="true" />Tool Handover Action 서버가 발견되지 않아 음성 요청을 전송할 수 없습니다.</p> : null}
        {hasSentence ? (
          <div className="debug-parse-preview" aria-live="polite"><span>최근 문장</span><strong>{status.voice.last_sentence}</strong><span>해석</span><StatusBadge state={parseReady ? "READY" : parse.ambiguous ? "TYPE_MISMATCH" : "WAITING"} label={parseReady ? String(parse.operation) : parse.ambiguous ? "모호함 · 실행 안 함" : String(parse.reason || "대기")} />{parse.payload ? <code>{JSON.stringify(parse.payload)}</code> : null}</div>
        ) : (
          <div className="debug-empty-state"><Headphones size={28} aria-hidden="true" /><p>아직 도구전달 문장이 없습니다. STT 입력에서 마이크를 시작하거나 수동 final 문장을 발행하세요.</p><button className="button button-secondary" onClick={openStt} type="button">STT 입력 열기</button></div>
        )}
      </article>
    </section>
  );
}

function RecordPanel({
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
            className={`debug-record-credential-status ${record.api_key_configured ? "is-configured" : "is-missing"}`}
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

export function DebugWorkspace({
  language,
  onExit,
}: {
  language: Language;
  onExit: () => void;
}) {
  const url = runtimeBridgeUrl("debug");
  const bridge = useIntegrationDebugBridge(url);
  const [activeTab, setActiveTab] = useState<DebugTab>("connection");
  const [notice, setNotice] = useState<Notice | null>(null);
  const [manualControlPending, setManualControlPending] = useState(false);
  const [coexistenceConfirmed, setCoexistenceConfirmed] = useState(false);

  const blockedNodes = bridge.status?.runtime.blocked_nodes ?? [];
  const blockedNodeSignature = blockedNodes.join("\u0000");
  const armed = bridge.status?.session.armed ?? false;

  useEffect(() => {
    setCoexistenceConfirmed(false);
  }, [blockedNodeSignature, bridge.status?.session.session_id]);

  useEffect(() => {
    if (!armed) setCoexistenceConfirmed(false);
  }, [armed]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 3200);
    return () => window.clearTimeout(timer);
  }, [notice]);

  async function runCommand(
    operation: string,
    payload: Record<string, unknown> = {},
    options: DebugCommandOptions = {},
  ) {
    try {
      const response = await bridge.command(operation, payload);
      if (!response.accepted) throw new Error(response.message || "명령이 거부되었습니다.");
      if (operation !== "heartbeat" && !options.silent) setNotice({ tone: "success", text: response.message });
      return response;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (!options.silent) setNotice({ tone: "error", text: message });
      return { accepted: false, command_id: "", message, result: {} };
    }
  }

  async function exitDebugMode() {
    if (manualControlPending) return;
    setManualControlPending(true);
    try {
      if (bridge.connected) {
        if (bridge.status && ["STARTING", "LISTENING", "STOPPING"].includes(bridge.status.asr.state)) {
          await runCommand("asr_stop", {}, SILENT_COMMAND_OPTIONS);
        }
        await runCommand("stop_outputs", {}, SILENT_COMMAND_OPTIONS);
        await runCommand("disarm", {}, SILENT_COMMAND_OPTIONS);
      }
      onExit();
    } finally {
      setManualControlPending(false);
    }
  }

  function focusManualRequirement(targetId: string) {
    setActiveTab((current) => current === "tool_voice" ? "tool_voice" : "retractor");
    window.requestAnimationFrame(() => document.getElementById(targetId)?.focus());
  }

  async function handleManualControl() {
    if (manualControlPending) return;
    const status = bridge.status;
    if (!status) return;
    if (status.action.recovery_required) {
      focusManualRequirement("debug-action-recovery");
      return;
    }
    if (!status.session.armed && !status.session.fault_locked && status.runtime.manual_control_available !== true) {
      setNotice({ tone: "warning", text: `${manualAvailabilityLabel(status)}입니다. 운영 시나리오와 Fault/명령 상태를 확인하세요.` });
      focusManualRequirement("debug-operational-interlock");
      return;
    }
    if (!status.session.armed && status.runtime.planner_coexistence_allowed === true && status.runtime.blocked_nodes.length > 0 && !coexistenceConfirmed) {
      setNotice({ tone: "warning", text: "발견된 전체 플래너의 자동 명령이 중지됐는지 먼저 확인하세요." });
      focusManualRequirement("debug-coexistence-checkbox");
      return;
    }
    setManualControlPending(true);
    try {
      if (status.session.fault_locked) {
        await runCommand("reset_fault");
      } else if (status.session.armed) {
        await runCommand("disarm");
      } else {
        await runCommand("arm", status.runtime.blocked_nodes.length > 0 ? {
          planner_coexistence_confirmed: true,
          acknowledged_blocked_nodes: status.runtime.blocked_nodes,
        } : {});
      }
    } finally {
      setManualControlPending(false);
    }
  }

  const readyInputCount = bridge.status?.inputs.filter((row) => row.state === "READY").length ?? 0;
  const readyEndpointCount = bridge.status?.endpoints.filter((row) => row.ready).length ?? 0;
  const enabledOutputCount = bridge.status?.outputs.filter((row) => row.enabled).length ?? 0;
  const blockedPlannerCount = blockedNodes.length;
  const asrFinalCount = bridge.status?.asr.finals?.length ?? 0;
  const statusAgeSec = bridge.statusReceivedAt ? Math.max(0, (Date.now() - bridge.statusReceivedAt) / 1000) : null;
  const statusFresh = Boolean(bridge.status && bridge.connected);
  const activeCommandIsAdmission = bridge.status?.action.response_semantics === "admission";
  const manualControlLabel = bridge.status?.action.recovery_required
    ? "명령 복구 필요"
    : bridge.status?.session.fault_locked
      ? "Fault 해제"
      : bridge.status?.session.armed
        ? bridge.status.action.terminal
          ? "수동 제어 해제"
          : activeCommandIsAdmission
            ? "수동 제어 해제 · Service 응답 대기"
            : "수동 제어 해제 · Action 취소"
        : blockedPlannerCount > 0 && bridge.status?.runtime.planner_coexistence_allowed === true && !coexistenceConfirmed
          ? "공존 확인 필요"
          : bridge.status?.runtime.manual_control_available === true
            ? "수동 제어 활성화"
            : "수동 제어 잠김";
  const manualControlDisabled = !bridge.status
    || manualControlPending
    || !statusFresh
    || (bridge.status.action.recovery_required
      ? false
      : bridge.status.session.fault_locked
        ? !bridge.connected
        : bridge.status.session.armed
          ? !bridge.connected
          : !bridge.connected
            || !bridge.status.action.terminal
            || statusAgeSec === null
            || statusAgeSec > DEBUG_STATUS_MAX_AGE_MS / 1_000
            || bridge.status.runtime.manual_control_available !== true
            || (blockedPlannerCount > 0 && bridge.status.runtime.planner_coexistence_allowed !== true));
  const tabs: DebugTabItem[] = [
    { id: "connection", group: "individual", label: "ROS 연결", meta: `${readyInputCount}/${bridge.status?.inputs.length ?? 0} 토픽`, icon: Radio },
    { id: "stt", group: "individual", label: "STT 입력·USB 캡처", meta: bridge.status ? `${bridge.status.asr.state} · final ${asrFinalCount}건` : "ASR 상태 대기", icon: Usb },
    { id: "vlm", group: "individual", label: "Text VLM 입·출력", meta: bridge.status?.vlm ? bridge.status.vlm.micro_test?.state || bridge.status.vlm.load_state || "상태 대기" : "optional 상태 대기", icon: Bug },
    { id: "perception", group: "individual", label: "CAM3·CAM4 인식 오버레이", meta: "1.7 · final raster + 상태", icon: ScanLine },
    { id: "tf", group: "individual", label: "TF 좌표계·3D 모델", meta: "/tf + /tf_static · 읽기 전용", icon: Network },
    { id: "multicam", group: "observability", label: "멀티캠 관제", meta: "읽기 전용", icon: ScanLine },
    { id: "endpoints", group: "individual", label: "Service·Action 종단", meta: `${readyEndpointCount}/${bridge.status?.endpoints.length ?? 0} 발견`, icon: Cable },
    { id: "tool_voice", group: "scenario", label: "음성 도구전달", meta: bridge.status?.voice.auto_execute ? "음성 게이트 ON" : "음성 게이트 OFF", icon: Headphones },
    { id: "retractor", group: "scenario", label: "리트랙터 6개 명령", meta: bridge.status?.action.recovery_required ? "명령 복구 필요" : bridge.status ? `${retractionInternalStateLabel(bridge.status.voice.retraction?.internal_state || "idle")} · ${bridge.status.voice.retraction?.mode === "voice_and_buttons" ? "음성 ON" : "버튼만"}` : "상태 대기", icon: Wrench },
    { id: "logs", group: "observability", label: "관측 로그", meta: bridge.status ? `이벤트 ${bridge.status.recent_events.length} · STT ${asrFinalCount}` : "로그 대기", icon: Activity },
    { id: "output", group: "utilities", label: "출력 검증", meta: `${enabledOutputCount}/${bridge.status?.outputs.length ?? 0} 발행`, icon: Send },
    { id: "record", group: "utilities", label: "수술기록 API", meta: bridge.status ? `${bridge.status.surgery_record.state} · 이력 ${bridge.status.surgery_record.history.length}건` : "계약 상태 대기", icon: FileText },
  ];
  const tabGroups: Array<{ id: DebugTabGroup; label: string }> = [
    { id: "individual", label: "개별 기능" },
    { id: "scenario", label: "통합 시나리오" },
    { id: "observability", label: "관측" },
    { id: "utilities", label: "추가 진단" },
  ];

  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
    if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = tabs.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const nextTab = tabs[nextIndex];
    setActiveTab(nextTab.id);
    window.requestAnimationFrame(() => document.getElementById(`debug-tab-${nextTab.id}`)?.focus());
  }

  return (
    <div className="app-shell debug-app-shell" data-slot="debug-workspace">
      <a className="skip-link" href={bridge.status ? `#debug-panel-${activeTab}` : "#debug-fallback"}>
        {language === "ko" ? "디버그 본문으로 이동" : "Skip to Debug content"}
      </a>
      <DebugHeader
        connected={bridge.connected}
        status={bridge.status}
        statusReceivedAt={bridge.statusReceivedAt}
        url={url}
        manualControlLabel={manualControlLabel}
        manualControlDisabled={manualControlDisabled}
        manualControlPending={manualControlPending}
        onManualControl={() => void handleManualControl()}
        onExit={() => void exitDebugMode()}
      />
      {!bridge.status ? (
        <ConnectionFallback connected={bridge.transportConnected} reconnecting={bridge.reconnecting} error={bridge.connectionError} url={url} onRetry={bridge.retry} />
      ) : (
        <>
          {!bridge.connected ? <div className="debug-disconnected-banner" role="status"><AlertTriangle size={16} aria-hidden="true" /><span>{bridge.connectionError || (bridge.transportConnected ? "디버그 상태 heartbeat가 만료되었습니다. 표시된 값은 마지막 수신 상태이며 모든 쓰기 제어는 잠겼습니다." : "ROSBridge 재연결 중입니다. 표시된 값은 마지막 수신 상태이며 모든 쓰기 제어는 잠겼습니다.")}</span><button className="runtime-transition-retry" onClick={bridge.retry} type="button">다시 연결</button></div> : null}
          <LayoutGroup id="debug-workspace-tabs">
            <div className="debug-tabs debug-tab-groups" role="tablist" aria-label="디버그 모드 기능">
              {tabGroups.map((group) => (
                <div className={`debug-tab-group ${group.id}`} key={group.id} role="presentation">
                  <p aria-hidden="true">{group.label}</p>
                  <div role="presentation">
                    {tabs.filter((tab) => tab.group === group.id).map((tab) => {
                      const index = tabs.findIndex((candidate) => candidate.id === tab.id);
                      const Icon = tab.icon;
                      const active = activeTab === tab.id;
                      return (
                        <button
                          aria-controls={`debug-panel-${tab.id}`}
                          aria-selected={active}
                          className={active ? "active" : ""}
                          id={`debug-tab-${tab.id}`}
                          key={tab.id}
                          onClick={() => setActiveTab(tab.id)}
                          onKeyDown={(event) => handleTabKeyDown(event, index)}
                          role="tab"
                          tabIndex={active ? 0 : -1}
                          type="button"
                        >
                          {active ? <m.span aria-hidden="true" className="debug-tab-focus" layoutId="debug-active-tab" transition={silk.layout.transition} /> : null}
                          <span className="debug-tab-label"><Icon size={17} aria-hidden="true" />{tab.label}</span>
                          <small>{tab.meta}</small>
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </LayoutGroup>
          <main aria-labelledby={`debug-tab-${activeTab}`} className="debug-main" id={`debug-panel-${activeTab}`} role="tabpanel" tabIndex={0}>
            {activeTab === "connection" ? <ConnectionPanel connected={bridge.connected} status={bridge.status} runCommand={runCommand} notify={setNotice} /> : null}
            {activeTab === "stt" ? <SttPanel connected={bridge.connected} status={bridge.status} runCommand={runCommand} /> : null}
            {activeTab === "perception" ? (
              <Suspense fallback={<div className="debug-section-card debug-vlm-skeleton" role="status"><span /><span /><span /><p className="sr-only">CAM3와 CAM4 인식 오버레이를 준비하고 있습니다.</p></div>}>
                <DebugPerceptionPanel
                  subscribeTopic={bridge.subscribeReadOnlyTopic}
                />
              </Suspense>
            ) : null}
            {activeTab === "tf" ? (
              <Suspense fallback={<div className="debug-section-card debug-vlm-skeleton" role="status"><span /><span /><span /><p className="sr-only">TF 좌표계와 3D 모델을 준비하고 있습니다.</p></div>}>
                <DebugTfPanel subscribeTopic={bridge.subscribeReadOnlyTopic} />
              </Suspense>
            ) : null}
            {activeTab === "multicam" ? (
              <Suspense fallback={<div className="debug-feedback-card" role="status"><LoaderCircle className="debug-spinner" size={28} aria-hidden="true" /><h2>멀티캠 관제 준비 중</h2></div>}>
                <DebugMulticamOpsPanel embedded language={language} />
              </Suspense>
            ) : null}
            {activeTab === "vlm" || activeTab === "endpoints" || activeTab === "logs" ? (
              <Suspense fallback={<div className="debug-feedback-card" role="status"><LoaderCircle className="debug-spinner" size={28} aria-hidden="true" /><h2>진단 화면을 준비하고 있습니다</h2></div>}>
                <DebugDiagnosticsPanels connected={bridge.connected} openStt={() => setActiveTab("stt")} readiness={bridge.readiness} runCommand={runCommand} status={bridge.status} tab={activeTab} />
              </Suspense>
            ) : null}
            {activeTab === "tool_voice" ? <><ToolVoiceScenarioPanel connected={bridge.connected} status={bridge.status} runCommand={runCommand} openStt={() => setActiveTab("stt")} /><ManualPanel connected={bridge.connected} status={bridge.status} runCommand={runCommand} coexistenceConfirmed={coexistenceConfirmed} setCoexistenceConfirmed={setCoexistenceConfirmed} manualControlPending={manualControlPending} scenario="tool_voice" /></> : null}
            {activeTab === "retractor" ? <ManualPanel connected={bridge.connected} status={bridge.status} runCommand={runCommand} coexistenceConfirmed={coexistenceConfirmed} setCoexistenceConfirmed={setCoexistenceConfirmed} manualControlPending={manualControlPending} scenario="retractor" /> : null}
            {activeTab === "output" ? <OutputPanel connected={bridge.connected} status={bridge.status} runCommand={runCommand} /> : null}
            {activeTab === "record" ? <RecordPanel connected={bridge.connected} status={bridge.status} runCommand={runCommand} notify={setNotice} /> : null}
          </main>
        </>
      )}
      <AnimatePresence initial={false}>
        {notice ? (
          <m.div
            {...statusSwap}
            className={`debug-toast ${notice.tone}`}
            key={`${notice.tone}-${notice.text}`}
            role={notice.tone === "error" ? "alert" : "status"}
          >
            {notice.tone === "error" || notice.tone === "warning"
              ? <AlertTriangle size={17} aria-hidden="true" />
              : notice.tone === "success"
                ? <CheckCircle2 size={17} aria-hidden="true" />
                : <RefreshCw size={17} aria-hidden="true" />}
            {notice.text}
          </m.div>
        ) : null}
      </AnimatePresence>
    </div>
  );
}
