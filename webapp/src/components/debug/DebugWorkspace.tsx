import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Bug,
  Cable,
  CheckCircle2,
  ChevronDown,
  CircleStop,
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
  Send,
  Shield,
  ShieldAlert,
  Square,
  ToggleLeft,
  ToggleRight,
  Wrench,
  XCircle,
} from "lucide-react";

import {
  type DebugCommandResponse,
  type DebugInputStatus,
  type DebugNetworkStatus,
  type DebugOutputStatus,
  type IntegrationDebugStatus,
  useIntegrationDebugBridge,
} from "../../hooks/useIntegrationDebugBridge";
import { runtimeBridgeUrl } from "../../runtimeModes";
import type { Language } from "../../utils/display";

type DebugTab = "connection" | "manual" | "output" | "voice";

interface Notice {
  tone: "success" | "error" | "warning" | "info";
  text: string;
}

interface DebugCommandOptions {
  silent?: boolean;
}

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

interface SpeechRecognitionResultLike {
  isFinal: boolean;
  0: { transcript: string };
}

interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: ArrayLike<SpeechRecognitionResultLike>;
}

interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onerror: ((event: { error?: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
}

type SpeechRecognitionConstructor = new () => SpeechRecognitionLike;

function speechRecognitionConstructor(): SpeechRecognitionConstructor | null {
  const speechWindow = window as unknown as {
    SpeechRecognition?: SpeechRecognitionConstructor;
    webkitSpeechRecognition?: SpeechRecognitionConstructor;
  };
  return speechWindow.SpeechRecognition ?? speechWindow.webkitSpeechRecognition ?? null;
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
    return `${stringValue("route") || "명령"} · ${stringValue("final_state") || "완료"} · ${stringValue("reason_code") || "reason 없음"}`;
  }
  if (event.event_type === "ui_command") {
    const accepted = payload.accepted === true ? "수락" : "거부";
    return `${stringValue("operation") || "UI 명령"} · ${accepted} · ${stringValue("message") || "응답 메시지 없음"}`;
  }
  if (event.event_type === "voice_dispatch") {
    return `${payload.accepted === true ? "실행 요청" : "실행 거부"} · ${stringValue("message") || "응답 메시지 없음"}`;
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
  if (["READY", "completed", "succeeded", "ARMED"].includes(state)) return "ok";
  if (["TYPE_MISMATCH", "FAULT_LOCKED", "failed", "rejected", "cancel_rejected"].includes(state)) return "error";
  if (["LOW_RATE", "STALE", "BUSY", "cancel_requested"].includes(state)) return "warn";
  return "idle";
}

function StatusBadge({ state, label }: { state: string; label?: string }) {
  const tone = stateTone(state);
  const Icon = tone === "ok" ? CheckCircle2 : tone === "error" ? XCircle : tone === "warn" ? AlertTriangle : Radio;
  return (
    <span className={"debug-status-badge " + tone} data-slot="debug-status-badge">
      <Icon size={14} aria-hidden="true" />
      {label ?? state}
    </span>
  );
}

function DebugHeader({
  connected,
  status,
  statusAgeSec,
  url,
  onExit,
}: {
  connected: boolean;
  status: IntegrationDebugStatus | null;
  statusAgeSec: number | null;
  url: string;
  onExit: () => void;
}) {
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
        <StatusBadge state={statusAgeSec !== null && statusAgeSec <= 3 ? "READY" : "STALE"} label={statusAgeSec === null ? "상태 대기" : statusAgeSec < 1 ? "방금 갱신" : `${statusAgeSec.toFixed(1)}초 전`} />
        <StatusBadge state={status?.session.state ?? "WAITING"} />
        <button className="button button-secondary debug-exit-button" onClick={onExit} type="button">
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
  if (connected || reconnecting) {
    return (
      <main className="debug-feedback-card debug-loading-card" aria-live="polite" data-slot="debug-loading-state">
        <div className="debug-skeleton-title" />
        <div className="debug-skeleton-row" />
        <div className="debug-skeleton-row short" />
        <p>{reconnecting ? "DDS 설정을 반영하고 디버그 모드에 다시 연결하고 있습니다." : "디버그 노드의 첫 상태 메시지를 기다리고 있습니다."}</p>
      </main>
    );
  }
  return (
    <main className="debug-feedback-card" data-slot="debug-error-state">
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
        <p className="debug-network-description">설정 적용 시 디버그 ROS 런타임만 재시작되며 UI가 자동으로 다시 연결됩니다.</p>
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
                <button aria-pressed={discoveryRange === value} className={discoveryRange === value ? "active" : ""} disabled={!connected} key={value} onClick={() => setDiscoveryRange(value)} type="button">
                  {value === "LOCALHOST" ? "이 컴퓨터만" : "같은 LAN"}
                  <small>{value}</small>
                </button>
              ))}
            </div>
          </fieldset>
          <label className="debug-field" htmlFor="debug-domain-id">
            <span>ROS Domain ID</span>
            <input aria-describedby={!validDomain ? "debug-domain-error" : "debug-domain-help"} aria-invalid={!validDomain} disabled={!connected} id="debug-domain-id" inputMode="numeric" max="232" min="0" step="1" type="number" value={domainId} onChange={(event) => setDomainId(event.target.value)} />
            <small id="debug-domain-help">상대 컴퓨터와 같은 값을 사용하세요. 허용 범위는 0–232입니다.</small>
          </label>
          {!validDomain ? <p className="debug-field-error" id="debug-domain-error" role="alert"><XCircle size={15} aria-hidden="true" />0부터 232 사이의 정수를 입력해 주세요.</p> : null}
          {domainCollisionWarning ? <p className="debug-inline-warning"><AlertTriangle size={15} aria-hidden="true" />Linux 임시 포트와 겹칠 수 있는 범위입니다. 가능하면 0–101 또는 215–232를 사용하세요.</p> : null}
          {changeBlocked ? <p className="debug-inline-warning"><AlertTriangle size={15} aria-hidden="true" />수동 제어, 실행 중 Action, 연속 더미 발행을 모두 정지한 뒤 변경할 수 있습니다.</p> : null}
          {!network.restart_supported ? <p className="debug-inline-warning"><AlertTriangle size={15} aria-hidden="true" />현재 실행 방식에서는 자동 재시작을 사용할 수 없습니다.</p> : null}
          <button className="button button-primary full" disabled={!connected || !changed || !validDomain || changeBlocked || networkPending || network.restart_scheduled || !network.restart_supported} type="submit">
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
      <td>
        <strong>{row.name}</strong>
        <code>{row.topic}</code>
      </td>
      <td>
        <StatusBadge state={row.state} />
        <small>{row.publisher_count} publisher</small>
      </td>
      <td>
        <strong>{formatHz(row.measured_hz)}</strong>
        <small>{row.expected_hz > 0 ? `기준 ${formatHz(row.expected_hz)}` : "측정 전용"}</small>
      </td>
      <td>
        <strong>{formatAge(row.last_age_sec)}</strong>
        <small>{formatBandwidth(row.bandwidth_bytes_sec)}</small>
      </td>
      <td>
        <code>{row.actual_types.join(", ") || row.expected_type}</code>
        <small>{row.qos_profiles.join(", ") || row.expected_qos}</small>
      </td>
      <td>
        <span className="debug-sample" title={row.last_sample}>{row.last_sample || "아직 메시지가 없습니다."}</span>
      </td>
    </tr>
  );
}

function ConnectionPanel({
  status,
  connected,
  readiness,
  runCommand,
  notify,
}: {
  status: IntegrationDebugStatus;
  connected: boolean;
  readiness: Record<string, unknown> | null;
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
          <dt>입력 토픽</dt><dd><strong>{readyInputs}</strong><span>/{status.inputs.length} 정상</span></dd><small>실시간 메시지 수신</small>
        </div>
        <div className={readyEndpoints === status.endpoints.length && status.endpoints.length ? "ok" : "warn"}>
          <dt>Action·Service</dt><dd><strong>{readyEndpoints}</strong><span>/{status.endpoints.length} 발견</span></dd><small>외부 로봇 종단</small>
        </div>
        <div className={outputSubscribers > 0 ? "ok" : enabledOutputs > 0 ? "warn" : "idle"}>
          <dt>출력 토픽</dt><dd><strong>{enabledOutputs}</strong><span> 발행 · {outputSubscribers} 구독</span></dd><small>총 {status.outputs.length}개 계약</small>
        </div>
        <div className={networkReady ? "ok debug-health-network" : "warn debug-health-network"}>
          <dt>{wiredSelected ? "유선 네트워크" : "로컬 네트워크"}</dt><dd><strong>{localAddressLabel(network)}</strong></dd><small>{network.primary_interface || "interface 대기"} · {localLinkLabel(network)} · D{status.runtime.ros_domain_id} · {status.runtime.discovery_range}</small>
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
              <div className="debug-table-scroll">
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

        <aside className="debug-observability-rail" aria-label="Action과 네트워크 상태">
          <article className="debug-section-card debug-endpoint-card">
            <div className="debug-section-heading">
              <div><p>CAPABILITY DISCOVERY</p><h2>로봇 Action·Service</h2></div>
              <StatusBadge state={Boolean(readiness?.ready) ? "READY" : "WAITING"} label={Boolean(readiness?.ready) ? "전체 Preflight 정상" : "전체 Preflight 미완료"} />
            </div>
            <div className="debug-active-action-compact" aria-live="polite">
              <span>실행 상태</span>
              <StatusBadge state={status.action.state} />
              <strong>{status.action.route || "활성 명령 없음"}</strong>
            </div>
            <div className="debug-endpoint-grid">
              {status.endpoints.map((endpoint) => (
                <div className="debug-endpoint-row" key={endpoint.endpoint}>
                  <StatusBadge state={endpoint.ready ? "READY" : "WAITING"} label={endpoint.ready ? "발견" : "대기"} />
                  <div><strong>{endpoint.name}</strong><code>{endpoint.endpoint}</code></div>
                  <small>{endpoint.kind}</small>
                </div>
              ))}
            </div>
          </article>
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
}: {
  status: IntegrationDebugStatus;
  connected: boolean;
  runCommand: RunDebugCommand;
}) {
  const [instrument, setInstrument] = useState("Kelly forceps");
  const [instance, setInstance] = useState("Kelly forceps#1");
  const [transition, setTransition] = useState("tray:surgeon");
  const [distance, setDistance] = useState(5);
  const [profile, setProfile] = useState("wide_retractor");
  const [pending, setPending] = useState("");
  const busy = !status.action.terminal;
  const armed = status.session.armed;
  const endpointReady = (name: string) => status.endpoints.find((row) => row.name === name)?.ready ?? false;

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

  async function move(direction: string) {
    await invoke("retraction", { operation: "MOVE", direction, distance_mm: distance });
  }

  const motionDisabled = !connected || !armed || busy || Boolean(pending);
  const moveDisabled =
    motionDisabled ||
    !endpointReady("retraction") ||
    !Number.isFinite(distance) ||
    distance <= 0 ||
    distance > 30;
  return (
    <section className="debug-panel-stack" data-slot="debug-manual-panel">
      <article className={"debug-arm-card " + status.session.state.toLowerCase()}>
        <div className="debug-arm-copy">
          {status.session.fault_locked ? <ShieldAlert size={24} aria-hidden="true" /> : <Shield size={24} aria-hidden="true" />}
          <div><p>MANUAL CONTROL</p><h2>{status.session.state}</h2><span>조그와 로봇 명령은 명시적으로 활성화한 동안만 전송됩니다.</span></div>
        </div>
        <div className="debug-arm-toolbar">
          <div className="debug-arm-readiness" aria-label="수동 제어 종단 준비 상태">
            {status.endpoints.map((endpoint) => <StatusBadge key={endpoint.endpoint} state={endpoint.ready ? "READY" : "WAITING"} label={`${endpoint.name} ${endpoint.ready ? "준비" : "대기"}`} />)}
          </div>
          {status.session.fault_locked ? (
            <button className="button button-secondary" disabled={!connected || Boolean(pending)} onClick={() => void invoke("reset_fault")} type="button">
              <RotateCcw size={16} aria-hidden="true" /> Fault 해제
            </button>
          ) : (
            <button className={armed ? "button button-secondary" : "button button-primary"} disabled={!connected || busy || Boolean(pending)} onClick={() => void invoke(armed ? "disarm" : "arm")} type="button">
              {armed ? <CircleStop size={16} aria-hidden="true" /> : <Play size={16} aria-hidden="true" />}{armed ? "수동 제어 해제" : "수동 제어 활성화"}
            </button>
          )}
        </div>
      </article>

      <div className="debug-control-grid debug-manual-grid">
        <form className="debug-section-card debug-control-card" onSubmit={(event) => void submitTool(event)}>
          <div className="debug-section-heading"><div><p>ACTION</p><h2>도구 전달</h2></div><StatusBadge state={endpointReady("tool_handover") ? "READY" : "WAITING"} label={endpointReady("tool_handover") ? "서버 발견" : "서버 대기"} /></div>
          <label className="debug-field"><span>실제 도구명</span><input value={instrument} onChange={(event) => setInstrument(event.target.value)} /></label>
          <label className="debug-field"><span>인스턴스 ID</span><input value={instance} onChange={(event) => setInstance(event.target.value)} /></label>
          <label className="debug-field"><span>전달 경로</span><select value={transition} onChange={(event) => setTransition(event.target.value)}>
            <option value="tray:robot">tray → robot</option><option value="tray:surgeon">tray → surgeon</option><option value="robot:surgeon">robot → surgeon</option><option value="robot:tray">robot → tray</option><option value="mayo:robot">mayo → robot</option><option value="mayo:tray">mayo → tray</option>
          </select></label>
          <button className="button button-primary full" disabled={motionDisabled || !endpointReady("tool_handover") || !instrument.trim()} type="submit"><Send size={16} aria-hidden="true" />도구 전달 요청</button>
          {!endpointReady("tool_handover") ? <p className="debug-inline-warning">Action 서버를 기다리고 있습니다.</p> : null}
        </form>

        <article className="debug-section-card debug-control-card">
          <div className="debug-section-heading"><div><p>STEP JOG</p><h2>리트랙터 조그</h2></div><StatusBadge state={endpointReady("retraction") ? "READY" : "WAITING"} label={endpointReady("retraction") ? "서버 발견" : "서버 대기"} /></div>
          <label className="debug-field"><span>이동 스텝</span><input aria-invalid={!Number.isFinite(distance) || distance <= 0 || distance > 30} min="0.1" max="30" step="0.5" type="number" value={distance} onChange={(event) => setDistance(Number(event.target.value))} /><small>한 번 누를 때 하나의 MOVE Goal · 최대 30 mm</small></label>
          <div className="debug-jog-pad" aria-label="리트랙터 방향 조그">
            <button aria-label="위로 이동" disabled={moveDisabled} onClick={() => void move("UP")} type="button"><ArrowUp aria-hidden="true" /></button>
            <button aria-label="왼쪽으로 이동" disabled={moveDisabled} onClick={() => void move("LEFT")} type="button"><ArrowLeft aria-hidden="true" /></button>
            <span>{distance || 0}<small>mm</small></span>
            <button aria-label="오른쪽으로 이동" disabled={moveDisabled} onClick={() => void move("RIGHT")} type="button"><ArrowRight aria-hidden="true" /></button>
            <button aria-label="아래로 이동" disabled={moveDisabled} onClick={() => void move("DOWN")} type="button"><ArrowDown aria-hidden="true" /></button>
          </div>
          <div className="debug-inline-actions">
            <button className="button button-secondary" disabled={motionDisabled || !endpointReady("retraction")} onClick={() => void invoke("retraction", { operation: "RELEASE" })} type="button">견인 해제</button>
            <button className="button button-quiet" disabled={motionDisabled || !endpointReady("retraction") || !profile.trim()} onClick={() => void invoke("retraction", { operation: "CHANGE_END_EFFECTOR", end_effector_profile: profile })} type="button">엔드이펙터 변경</button>
          </div>
          <label className="debug-field"><span>엔드이펙터 프로파일</span><input value={profile} onChange={(event) => setProfile(event.target.value)} /></label>
          {!endpointReady("retraction") ? <p className="debug-inline-warning">Action 서버가 발견될 때까지 조그 명령을 전송하지 않습니다.</p> : null}
        </article>

        <div className="debug-manual-side">
          <article className="debug-section-card debug-control-card debug-suction-card">
            <div className="debug-section-heading"><div><p>SERVICE</p><h2>석션 제어</h2></div><StatusBadge state={endpointReady("suction") ? "READY" : "WAITING"} label={endpointReady("suction") ? "서비스 발견" : "서비스 대기"} /></div>
            <p className="debug-card-description">ON/OFF 응답을 추정 없이 그대로 확인합니다.</p>
            <div className="debug-suction-actions">
              <button className="button button-primary" disabled={motionDisabled || !endpointReady("suction")} onClick={() => void invoke("suction", { enabled: true })} type="button">석션 ON</button>
              <button className="button button-secondary" disabled={motionDisabled || !endpointReady("suction")} onClick={() => void invoke("suction", { enabled: false })} type="button">석션 OFF</button>
            </div>
            {!endpointReady("suction") ? <p className="debug-inline-warning">Service 서버를 기다리고 있습니다.</p> : null}
          </article>

          <article className="debug-section-card debug-action-card" aria-live="polite">
            <div className="debug-section-heading"><div><p>ACTION FEEDBACK</p><h2>실행 상태</h2></div><Activity size={19} aria-hidden="true" /></div>
            <div className="debug-action-summary"><StatusBadge state={status.action.state} /><strong>{status.action.route || "대기"}</strong><code>{status.action.command_id || "활성 명령 없음"}</code></div>
            <div className="debug-progress-track" role="progressbar" aria-label="Action 진행률" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(status.action.progress * 100)} aria-valuetext={`${Math.round(status.action.progress * 100)}%, ${status.action.state}`}><span style={{ width: `${Math.round(status.action.progress * 100)}%` }} /></div>
            <div className="debug-action-meta"><span>{Math.round(status.action.progress * 100)}%</span><span>{(status.action.elapsed_sec ?? 0).toFixed(1)} s</span><span>{status.action.reason_code || "feedback 대기"}</span></div>
            {busy && status.action.route !== "suction" ? <button className="button button-secondary full" disabled={!connected || Boolean(pending)} onClick={() => void invoke("cancel_active")} type="button"><Square size={15} aria-hidden="true" />현재 Action 취소</button> : null}
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
  runCommand,
}: {
  row: DebugOutputStatus;
  rate: number;
  setRate: (value: number) => void;
  connected: boolean;
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
      <td><div className="debug-row-actions"><button className="button button-quiet" disabled={!connected || pending} onClick={() => void invoke("publish_once", { topic: row.topic })} type="button">1회 발행</button><button className={row.enabled ? "button button-secondary" : "button button-primary"} disabled={!connected || pending || (!row.enabled && !validRate)} onClick={() => void invoke("configure_output", { topic: row.topic, enabled: !row.enabled, rate_hz: row.enabled && !validRate ? row.configured_hz : rate })} type="button">{row.enabled ? "정지" : "연속 발행"}</button></div></td>
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
        <div className="debug-table-scroll">
          <table className="debug-table debug-output-table">
            <caption className="sr-only">공개 출력 토픽의 발행 상태, 발행률, 구독자 및 수동 제어</caption>
            <thead><tr><th>출력 토픽</th><th>상태</th><th>발행률</th><th>Subscriber</th><th>제어</th></tr></thead>
            <tbody>{status.outputs.map((row) => <OutputRow connected={connected} key={row.topic} row={row} rate={rates[row.topic] ?? row.configured_hz} setRate={(value) => setRates((current) => ({ ...current, [row.topic]: value }))} runCommand={runCommand} />)}</tbody>
          </table>
        </div>
      </article>
    </section>
  );
}

function VoicePanel({
  status,
  connected,
  publishSentence,
  runCommand,
  notify,
}: {
  status: IntegrationDebugStatus;
  connected: boolean;
  publishSentence: (sentence: string) => void;
  runCommand: RunDebugCommand;
  notify: (notice: Notice) => void;
}) {
  const [sentence, setSentence] = useState("");
  const [listening, setListening] = useState(false);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const recognitionAvailable = useMemo(() => speechRecognitionConstructor() !== null, []);

  function sendSentence(value = sentence) {
    try {
      publishSentence(value);
      setSentence(value.trim());
      notify({ tone: "success", text: "완성된 문장을 음성 입력 토픽에 발행했습니다." });
    } catch (error) {
      notify({ tone: "error", text: error instanceof Error ? error.message : String(error) });
    }
  }

  function startMicrophone() {
    const Constructor = speechRecognitionConstructor();
    if (!Constructor) {
      notify({ tone: "error", text: "이 브라우저에서는 음성 인식을 사용할 수 없습니다. 텍스트 입력을 사용해 주세요." });
      return;
    }
    const recognition = new Constructor();
    recognition.lang = "ko-KR";
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.onresult = (event) => {
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        if (!result.isFinal) continue;
        const transcript = result[0].transcript.trim();
        if (!transcript) continue;
        setSentence(transcript);
        sendSentence(transcript);
      }
    };
    recognition.onerror = (event) => {
      setListening(false);
      notify({ tone: "error", text: `마이크 입력을 완료하지 못했습니다: ${event.error || "unknown"}` });
    };
    recognition.onend = () => {
      setListening(false);
      recognitionRef.current = null;
    };
    recognitionRef.current = recognition;
    setListening(true);
    try {
      recognition.start();
    } catch (error) {
      recognitionRef.current = null;
      setListening(false);
      notify({ tone: "error", text: `마이크를 시작할 수 없습니다: ${error instanceof Error ? error.message : String(error)}` });
    }
  }

  function stopMicrophone() {
    recognitionRef.current?.stop();
  }

  useEffect(() => () => recognitionRef.current?.stop(), []);

  const parse = status.voice.last_parse;
  return (
    <section className="debug-panel-stack" data-slot="debug-voice-panel">
      <div className="debug-voice-workspace">
        <div className="debug-voice-controls">
          <article className="debug-section-card">
            <div className="debug-section-heading"><div><p>SENTENCE INPUT</p><h2>텍스트·마이크 입력</h2></div><Headphones size={19} aria-hidden="true" /></div>
            <label className="debug-field"><span>집도의 완성 문장</span><textarea rows={4} value={sentence} onChange={(event) => setSentence(event.target.value)} placeholder="예: 켈리 주세요" /></label>
            <div className="debug-inline-actions">
              <button className="button button-primary" disabled={!connected || !sentence.trim()} onClick={() => sendSentence()} type="button"><Send size={16} aria-hidden="true" />문장 토픽 발행</button>
              <button aria-pressed={listening} className="button button-secondary" disabled={!connected || !recognitionAvailable} onClick={listening ? stopMicrophone : startMicrophone} type="button">{listening ? <MicOff size={16} aria-hidden="true" /> : <Mic size={16} aria-hidden="true" />}{listening ? "듣기 중지" : "마이크 입력"}</button>
            </div>
            {!recognitionAvailable ? <p className="debug-inline-warning">마이크 인식 미지원 환경입니다. 텍스트 발행은 계속 사용할 수 있습니다.</p> : null}
            <code className="debug-topic-code">/sensors/surgeon/sentence · std_msgs/msg/String</code>
          </article>

          <article className="debug-section-card">
            <div className="debug-section-heading"><div><p>DETERMINISTIC ROUTER</p><h2>음성 즉시 실행</h2></div><Shield size={19} aria-hidden="true" /></div>
            <p className="debug-card-description">VLM·BT 없이 설정된 정확한 문법만 Action으로 변환합니다. 모호한 문장은 실행하지 않습니다.</p>
            <button className={status.voice.auto_execute ? "button button-secondary full" : "button button-primary full"} disabled={!connected || !status.session.armed} onClick={() => void runCommand("configure_voice", { enabled: !status.voice.auto_execute })} type="button">{status.voice.auto_execute ? <ToggleRight size={17} aria-hidden="true" /> : <ToggleLeft size={17} aria-hidden="true" />}{status.voice.auto_execute ? "즉시 실행 해제" : "즉시 실행 활성화"}</button>
            {!status.session.armed ? <p className="debug-inline-warning">수동 실행 탭에서 제어를 먼저 활성화해야 합니다.</p> : null}
            <div className="debug-parse-preview">
              <span>최근 문장</span><strong>{status.voice.last_sentence || "수신 전"}</strong>
              <span>해석</span><StatusBadge state={parse.matched ? "READY" : parse.ambiguous ? "TYPE_MISMATCH" : "WAITING"} label={parse.matched ? String(parse.operation) : parse.ambiguous ? "모호함 · 실행 안 함" : String(parse.reason || "대기")} />
              {parse.payload ? <code>{JSON.stringify(parse.payload)}</code> : null}
            </div>
          </article>
        </div>

        <article className="debug-section-card debug-event-card">
          <div className="debug-section-heading"><div><p>SESSION LOG</p><h2>검증 이벤트</h2><span title={status.session.event_log_path}>{status.session.event_log_path}</span></div><span className="debug-meta-pill">최근 {Math.min(status.recent_events.length, 30)}건</span></div>
          {status.recent_events.length ? (
            <ol className="debug-event-list">
              {[...status.recent_events].reverse().slice(0, 30).map((event, index) => (
                <li key={`${event.stamp}-${event.event_type}-${index}`}>
                  <time dateTime={event.stamp}>{formatEventTime(event.stamp)}</time>
                  <strong>{event.event_type}</strong>
                  <span className="debug-event-summary">{eventSummary(event)}</span>
                  <details className="debug-event-raw"><summary>원문</summary><code>{JSON.stringify(event.payload, null, 2)}</code></details>
                </li>
              ))}
            </ol>
          ) : <div className="debug-empty-state"><Activity size={28} aria-hidden="true" /><p>아직 기록된 검증 이벤트가 없습니다.</p></div>}
        </article>
      </div>
    </section>
  );
}

export function DebugWorkspace({
  language: _language,
  onExit,
}: {
  language: Language;
  onExit: () => void;
}) {
  const url = runtimeBridgeUrl("debug");
  const bridge = useIntegrationDebugBridge(url);
  const [activeTab, setActiveTab] = useState<DebugTab>("connection");
  const [notice, setNotice] = useState<Notice | null>(null);

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
    if (bridge.connected) {
      try { await bridge.command("stop_outputs"); } catch { /* heartbeat timeout remains the fallback */ }
      try { await bridge.command("disarm"); } catch { /* heartbeat timeout remains the fallback */ }
    }
    onExit();
  }

  const readyInputCount = bridge.status?.inputs.filter((row) => row.state === "READY").length ?? 0;
  const readyEndpointCount = bridge.status?.endpoints.filter((row) => row.ready).length ?? 0;
  const enabledOutputCount = bridge.status?.outputs.filter((row) => row.enabled).length ?? 0;
  const statusAgeSec = bridge.statusReceivedAt ? Math.max(0, (Date.now() - bridge.statusReceivedAt) / 1000) : null;
  const tabs: Array<{ id: DebugTab; label: string; meta: string; icon: typeof Radio }> = [
    { id: "connection", label: "연결·입력", meta: `${readyInputCount}/${bridge.status?.inputs.length ?? 0} 토픽 · ${readyEndpointCount}/${bridge.status?.endpoints.length ?? 0} 종단`, icon: Radio },
    { id: "manual", label: "조그·수동 실행", meta: bridge.status?.session.state ?? "상태 대기", icon: Wrench },
    { id: "output", label: "출력 검증", meta: `${enabledOutputCount}/${bridge.status?.outputs.length ?? 0} 발행`, icon: Send },
    { id: "voice", label: "음성·로그", meta: bridge.status?.voice.auto_execute ? "즉시 실행 ON" : "수동 입력", icon: Mic },
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
      <DebugHeader connected={bridge.connected} status={bridge.status} statusAgeSec={statusAgeSec} url={url} onExit={() => void exitDebugMode()} />
      {!bridge.status ? (
        <ConnectionFallback connected={bridge.connected} reconnecting={bridge.reconnecting} error={bridge.connectionError} url={url} onRetry={bridge.retry} />
      ) : (
        <>
          {!bridge.connected ? <div className="debug-disconnected-banner" role="status"><AlertTriangle size={16} aria-hidden="true" />ROSBridge 재연결 중입니다. 표시된 값은 마지막 수신 상태이며 모든 쓰기 제어는 잠겼습니다.</div> : null}
          <div className="debug-tabs" role="tablist" aria-label="디버그 모드 기능">
            {tabs.map((tab, index) => {
              const Icon = tab.icon;
              return <button aria-controls={`debug-panel-${tab.id}`} aria-selected={activeTab === tab.id} className={activeTab === tab.id ? "active" : ""} id={`debug-tab-${tab.id}`} key={tab.id} onClick={() => setActiveTab(tab.id)} onKeyDown={(event) => handleTabKeyDown(event, index)} role="tab" tabIndex={activeTab === tab.id ? 0 : -1} type="button"><span><Icon size={17} aria-hidden="true" />{tab.label}</span><small>{tab.meta}</small></button>;
            })}
          </div>
          <main aria-labelledby={`debug-tab-${activeTab}`} className="debug-main" id={`debug-panel-${activeTab}`} role="tabpanel" tabIndex={0}>
            {activeTab === "connection" ? <ConnectionPanel connected={bridge.connected} status={bridge.status} readiness={bridge.readiness} runCommand={runCommand} notify={setNotice} /> : null}
            {activeTab === "manual" ? <ManualPanel connected={bridge.connected} status={bridge.status} runCommand={runCommand} /> : null}
            {activeTab === "output" ? <OutputPanel connected={bridge.connected} status={bridge.status} runCommand={runCommand} /> : null}
            {activeTab === "voice" ? <VoicePanel connected={bridge.connected} status={bridge.status} publishSentence={bridge.publishSentence} runCommand={runCommand} notify={setNotice} /> : null}
          </main>
        </>
      )}
      {notice ? <div className={"debug-toast " + notice.tone} role={notice.tone === "error" ? "alert" : "status"}>{notice.tone === "error" || notice.tone === "warning" ? <AlertTriangle size={17} aria-hidden="true" /> : notice.tone === "success" ? <CheckCircle2 size={17} aria-hidden="true" /> : <RefreshCw size={17} aria-hidden="true" />}{notice.text}</div> : null}
    </div>
  );
}
