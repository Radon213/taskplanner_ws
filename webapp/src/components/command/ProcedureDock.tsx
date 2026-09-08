import { lazy, Suspense, useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  CircleX,
  Clock3,
  GitBranch,
  Pause,
  Play,
  RadioTower,
  RotateCcw,
  Square,
  Wifi,
} from "lucide-react";

import { SafetyConfirmationDialog } from "../common/SafetyConfirmationDialog";
import {
  ExecutionRouteSelector,
  type ExecutionEndpointServerHealth,
  type ExecutionEndpointSource,
  type ExecutionRouteSourceReadinessByOperation,
  type ExecutionRouteTransitionState,
} from "./ExecutionRouteSelector";
import {
  type ControlCommand,
  type ExecutionRouteInitializationState,
  type RuntimeAuthorityStatus,
} from "../../hooks/useRosBridge";
import {
  integrationReadinessDiagnosticReason,
  type IntegrationReadiness,
  type IntegrationReadinessDiagnosticReason,
  type IntegrationReadinessChecklistItem,
} from "../../ros/runtimeAdmissionMessages";
import type { useDigitalTwinViewModel } from "../../hooks/useDigitalTwinViewModel";
import type { RuntimeTransitionStatus } from "../../hooks/useRuntimeControl";
import type { TaskplannerRuntimeMode } from "../../runtimeModes";
import type {
  ScenarioRevisionApplyAdmission,
  ScenarioRevisionState,
} from "../../ros/scenarioRevision";
import type { RosbagRecordingStatus } from "../../ros/rosbagRecordingMessages";
import { runtimeAuthorityCopy } from "../../utils/runtimeAuthorityCopy";

type ViewModel = ReturnType<typeof useDigitalTwinViewModel>;
type MissionRuntimeMode = Exclude<TaskplannerRuntimeMode, "debug">;

const ScenarioRevisionControl = lazy(() =>
  import("./ScenarioRevisionControl").then((module) => ({
    default: module.ScenarioRevisionControl,
  })),
);

const INTEGRATION_CHECK_ORDER = [
  "contract_configuration",
  "surgeon_sentence_publisher",
  "tool_handover_action_server",
  "retraction_command_service",
  "asr_runtime_status",
  "perception_input",
] as const;

// These remain available on diagnostic telemetry, but they are no longer
// admission requirements and must not reappear as synthetic start-check rows
// when an older replay or cached bridge snapshot still contains them.
const HIDDEN_INTEGRATION_CHECKS = new Set([
  "controller_contract",
  "bed_robot_arm_status",
]);

type PreflightDisplayItem = IntegrationReadinessChecklistItem & {
  displayStatus: "pass" | "fail" | "pending";
};

function integrationCheckLabel(check: string, language: ViewModel["language"]) {
  const labels: Record<string, readonly [string, string]> = {
    contract_configuration: ["통합 계약 설정", "integration contract configuration"],
    surgeon_sentence_publisher: ["최종 ASR 입력", "final ASR input"],
    tool_handover_action_server: ["도구 전달 Action", "tool-handover Action"],
    retraction_command_service: ["리트랙션 Service", "retraction Service"],
    perception_input: ["인식 입력", "perception input"],
    execution_contract: ["실행 계약", "execution contract"],
    external_execution_contract: ["외부 실행 계약", "external execution contract"],
    asr_runtime_status: ["ASR 실행 상태", "ASR runtime status"],
  };
  const label = labels[check];
  return label ? label[language === "ko" ? 0 : 1] : check.replace(/_/g, " ");
}

function integrationReadinessCopy(
  blocker: ReturnType<typeof integrationReadinessDiagnosticReason>,
  readiness: IntegrationReadiness | null,
  bundle: string,
  language: ViewModel["language"],
) {
  const isKorean = language === "ko";
  if (blocker === "missing") {
    return isKorean
      ? "통합 점검 상태를 기다리는 중입니다. 이 정보는 관찰용이며 수술 시작을 잠그지 않습니다."
      : "Waiting for the integration diagnostic. It is observational and does not lock Start.";
  }
  if (blocker === "stale") {
    return isKorean
      ? "통합 점검 상태가 만료되었습니다. 새 결과가 오면 표시를 갱신하지만 수술 시작을 잠그지 않습니다."
      : "The integration diagnostic expired. A fresh result will update this display, but Start remains available.";
  }
  if (blocker === "bundle_mismatch") {
    const selected = bundle || (isKorean ? "선택된 수술" : "the selected procedure");
    const observed = readiness?.activeBundle || (isKorean ? "확인되지 않음" : "unconfirmed");
    return isKorean
      ? `통합 점검은 ${observed} 기준입니다. ${selected}의 관찰 상태는 새 결과가 오면 갱신됩니다.`
      : `The integration diagnostic is for ${observed}. ${selected} updates when fresh observation arrives.`;
  }
  if (blocker === "not_ready") {
    const missing = readiness?.missing
      .map((check) => integrationCheckLabel(check, language))
      .join(isKorean ? " · " : ", ");
    return isKorean
      ? `통합 점검에서 주의가 필요합니다${missing ? `: ${missing}` : ""}. 실행 endpoint가 요청을 수락하면 시작할 수 있습니다.`
      : `The integration diagnostic needs attention${missing ? `: ${missing}` : ""}. Start remains available if the execution endpoint admits the request.`;
  }
  const actionSource = readiness?.robotEndpointSource ?? "external";
  const retractionSource = readiness?.retractionEndpointSource ?? actionSource;
  const sourceCopy = (source: string) => source === "virtual"
    ? isKorean ? "가상" : "virtual"
    : isKorean ? "외부 계약" : "external contract";
  const route = actionSource === retractionSource
    ? actionSource === "virtual"
      ? isKorean
        ? "가상 Action·Service 경로(실물 제어 아님)"
        : "virtual Action/Service route (not physical control)"
      : isKorean
        ? "외부 Action·Service 계약 경로"
        : "external Action/Service contract route"
    : isKorean
      ? `도구 전달: ${sourceCopy(actionSource)} · 리트랙션: ${sourceCopy(retractionSource)}`
      : `tool handover: ${sourceCopy(actionSource)} · retraction: ${sourceCopy(retractionSource)}`;
  return isKorean
    ? `통합 시작 점검을 통과했습니다. 실행 경로: ${route}.`
    : `The integration-start check passed. Execution route: ${route}.`;
}

function integrationReadinessStateCopy(
  blocker: IntegrationReadinessDiagnosticReason | null,
  language: ViewModel["language"],
) {
  const isKorean = language === "ko";
  if (blocker === null) return isKorean ? "통과" : "Passed";
  if (blocker === "not_ready") return isKorean ? "주의 필요" : "Needs attention";
  if (blocker === "bundle_mismatch") return isKorean ? "번들 불일치" : "Bundle mismatch";
  return isKorean ? "점검 대기" : "Check pending";
}

function rosbagRecordingStateCopy(
  status: RosbagRecordingStatus | null,
  language: ViewModel["language"],
) {
  const isKorean = language === "ko";
  if (!status) return isKorean ? "상태 수신 대기" : "Waiting for recorder status";
  const labels: Record<RosbagRecordingStatus["state"], readonly [string, string]> = {
    idle: ["대기", "Idle"],
    starting: ["녹화 시작 중", "Starting recording"],
    recording: ["녹화 중", "Recording"],
    stopping: ["저장 중", "Saving"],
    saved: ["저장됨", "Saved"],
    failed: ["녹화 오류", "Recording error"],
  };
  return labels[status.state][isKorean ? 0 : 1];
}

function integrationReasonCopy(
  reason: string,
  detail: string,
  language: ViewModel["language"],
) {
  const exactReason = reason.trim();
  const normalized = exactReason.toLowerCase();
  const known: Record<string, readonly [string, string]> = {
    passed: ["확인됨", "Confirmed"],
    not_required: ["이 번들에서는 필요하지 않음", "Not required for this bundle"],
    waiting_for_fresh_observation: ["새 관측 결과 대기", "Waiting for a fresh observation"],
    observation_missing: ["관측 결과 없음", "Observation missing"],
    observation_stale: ["관측 결과 만료", "Observation expired"],
    source_missing: ["입력 소스 없음", "Input source missing"],
    source_stale: ["입력 소스 만료", "Input source expired"],
    server_not_ready: ["서버 준비 안 됨", "Server not ready"],
    contract_mismatch: ["계약 불일치", "Contract mismatch"],
  };
  const summary = known[normalized]?.[language === "ko" ? 0 : 1]
    ?? exactReason.replace(/_/g, " ");
  const displayReason = known[normalized]
    ? `${summary} (${exactReason})`
    : exactReason;
  return detail ? `${displayReason} · ${detail}` : displayReason;
}

function integrationTimestampCopy(stampSec: number | undefined, language: ViewModel["language"]) {
  if (!Number.isFinite(stampSec) || !stampSec || stampSec <= 0) {
    return language === "ko" ? "수신 대기" : "Awaiting status";
  }
  try {
    const formatted = new Intl.DateTimeFormat(language === "ko" ? "ko-KR" : "en-US", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date(stampSec * 1_000));
    return `${formatted} · stamp_sec=${stampSec.toFixed(3)}`;
  } catch {
    return `stamp_sec=${stampSec.toFixed(3)}`;
  }
}

function integrationPreflightItems(
  readiness: IntegrationReadiness | null,
  blocker: IntegrationReadinessDiagnosticReason | null,
): readonly PreflightDisplayItem[] {
  const received = new Map(
    readiness?.checklist
      .filter((item) => !HIDDEN_INTEGRATION_CHECKS.has(item.id))
      .map((item) => [item.id, item]) ?? [],
  );
  const ids = [
    ...INTEGRATION_CHECK_ORDER,
    ...[...received.keys()]
      .filter((id) => !INTEGRATION_CHECK_ORDER.includes(id as typeof INTEGRATION_CHECK_ORDER[number]))
      .sort((left, right) => left.localeCompare(right)),
  ];
  return ids.map((id) => {
    const current = received.get(id);
    if (blocker === "missing") {
      return {
        id,
        required: current?.required ?? true,
        status: "pending",
        displayStatus: "pending",
        reason: "waiting_for_fresh_observation",
        detail: "",
      };
    }
    if (blocker === "stale") {
      return {
        id,
        required: current?.required ?? true,
        status: "pending",
        displayStatus: "pending",
        reason: "observation_stale",
        detail: "",
      };
    }
    if (current) return { ...current, displayStatus: current.status };
    const passed = readiness?.checks[id];
    return {
      id,
      required: true,
      status: passed === true ? "pass" : passed === false ? "fail" : "pending",
      displayStatus: passed === true ? "pass" : passed === false ? "fail" : "pending",
      reason: passed === true ? "passed" : passed === false ? "observation_missing" : "waiting_for_fresh_observation",
      detail: "",
    };
  });
}

function IntegrationPreflightPanel({
  readiness,
  blocker,
  selectedBundle,
  language,
}: {
  readiness: IntegrationReadiness | null;
  blocker: IntegrationReadinessDiagnosticReason | null;
  selectedBundle: string;
  language: ViewModel["language"];
}) {
  const items = integrationPreflightItems(readiness, blocker);
  const passed = items.filter((item) => item.displayStatus === "pass").length;
  const failed = items.filter((item) => item.displayStatus === "fail").length;
  const pending = items.length - passed - failed;
  const state = blocker === null ? "pass" : blocker === "not_ready" ? "fail" : "pending";
  const activeBundle = readiness?.activeBundle || (language === "ko" ? "수신 대기" : "Awaiting status");
  const selected = selectedBundle || (language === "ko" ? "선택 없음" : "Not selected");
  const timestamp = integrationTimestampCopy(readiness?.stampSec, language);
  return (
    <section
      aria-label={language === "ko" ? "통합 시작 점검 상세" : "Integration start-check details"}
      className={`integration-preflight-panel state-${state}`}
      data-slot="integration-preflight-checklist"
      data-preflight-state={state}
    >
      <div className="integration-preflight-header">
        <div>
          <span>{language === "ko" ? "통합 시작 점검" : "Integration start check"}</span>
          <strong>{integrationReadinessStateCopy(blocker, language)}</strong>
        </div>
        {state === "pass" ? (
          <CheckCircle2 aria-hidden="true" size={18} />
        ) : state === "fail" ? (
          <CircleX aria-hidden="true" size={18} />
        ) : (
          <Clock3 aria-hidden="true" size={18} />
        )}
      </div>
      <div className="integration-preflight-meta" data-preflight-stamp={readiness?.stampSec ?? ""}>
        <span>{language === "ko" ? `점검 번들 ${activeBundle}` : `Checked bundle ${activeBundle}`}</span>
        <span>{language === "ko" ? `선택 번들 ${selected}` : `Selected bundle ${selected}`}</span>
        <span>{language === "ko" ? `점검 시각 ${timestamp}` : `Checked at ${timestamp}`}</span>
      </div>
      <div className="integration-preflight-counts" aria-label={language === "ko" ? "점검 요약" : "Check summary"}>
        <span data-status="pass">{language === "ko" ? `통과 ${passed}` : `Pass ${passed}`}</span>
        <span data-status="fail">{language === "ko" ? `미통과 ${failed}` : `Fail ${failed}`}</span>
        <span data-status="pending">{language === "ko" ? `대기 ${pending}` : `Pending ${pending}`}</span>
      </div>
      <ol>
        {items.map((item) => {
          const Icon = item.displayStatus === "pass"
            ? CheckCircle2
            : item.displayStatus === "fail"
              ? CircleX
              : Clock3;
          return (
            <li data-check-id={item.id} data-check-status={item.displayStatus} key={item.id}>
              <Icon aria-hidden="true" size={15} />
              <div>
                <strong>{integrationCheckLabel(item.id, language)}</strong>
                <small>{integrationReasonCopy(item.reason, item.detail, language)}</small>
                <small
                  className="integration-preflight-item-stamp"
                  data-check-stamp={readiness?.stampSec ?? ""}
                >
                  {language === "ko" ? `점검 시각 ${timestamp}` : `Checked at ${timestamp}`}
                </small>
              </div>
              <em>{item.required ? (language === "ko" ? "필수" : "Required") : (language === "ko" ? "선택" : "Optional")}</em>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export function ProcedureDock({
  vm,
  url,
  runtimeMode,
  onRuntimeModeChange,
  allowRuntimeModeSelection,
  runtimeTransition,
  onRetryRuntimeMode,
  bundle,
  onBundleChange,
  activeBundle,
  scenarioRevision,
  scenarioRevisionAdmission,
  onPreviewBundle,
  onApplyBundle,
  startPhase,
  setStartPhase,
  transportConnected,
  connected,
  runtimeAuthorityStatus,
  actionPending,
  actionMessage,
  runtimeMessage,
  runtimeReady,
  rosbagRecording,
  rosbagRecordingControlPending,
  rosbagRecordingControlMessage,
  onRosbagRecordingControl,
  integrationReadiness,
  integrationReadinessReceivedAt,
  executionRoute,
  executionState,
  isRunning,
  isPaused,
  canPauseResume,
  onControl,
}: {
  vm: ViewModel;
  url: string;
  runtimeMode: TaskplannerRuntimeMode;
  onRuntimeModeChange: (mode: TaskplannerRuntimeMode) => void | Promise<void>;
  /** Optional Lab profiles may expose LLM/Replay; Production Live is fixed. */
  allowRuntimeModeSelection: boolean;
  runtimeTransition: RuntimeTransitionStatus;
  onRetryRuntimeMode: () => void;
  bundle: string;
  onBundleChange: (bundle: string) => void;
  activeBundle: string;
  scenarioRevision: ScenarioRevisionState;
  scenarioRevisionAdmission: ScenarioRevisionApplyAdmission;
  onPreviewBundle: () => void;
  onApplyBundle: () => void;
  startPhase: string;
  setStartPhase: (phaseId: string) => void;
  /** Transport is enough for the independent recorder Service; no scenario state is required. */
  transportConnected: boolean;
  connected: boolean;
  runtimeAuthorityStatus: RuntimeAuthorityStatus;
  actionPending: string;
  actionMessage: string;
  runtimeMessage: string;
  runtimeReady: boolean;
  rosbagRecording: RosbagRecordingStatus | null;
  rosbagRecordingControlPending: string;
  rosbagRecordingControlMessage: string;
  onRosbagRecordingControl: (enabled: boolean) => void | Promise<unknown>;
  integrationReadiness: IntegrationReadiness | null;
  integrationReadinessReceivedAt: number | null;
  /** Live-only, server-confirmed Action/Service route selection surface. */
  executionRoute?: {
    currentSource: ExecutionEndpointSource | null;
    currentRetractionSource: ExecutionEndpointSource | null;
    initializationState: ExecutionRouteInitializationState | null;
    sourceReadiness: ExecutionRouteSourceReadinessByOperation;
    sourceHealth: Readonly<
      Partial<Record<ExecutionEndpointSource, ExecutionEndpointServerHealth>>
    >;
    transitionState: ExecutionRouteTransitionState;
    transitionMessage: string;
    procedureStopped: boolean;
    disabledReason: string;
    onSourcesChange: (sources: {
      toolHandoverSource: ExecutionEndpointSource;
      retractionSource: ExecutionEndpointSource;
    }) => void;
  };
  executionState: string;
  isRunning: boolean;
  isPaused: boolean;
  canPauseResume: boolean;
  onControl: (command: ControlCommand) => void;
}) {
  const [resetConfirmationOpen, setResetConfirmationOpen] = useState(false);
  const runtimeSwitchPending = runtimeTransition.phase === "starting";
  const runtimeSwitchStartedAtRef = useRef<number | null>(null);
  const [runtimeSwitchNow, setRuntimeSwitchNow] = useState(() => Date.now());
  const [integrationReadinessNow, setIntegrationReadinessNow] = useState(() => Date.now());
  useEffect(() => {
    if (!runtimeSwitchPending) {
      runtimeSwitchStartedAtRef.current = null;
      return;
    }
    if (runtimeSwitchStartedAtRef.current === null) {
      runtimeSwitchStartedAtRef.current = Date.now();
    }
    setRuntimeSwitchNow(Date.now());
    const timer = window.setInterval(() => setRuntimeSwitchNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [runtimeSwitchPending]);
  useEffect(() => {
    if (runtimeMode !== "live") return;
    setIntegrationReadinessNow(Date.now());
    const timer = window.setInterval(() => setIntegrationReadinessNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [runtimeMode, integrationReadinessReceivedAt]);
  const runtimeSwitchElapsedSec = runtimeSwitchPending && runtimeSwitchStartedAtRef.current !== null
    ? Math.max(0, Math.floor((runtimeSwitchNow - runtimeSwitchStartedAtRef.current) / 1_000))
    : 0;
  const runtimeSwitchDelayed = runtimeSwitchElapsedSec >= 10;
  const runtimeStatusChecking = runtimeTransition.phase === "checking";
  const runtimeProfileMismatch =
    runtimeTransition.diagnosticCode === "runtime_profile_mismatch";
  const startInFlight = executionState === "starting" || actionPending.toLowerCase().includes("starting");
  const commandBusy = Boolean(actionPending);
  const rosbagRecordingActive = Boolean(rosbagRecording?.recordingActive);
  const rosbagRecordingTransitioning =
    rosbagRecording?.state === "starting" || rosbagRecording?.state === "stopping";
  const rosbagRecordingDisabled =
    !transportConnected || Boolean(rosbagRecordingControlPending) || rosbagRecordingTransitioning;
  const rosbagRecordingStatusCopy = rosbagRecordingStateCopy(rosbagRecording, vm.language);
  const rosbagRecordingDetail = rosbagRecordingControlMessage
    || rosbagRecording?.message
    || (transportConnected
      ? vm.language === "ko"
        ? "수동으로 시작·종료합니다. 시나리오 상태와 무관합니다."
        : "Start and stop manually; scenario state is not used."
      : vm.language === "ko"
        ? "ROS 전송 연결이 준비되면 사용할 수 있습니다."
        : "Available when the ROS transport is connected.");
  const integrationStartBlocker = runtimeMode === "live"
    ? integrationReadinessDiagnosticReason(
        integrationReadiness,
        integrationReadinessReceivedAt,
        activeBundle,
        integrationReadinessNow,
      )
    : null;
  const liveRouteInitializing =
    runtimeMode === "live" && executionRoute?.initializationState === "initializing";
  const routeInitializationMessage = vm.language === "ko"
    ? "실행 경로를 준비 중입니다. 준비가 완료될 때까지 시작할 수 없습니다."
    : "Preparing the execution route. Start is unavailable until preparation completes.";
  const integrationReadinessMessage = integrationReadinessCopy(
    integrationStartBlocker,
    integrationReadiness,
    activeBundle,
    vm.language,
  );
  const runtimeProfileMismatchMessage = vm.language === "ko"
    ? "표시된 실제 통합 모드와 실행 중인 런타임 프로필이 다릅니다. ‘현재 모드 시작’으로 올바른 런타임을 다시 시작하세요."
    : "The displayed Live mode does not match the running runtime profile. Restart the displayed mode through runtime control.";
  const effectiveIntegrationReadinessMessage = runtimeProfileMismatch
    ? runtimeProfileMismatchMessage
    : liveRouteInitializing
      ? routeInitializationMessage
      : integrationReadinessMessage;
  const liveStartControlBlocked = runtimeProfileMismatch || liveRouteInitializing;
  const liveResumeControlBlocked = runtimeMode === "live" && isPaused && liveStartControlBlocked;
  const runtimeModeLocked =
    !allowRuntimeModeSelection ||
    runtimeStatusChecking ||
    runtimeSwitchPending ||
    isRunning || isPaused || startInFlight || commandBusy;
  const disabled = !connected || !bundle || runtimeStatusChecking || runtimeSwitchPending;
  const formDisabled = disabled || commandBusy;
  const bundleRevisionBusy =
    scenarioRevision.phase === "previewing" || scenarioRevision.phase === "applying";
  const bundleSelectDisabled = formDisabled || bundleRevisionBusy;
  const phaseSelectDisabled = disabled || commandBusy || isRunning || startInFlight;
  const startDisabled = disabled || commandBusy || !runtimeReady || liveStartControlBlocked || isRunning || startInFlight;
  const pauseResumeDisabled =
    disabled || commandBusy || startInFlight || !canPauseResume || liveResumeControlBlocked;
  const resetDisabled = disabled || commandBusy || startInFlight;
  const stopDisabled =
    !connected || runtimeStatusChecking || runtimeSwitchPending || (!isRunning && !isPaused && !startInFlight && !commandBusy);
  const statusMessage = vm.runtime.statusMessage;
  const trimmedActionMessage = actionMessage.trim();
  const authorityFeedback = runtimeAuthorityCopy(runtimeAuthorityStatus, vm.language);
  const authorityNeedsAttention = runtimeAuthorityStatus === "blocked" || (!connected && (
    runtimeAuthorityStatus === "invalid" ||
    runtimeAuthorityStatus === "stale" ||
    runtimeAuthorityStatus === "offline"
  ));
  const isBridgeLifecycleMessage =
    trimmedActionMessage === "ROS bridge connected." ||
    trimmedActionMessage === "ROS bridge connected. Waiting for fresh runtime state..." ||
    trimmedActionMessage === "Connecting to ROS bridge..." ||
    trimmedActionMessage === "ROS bridge disconnected. Reconnecting..." ||
    trimmedActionMessage === "ROS bridge error. Retrying connection..." ||
    trimmedActionMessage === "Fresh runtime state did not arrive. Reconnecting to the ROS bridge..." ||
    trimmedActionMessage === "Runtime state heartbeat expired. Waiting for a fresh state..." ||
    trimmedActionMessage === "Runtime state payload was invalid. Waiting for a valid state..." ||
    trimmedActionMessage === "Replay state payload was invalid. Waiting for a valid state...";
  const displayedActionMessage = authorityNeedsAttention
    ? authorityFeedback.detail
    : trimmedActionMessage;
  const shouldShowActionMessage =
    authorityNeedsAttention || (
      Boolean(displayedActionMessage) &&
      displayedActionMessage !== "Ready." &&
      !isBridgeLifecycleMessage &&
      displayedActionMessage !== runtimeMessage
    );
  const actionMessageTone =
    authorityNeedsAttention ||
    /failed|error|cannot|unknown|unsupported|offline|timed out|expired|invalid|paused;|실패|오류|거부|거절|차단|만료|잠갔|끊어졌|유효하지/i.test(displayedActionMessage)
      ? "error"
      : actionPending
        ? "pending"
        : "normal";
  const allRuntimeModeOptions: Array<{
    id: MissionRuntimeMode;
    label: string;
    detail: string;
  }> =
    vm.language === "ko"
      ? [
          { id: "live", label: "실제 통합 모드", detail: "실시간 로봇 · 영상 · 음성" },
          { id: "llm", label: "LLM 집도의 모드", detail: "LLM 기반 검증 시뮬레이션" },
          { id: "shadow", label: "리플레이 (Shadow) 모드", detail: "기록 영상 재생 및 평가" },
        ]
      : [
          { id: "live", label: "Live integration", detail: "Live robot, vision, and speech" },
          { id: "llm", label: "LLM surgeon", detail: "LLM-driven validation simulation" },
          { id: "shadow", label: "Replay (Shadow)", detail: "Recorded replay and evaluation" },
        ];
  const runtimeModeOptions = allowRuntimeModeSelection
    ? allRuntimeModeOptions
    : allRuntimeModeOptions.filter((option) => option.id === "live");
  const displayedRuntimeMode = runtimeSwitchPending
    ? runtimeTransition.requestedMode ?? runtimeMode
    : runtimeMode;
  const selectedRuntimeMode =
    runtimeModeOptions.find((option) => option.id === displayedRuntimeMode) ??
    runtimeModeOptions[0];
  const apiTransitionMessage = runtimeTransition.message.trim();
  const noActiveRuntime =
    runtimeTransition.phase === "idle" && runtimeTransition.activeMode === null;
  const transitionCopy =
    runtimeTransition.phase === "checking"
      ? vm.language === "ko"
        ? "자동 시작 서비스를 확인하는 중입니다."
        : "Checking the runtime starter."
      : runtimeTransition.phase === "starting"
        ? runtimeSwitchDelayed
          ? vm.language === "ko"
            ? `${selectedRuntimeMode.label} 기동 응답을 ${runtimeSwitchElapsedSec}초째 기다리는 중입니다. 런처가 응답할 때까지 새 전환을 요청하지 않습니다.`
            : `Waiting ${runtimeSwitchElapsedSec}s for ${selectedRuntimeMode.label} to respond. No duplicate transition will be requested.`
          : vm.language === "ko"
            ? `${selectedRuntimeMode.label} 시작 중입니다. ROS 연결이 자동으로 재개됩니다.`
            : `Starting ${selectedRuntimeMode.label}. ROS will reconnect automatically.`
        : runtimeTransition.phase === "blocked"
          ? apiTransitionMessage || (vm.language === "ko"
            ? "현재 실행 상태를 안전하게 확인할 수 없어 런타임 전환이 차단되었습니다."
            : "The runtime switch was blocked because the active state could not be verified safely.")
        : runtimeTransition.phase === "failed"
          ? runtimeProfileMismatch
            ? vm.language === "ko"
              ? "실행 중인 컨테이너가 표시된 모드의 계약과 다릅니다. 자동으로 실제 장비 모드로 바꾸지 않았습니다. ‘현재 모드 시작’을 눌러 검증된 모드 전환을 수행하세요."
              : "The running container does not match the displayed runtime contract. It was not switched into a hardware mode automatically. Use Start displayed mode for a reviewed transition."
            : vm.language === "ko"
              ? apiTransitionMessage || "선택한 런타임을 시작하지 못했습니다. 다시 시도해 주세요."
              : apiTransitionMessage || "The selected runtime did not start. Please try again."
          : runtimeTransition.phase === "unavailable"
            ? vm.language === "ko"
              ? "자동 시작 서비스에 연결할 수 없습니다. 현재 런타임은 변경되지 않았습니다."
              : "The runtime starter is unavailable. The current runtime was not changed."
            : noActiveRuntime
              ? vm.language === "ko"
                ? "실행 중인 런타임이 없습니다. 표시된 모드를 시작할 수 있습니다."
                : "No runtime is active. You can start the displayed mode."
            : "";
  const transitionTone =
    runtimeTransition.phase === "blocked" ||
    runtimeTransition.phase === "failed" ||
    runtimeTransition.phase === "unavailable"
      ? "error"
      : runtimeTransition.phase === "starting" || runtimeTransition.phase === "checking"
        ? "pending"
        : noActiveRuntime
          ? "error"
        : "";
  const operationBusy = runtimeSwitchPending || commandBusy;
  const operationLabel = runtimeSwitchPending
    ? vm.language === "ko" ? "런타임을 안전하게 전환하는 중" : "Switching runtime safely"
    : vm.language === "ko" ? "제어 요청 결과를 확인하는 중" : "Waiting for control result";
  return (
    <>
      <aside
        aria-busy={operationBusy}
        className="dock procedure-dock"
        data-slot="procedure-dock"
        id="mission-controls"
      >
        <div className="dock-header">
          <div>
            <p className="section-kicker">{vm.ui.currentState}</p>
            <h2>{vm.runtime.stateLabel}</h2>
            {statusMessage ? <span className="dock-inline-status">{statusMessage}</span> : null}
          </div>
          <GitBranch aria-hidden="true" size={18} />
        </div>

      {shouldShowActionMessage ? (
        <div
          aria-atomic="true"
          aria-live={actionMessageTone === "error" ? "assertive" : "polite"}
          className={["dock-action-message", actionMessageTone].join(" ")}
          role={actionMessageTone === "error" ? "alert" : "status"}
        >
          {displayedActionMessage}
        </div>
      ) : null}

      {runtimeMode === "live" ? (
        <>
          <div
            aria-atomic="true"
            aria-live="polite"
            className={[
              "dock-action-message",
              "integration-readiness-message",
              liveStartControlBlocked ? "error" : "pending",
            ].join(" ")}
            data-slot="integration-readiness"
            id="integration-readiness-status"
            role="status"
          >
            {effectiveIntegrationReadinessMessage}
          </div>
          <IntegrationPreflightPanel
            blocker={integrationStartBlocker}
            language={vm.language}
            readiness={integrationReadiness}
            selectedBundle={bundle}
          />
          {executionRoute ? (
            <ExecutionRouteSelector
              connected={connected}
              currentSource={executionRoute.currentSource}
              currentRetractionSource={executionRoute.currentRetractionSource}
              disabledReason={executionRoute.disabledReason}
              language={vm.language}
              onSourcesChange={executionRoute.onSourcesChange}
              procedureStopped={executionRoute.procedureStopped}
              sourceReadiness={executionRoute.sourceReadiness}
              sourceHealth={executionRoute.sourceHealth}
              transitionMessage={executionRoute.transitionMessage}
              transitionState={executionRoute.transitionState}
            />
          ) : null}
        </>
      ) : null}

      {operationBusy ? (
        <div
          aria-label={operationLabel}
          aria-valuetext={operationLabel}
          className="operation-progress"
          role="progressbar"
        >
          <span aria-hidden="true" className="operation-progress-bar" />
        </div>
      ) : null}

      <div className="control-stack">
        <label className="field">
          <span>{vm.language === "ko" ? "실행 모드" : "Runtime mode"}</span>
          <div className="runtime-mode-select">
            <RadioTower size={16} aria-hidden="true" />
            <select
              value={displayedRuntimeMode}
              aria-describedby={runtimeModeLocked ? "runtime-mode-lock-note" : undefined}
              disabled={runtimeModeLocked}
              onChange={(event) => void onRuntimeModeChange(event.target.value as MissionRuntimeMode)}
            >
              {runtimeModeOptions.map((option) => (
                <option value={option.id} key={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
            <i className={runtimeSwitchPending ? "starting" : connected ? "connected" : "offline"}>
              {runtimeSwitchPending
                ? vm.language === "ko"
                  ? "기동 중"
                  : "Starting"
                : connected
                ? vm.language === "ko"
                  ? "연결"
                  : "Online"
                : vm.language === "ko"
                  ? "대기"
                  : "Offline"}
            </i>
          </div>
          <small className="runtime-mode-detail">{selectedRuntimeMode.detail}</small>
          {runtimeModeLocked ? (
            <small className="runtime-mode-lock-note" id="runtime-mode-lock-note" role="status">
              {!allowRuntimeModeSelection
                ? vm.language === "ko"
                  ? "Production은 실제 통합 모드로 고정됩니다. LLM·Replay는 별도 Lab 프로필에서만 사용할 수 있습니다."
                  : "Production is fixed to Live integration. LLM and Replay are available only in separate Lab profiles."
                : runtimeStatusChecking
                ? vm.language === "ko"
                  ? "자동 시작 서비스의 현재 런타임을 확인한 뒤 모드 변경을 활성화합니다."
                  : "Runtime switching will be enabled after the starter confirms the active runtime."
                : commandBusy
                ? vm.language === "ko"
                  ? "현재 제어 요청의 결과를 확인할 때까지 실행 모드를 바꿀 수 없습니다."
                  : "Runtime switching is locked until the current control request finishes."
                : vm.language === "ko"
                  ? "진행 상태를 보존하기 위해 실행 중·일시정지 상태에서는 모드를 바꿀 수 없습니다. 먼저 실행을 정지해 주세요."
                  : "Runtime switching is locked while running or paused to preserve progress. Stop the run first."}
            </small>
          ) : null}
          <small className="runtime-endpoint" title={url}>
            <Wifi size={12} aria-hidden="true" />
            {url}
          </small>
          {transitionCopy ? (
            <div
              aria-live="polite"
              className={["runtime-transition-feedback", transitionTone].filter(Boolean).join(" ")}
              id="runtime-transition-status"
              role={transitionTone === "error" ? "alert" : "status"}
            >
              <span>{transitionCopy}</span>
              {runtimeTransition.retryable || noActiveRuntime ? (
                <button className="runtime-transition-retry" onClick={onRetryRuntimeMode} type="button">
                  {runtimeProfileMismatch || noActiveRuntime
                    ? vm.language === "ko"
                      ? "현재 모드 시작"
                      : "Start displayed mode"
                    : vm.language === "ko"
                      ? "다시 시도"
                      : "Retry"}
                </button>
              ) : null}
            </div>
          ) : null}
        </label>

        <label className="field">
          <span>{vm.ui.surgery}</span>
          <select
            aria-describedby="bundle-revision-selection-note"
            value={bundle}
            disabled={bundleSelectDisabled}
            onChange={(event) => onBundleChange(event.target.value)}
          >
            {vm.bundleOptions.map((option) => (
              <option value={option.id} key={option.id}>
                {option.label}
              </option>
            ))}
          </select>
          <small
            className="runtime-mode-detail"
            id="bundle-revision-selection-note"
            role="status"
          >
            {vm.language === "ko"
              ? "선택은 실행 상태를 바꾸지 않습니다. 아래에서 revision을 확인한 뒤 서버가 허용한 상태에서만 적용하세요."
              : "Selection does not change runtime state. Preview the revision below, then apply only when the server contract allows it."}
          </small>
        </label>

        <Suspense fallback={<div role="status">{vm.language === "ko" ? "Revision 준비 중" : "Loading revision"}</div>}>
          <ScenarioRevisionControl
            activeBundle={activeBundle}
            admission={scenarioRevisionAdmission}
            connected={connected}
            language={vm.language}
            onApply={onApplyBundle}
            onPreview={onPreviewBundle}
            revision={scenarioRevision}
            selectedBundle={bundle}
          />
        </Suspense>

        <label className="field">
          <span>{vm.ui.startPhase}</span>
          <select
            value={startPhase}
            disabled={phaseSelectDisabled}
            onChange={(event) => setStartPhase(event.target.value)}
          >
            <option value="">{vm.ui.fromBeginning}</option>
            {vm.stage.phaseSteps.map((phase, index) => (
              <option value={phase.id} key={phase.id}>
                {index + 1}. {phase.label}
              </option>
            ))}
          </select>
        </label>

        {runtimeMode === "live" ? (
          <section
            aria-busy={Boolean(rosbagRecordingControlPending)}
            className={[
              "rosbag-recording-control",
              rosbagRecordingActive ? "is-recording" : "",
              rosbagRecording?.state === "failed" ? "is-failed" : "",
            ].filter(Boolean).join(" ")}
            data-slot="rosbag-recording-control"
          >
            <div className="rosbag-recording-heading">
              <div>
                <p>ROSbag2 · MCAP</p>
                <strong>{rosbagRecordingStatusCopy}</strong>
              </div>
              <span aria-label={rosbagRecordingStatusCopy} data-state={rosbagRecording?.state ?? "unknown"}>
                {rosbagRecordingActive
                  ? vm.language === "ko" ? "수동 녹화" : "Manual recording"
                  : vm.language === "ko" ? "독립 제어" : "Independent control"}
              </span>
            </div>
            <p className="rosbag-recording-note" id="rosbag-recording-note">
              {vm.language === "ko"
                ? "표시되는 ROS 상태·영상은 기록됩니다. 화면 픽셀 캡처가 아니며, 재생에는 동일 앱 빌드가 필요합니다."
                : "Visible ROS state and video are recorded, not screen pixels. Replay needs the same app build."}
            </p>
            <button
              aria-describedby="rosbag-recording-note rosbag-recording-status"
              aria-pressed={rosbagRecordingActive}
              className={[
                "button",
                "rosbag-recording-toggle",
                rosbagRecordingActive ? "button-stop" : "button-secondary",
              ].join(" ")}
              disabled={rosbagRecordingDisabled}
              onClick={() => void onRosbagRecordingControl(!rosbagRecordingActive)}
              type="button"
            >
              {rosbagRecordingActive ? (
                <Square aria-hidden="true" size={16} />
              ) : (
                <RadioTower aria-hidden="true" size={16} />
              )}
              {rosbagRecordingControlPending
                ? vm.language === "ko"
                  ? rosbagRecordingControlPending === "start" ? "녹화 시작 중" : "저장 중"
                  : rosbagRecordingControlPending === "start" ? "Starting recording" : "Saving recording"
                : rosbagRecordingActive
                  ? vm.language === "ko" ? "녹화 종료" : "Stop recording"
                  : vm.language === "ko" ? "녹화 시작" : "Start recording"}
            </button>
            <div
              aria-atomic="true"
              aria-live={rosbagRecording?.state === "failed" ? "assertive" : "polite"}
              className="rosbag-recording-status"
              id="rosbag-recording-status"
              role={rosbagRecording?.state === "failed" ? "alert" : "status"}
            >
              <span>{rosbagRecordingDetail}</span>
              {rosbagRecording?.outputDir ? (
                <code title={rosbagRecording.outputDir}>{rosbagRecording.outputDir}</code>
              ) : null}
            </div>
          </section>
        ) : null}
      </div>

        <div className="transport-controls" aria-label={vm.ui.control}>
        <button
          aria-describedby={runtimeProfileMismatch
            ? "runtime-transition-status"
            : liveStartControlBlocked ? "integration-readiness-status" : undefined}
          className="button button-primary"
          disabled={startDisabled}
          onClick={() => onControl("start")}
          title={liveStartControlBlocked ? effectiveIntegrationReadinessMessage : undefined}
          type="button"
        >
          <Play aria-hidden="true" size={17} />
          {runtimeProfileMismatch
            ? vm.language === "ko" ? "런타임 복구 필요" : "Runtime recovery required"
            : runtimeReady && !liveStartControlBlocked ? vm.ui.start : vm.ui.preparing}
        </button>
        <button
          className="button button-secondary"
          disabled={pauseResumeDisabled}
          aria-describedby={liveResumeControlBlocked ? "integration-readiness-status" : undefined}
          onClick={() => onControl(isPaused ? "resume" : "pause")}
          title={liveResumeControlBlocked ? effectiveIntegrationReadinessMessage : undefined}
          type="button"
        >
          {isPaused ? <Play aria-hidden="true" size={17} /> : <Pause aria-hidden="true" size={17} />}
          {isPaused ? vm.ui.resume : vm.ui.pause}
        </button>
        <button className="button button-secondary" disabled={resetDisabled} onClick={() => setResetConfirmationOpen(true)} type="button">
          <RotateCcw aria-hidden="true" size={17} />
          {vm.ui.reset}
        </button>
        <button
          aria-describedby={runtimeMode === "live" ? "live-stop-scope-note" : undefined}
          className="button button-stop"
          disabled={stopDisabled}
          onClick={() => onControl("stop")}
          type="button"
        >
          <Square aria-hidden="true" size={16} />
          {runtimeMode === "live"
            ? vm.language === "ko" ? "실행 중지" : "Stop planner execution"
            : vm.ui.stop}
        </button>
        </div>
        {runtimeMode === "live" ? (
          <small className="runtime-mode-lock-note" id="live-stop-scope-note" role="status">
            {vm.language === "ko"
              ? "‘실행 중지’는 태스크플래너의 실행 요청을 중단합니다. 이미 외부 Service가 수락한 로봇 동작의 실물 정지는 장비의 독립 안전 절차로 수행하고 상태를 확인하세요."
              : "Stop planner execution halts Taskplanner requests. Use the device's independent safety procedure and verify its state for any externally accepted robot motion."}
          </small>
        ) : null}
      </aside>
      <SafetyConfirmationDialog
        closeLabel={vm.language === "ko" ? "닫기" : "Close"}
        confirmLabel={vm.language === "ko" ? "실행 상태 초기화" : "Reset run state"}
        description={
          vm.language === "ko"
            ? "현재 실행 진행 상태를 지우고 선택한 시작 단계의 초기 상태로 되돌립니다. 이 작업은 되돌릴 수 없습니다."
            : "Clear the current run progress and return to the initial state for the selected start phase. This cannot be undone."
        }
        note={
          vm.language === "ko"
            ? "즉시 정지가 필요하면 이 창을 닫고 ‘정지’를 사용하세요. 정지는 확인 없이 바로 요청됩니다."
            : "For an immediate halt, close this dialog and use Stop. Stop is requested without confirmation."
        }
        onClose={() => setResetConfirmationOpen(false)}
        onConfirm={() => onControl("reset")}
        open={resetConfirmationOpen}
        title={vm.language === "ko" ? "실행 상태를 초기화할까요?" : "Reset the run state?"}
      />
    </>
  );
}
