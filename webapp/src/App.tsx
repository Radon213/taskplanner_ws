import {
  Component,
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ErrorInfo,
  type ReactNode,
} from "react";
import { useReducedMotion } from "framer-motion";
import * as m from "framer-motion/m";

import { ProcedureDock } from "./components/command/ProcedureDock";
import { LiveAsrPanel } from "./components/command/LiveAsrPanel";
import {
  type HandHandoverSignal,
} from "./components/command/HandHandoverSignalStatus";
import { StatusRibbon } from "./components/command/StatusRibbon";
import { TypedRfdetrObservationStatus } from "./components/observability/TypedRfdetrObservationStatus";
import {
  deriveVlmOperationObservations,
  executionDispatchEventFromTrace,
  latestActualDispatch,
  OperationExecutionDispatchFeed,
  type ExecutionDispatchEvent,
} from "./components/observability/OperationVlmObservability";
import { useDigitalTwinViewModel } from "./hooks/useDigitalTwinViewModel";
import { useRosBridge } from "./hooks/useRosBridge";
import { useRuntimeControl } from "./hooks/useRuntimeControl";
import {
  initialRuntimeMode,
  lastMissionModeStorageKey,
  persistRuntimeMode,
  runtimeBridgeUrl,
  type TaskplannerRuntimeMode,
} from "./runtimeModes";
import {
  missionObservationProfile,
  OPTIONAL_OPERATIONS_UI_ENABLED,
  optionalOperationsUiEnabled,
  runtimeModeIsAvailable,
} from "./runtimeFeatures";
import { type Language } from "./utils/display";
import { shimmer } from "./motion-system";

type PrimaryWorkspace = "mission" | "monitor" | "multicam" | "debug";
type WorkspaceHistoryAction = "push" | "replace" | "none";
type MissionRuntimeMode = Exclude<TaskplannerRuntimeMode, "debug">;
type RuntimeTransitionSafety = {
  isRunning: boolean;
  isPaused: boolean;
  startInFlight: boolean;
  actionPending: boolean;
};

const DebugWorkspace = lazy(() =>
  import("./components/debug/DebugWorkspace").then((module) => ({
    default: module.DebugWorkspace,
  })),
);
const MulticamOpsWorkspace = lazy(() =>
  import("./components/multicam/MulticamOpsWorkspace").then((module) => ({
    default: module.MulticamOpsWorkspace,
  })),
);
const SurgiMateMonitorWorkspace = lazy(() =>
  import("./components/monitor/SurgiMateMonitorWorkspace").then((module) => ({
    default: module.SurgiMateMonitorWorkspace,
  })),
);
const ShadowReplayDock = lazy(() =>
  import("./components/command/ShadowReplayDock").then((module) => ({
    default: module.ShadowReplayDock,
  })),
);
const SurgeonIntentDock = lazy(() =>
  import("./components/command/SurgeonIntentDock").then((module) => ({
    default: module.SurgeonIntentDock,
  })),
);
const ObservabilityPanel = lazy(() =>
  import("./components/observability/ObservabilityPanel").then((module) => ({
    default: module.ObservabilityPanel,
  })),
);
const OperatingRoomStage = lazy(() =>
  import("./components/stage/OperatingRoomStage").then((module) => ({
    default: module.OperatingRoomStage,
  })),
);
const VlmStructuredToolDetectionEvidencePanel = lazy(() =>
  import("./components/observability/VlmStructuredToolDetectionEvidencePanel").then((module) => ({
    default: module.VlmStructuredToolDetectionEvidencePanel,
  })),
);

function WorkspaceLoading({
  label,
  error = "",
  onRetry,
  retryLabel = "Retry",
  onExit,
  exitLabel = "Back",
}: {
  label: string;
  error?: string;
  onRetry?: () => void;
  retryLabel?: string;
  onExit?: () => void;
  exitLabel?: string;
}) {
  const reduceMotion = useReducedMotion();
  const shimmerMotion = reduceMotion ? {} : shimmer;
  return (
    <div className="app-shell" data-slot="workspace-loading-state">
      <main aria-busy={!error} aria-live="polite" className="debug-main" id="workspace-loading-main">
        <section className="debug-feedback-card debug-loading-card" role={error ? "alert" : "status"}>
          {error ? (
            <div className="runtime-transition-feedback error">
              <span>{error}</span>
              {onExit ? (
                <button className="runtime-transition-retry" onClick={onExit} type="button">
                  {exitLabel}
                </button>
              ) : null}
              {onRetry ? (
                <button className="runtime-transition-retry" onClick={onRetry} type="button">
                  {retryLabel}
                </button>
              ) : null}
            </div>
          ) : (
            <>
              <m.div className="debug-skeleton-title" {...shimmerMotion} />
              <m.div className="debug-skeleton-row" {...shimmerMotion} />
              <m.div className="debug-skeleton-row short" {...shimmerMotion} />
              <span className="sr-only">{label}</span>
              {onExit ? (
                <button className="runtime-transition-retry" onClick={onExit} type="button">
                  {exitLabel}
                </button>
              ) : null}
            </>
          )}
        </section>
      </main>
    </div>
  );
}

class WorkspaceErrorBoundary extends Component<{
  children: ReactNode;
  errorMessage: string;
  reloadLabel: string;
  onExit?: () => void;
  exitLabel?: string;
}, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Workspace chunk failed to load", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <WorkspaceLoading
          error={this.props.errorMessage}
          label={this.props.errorMessage}
          onRetry={() => window.location.reload()}
          retryLabel={this.props.reloadLabel}
          onExit={this.props.onExit}
          exitLabel={this.props.exitLabel}
        />
      );
    }
    return this.props.children;
  }
}

function workspaceFromLocation(optionalUiEnabled: boolean): PrimaryWorkspace {
  if (typeof window === "undefined") return "mission";
  if (!optionalUiEnabled) return "mission";
  const pathname = window.location.pathname.replace(/\/+$/, "") || "/";
  if (pathname === "/debug") return "debug";
  const requested = new URLSearchParams(window.location.search).get("workspace");
  return requested === "monitor" || requested === "multicam" || requested === "debug"
    ? requested
    : "mission";
}

export default function App() {
  const [runtimeMode, setRuntimeMode] = useState<TaskplannerRuntimeMode>(initialRuntimeMode);
  const {
    status: runtimeTransition,
    refresh: refreshRuntimeControl,
    requestTransition,
  } = useRuntimeControl();
  const optionalUiEnabled = optionalOperationsUiEnabled(runtimeTransition.activeMode);
  const [workspace, setWorkspace] = useState<PrimaryWorkspace>(() =>
    workspaceFromLocation(OPTIONAL_OPERATIONS_UI_ENABLED),
  );
  const previousWorkspaceRef = useRef<PrimaryWorkspace>(workspace);
  const [language, setLanguage] = useState<Language>(() => {
    if (typeof window === "undefined") return "ko";
    return window.localStorage.getItem("taskplanner.language") === "en" ? "en" : "ko";
  });
  const [lastMissionMode, setLastMissionMode] = useState<MissionRuntimeMode>(() => {
    if (!OPTIONAL_OPERATIONS_UI_ENABLED) return "live";
    if (typeof window === "undefined") return "live";
    const stored = window.localStorage.getItem(lastMissionModeStorageKey());
    return stored === "live" || stored === "llm" || stored === "shadow" ? stored : "live";
  });

  useEffect(() => {
    window.localStorage.setItem("taskplanner.language", language);
    document.documentElement.lang = language;
  }, [language]);

  useEffect(() => {
    persistRuntimeMode(runtimeMode);
    if (runtimeMode !== "debug" && runtimeModeIsAvailable(runtimeMode)) {
      setLastMissionMode(runtimeMode);
      window.localStorage.setItem(lastMissionModeStorageKey(), runtimeMode);
    }
  }, [runtimeMode]);

  useEffect(() => {
    if (
      (workspace !== "mission" && workspace !== "debug") ||
      runtimeTransition.phase !== "idle" ||
      runtimeTransition.activeMode === null ||
      runtimeTransition.activeMode === runtimeMode
    ) {
      return;
    }
    setRuntimeMode(runtimeTransition.activeMode);
  }, [runtimeMode, runtimeTransition.activeMode, runtimeTransition.phase, workspace]);

  const navigateWorkspace = useCallback((
    next: PrimaryWorkspace,
    historyAction: WorkspaceHistoryAction = "push",
  ) => {
    const availableNext = next === "mission" || optionalUiEnabled
      ? next
      : "mission";
    if (typeof window !== "undefined") {
      const location = new URL(window.location.href);
      if (location.pathname.replace(/\/+$/, "") === "/debug") location.pathname = "/";
      if (availableNext !== "mission") location.searchParams.set("workspace", availableNext);
      else location.searchParams.delete("workspace");
      const currentState = typeof window.history.state === "object" && window.history.state !== null
        ? window.history.state
        : {};
      const nextState = {
        ...currentState,
        taskplannerWorkspaceEntry: availableNext === "mission" ? null : availableNext,
      };
      if (historyAction === "push") window.history.pushState(nextState, "", location);
      if (historyAction === "replace") window.history.replaceState(nextState, "", location);
    }
    setWorkspace(availableNext);
  }, [optionalUiEnabled]);

  const exitMonitorWorkspace = useCallback(() => {
    if (typeof window !== "undefined" && window.history.state?.taskplannerWorkspaceEntry === "monitor") {
      window.history.back();
      return;
    }
    navigateWorkspace("mission", "replace");
  }, [navigateWorkspace]);

  const exitMulticamWorkspace = useCallback(() => {
    if (typeof window !== "undefined" && window.history.state?.taskplannerWorkspaceEntry === "multicam") {
      window.history.back();
      return;
    }
    navigateWorkspace("mission", "replace");
  }, [navigateWorkspace]);

  const exitIntegratedDebugWorkspace = useCallback(() => {
    if (typeof window !== "undefined" && window.history.state?.taskplannerWorkspaceEntry === "debug") {
      window.history.back();
      return;
    }
    navigateWorkspace("mission", "replace");
  }, [navigateWorkspace]);

  useEffect(() => {
    const onPopState = () => {
      navigateWorkspace(workspaceFromLocation(optionalUiEnabled), "none");
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [navigateWorkspace, optionalUiEnabled]);

  useEffect(() => {
    if (!optionalUiEnabled && workspace !== "mission") {
      navigateWorkspace("mission", "replace");
    }
  }, [navigateWorkspace, optionalUiEnabled, workspace]);

  useEffect(() => {
    const previousWorkspace = previousWorkspaceRef.current;
    previousWorkspaceRef.current = workspace;
    if (previousWorkspace === workspace) return;
    const targetId = workspace === "mission"
      ? "mission-main"
      : workspace === "multicam"
        ? "multicam-main"
        : workspace === "monitor"
          ? "surgimate-monitor-main"
          : "";
    const focusFrame = window.requestAnimationFrame(() => {
      if (workspace === "debug") {
        document.querySelector<HTMLElement>("[data-slot='debug-workspace'] .debug-main")?.focus({ preventScroll: true });
        return;
      }
      document.getElementById(targetId)?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(focusFrame);
  }, [workspace]);

  useEffect(() => {
    if (workspace === "debug" && runtimeMode !== "live" && runtimeMode !== "debug") {
      navigateWorkspace("mission", "replace");
    }
  }, [navigateWorkspace, runtimeMode, workspace]);

  const requestRuntimeMode = useCallback(async (
    mode: TaskplannerRuntimeMode,
    safety?: RuntimeTransitionSafety,
  ) => {
    if (mode !== "live" && !optionalUiEnabled) return false;
    if (
      runtimeTransition.phase === "checking" ||
      runtimeTransition.phase === "starting" ||
      safety?.isRunning ||
      safety?.isPaused ||
      safety?.startInFlight ||
      safety?.actionPending
    ) {
      return;
    }
    if (
      mode === runtimeMode &&
      runtimeTransition.activeMode === mode &&
      runtimeTransition.phase !== "failed"
    ) {
      void refreshRuntimeControl();
      return;
    }
    return requestTransition(mode);
  }, [
    refreshRuntimeControl,
    requestTransition,
    runtimeMode,
    runtimeTransition.activeMode,
    runtimeTransition.phase,
    optionalUiEnabled,
  ]);

  if (optionalUiEnabled && workspace === "monitor") {
    return (
      <WorkspaceErrorBoundary
        key="monitor-workspace-boundary"
        errorMessage={language === "ko" ? "수술 관제 화면을 불러오지 못했습니다." : "Could not load SurgiMate monitoring."}
        reloadLabel={language === "ko" ? "페이지 다시 불러오기" : "Reload page"}
        onExit={exitMonitorWorkspace}
        exitLabel={language === "ko" ? "미션 화면" : "Mission"}
      >
        <Suspense
          fallback={(
            <WorkspaceLoading
              label={language === "ko" ? "수술 관제 화면을 불러오는 중입니다." : "Loading SurgiMate monitoring."}
              onExit={exitMonitorWorkspace}
              exitLabel={language === "ko" ? "미션 화면" : "Mission"}
            />
          )}
        >
          <SurgiMateMonitorWorkspace language={language} onExit={exitMonitorWorkspace} />
        </Suspense>
      </WorkspaceErrorBoundary>
    );
  }

  if (optionalUiEnabled && workspace === "multicam") {
    return (
      <WorkspaceErrorBoundary
        key="multicam-workspace-boundary"
        errorMessage={language === "ko" ? "멀티캠 관제 화면을 불러오지 못했습니다." : "Could not load multicamera operations."}
        reloadLabel={language === "ko" ? "페이지 다시 불러오기" : "Reload page"}
      >
        <Suspense fallback={<WorkspaceLoading label={language === "ko" ? "멀티캠 관제 화면을 불러오는 중입니다." : "Loading multicamera operations."} />}>
          <MulticamOpsWorkspace language={language} onExit={exitMulticamWorkspace} />
        </Suspense>
      </WorkspaceErrorBoundary>
    );
  }

  if (
    optionalUiEnabled &&
    (runtimeMode === "debug" || (workspace === "debug" && runtimeMode === "live"))
  ) {
    return (
      <WorkspaceErrorBoundary
        key="debug-workspace-boundary"
        errorMessage={language === "ko" ? "디버그 화면을 불러오지 못했습니다." : "Could not load the Debug workspace."}
        reloadLabel={language === "ko" ? "페이지 다시 불러오기" : "Reload page"}
        onExit={runtimeMode === "debug"
          ? () => void requestRuntimeMode(lastMissionMode)
          : exitIntegratedDebugWorkspace}
        exitLabel={language === "ko" ? "운영 화면" : "Operational view"}
      >
        <Suspense
          fallback={(
            <WorkspaceLoading
              label={language === "ko" ? "디버그 화면을 불러오는 중입니다." : "Loading debug workspace."}
              onExit={runtimeMode === "debug"
                ? () => void requestRuntimeMode(lastMissionMode)
                : exitIntegratedDebugWorkspace}
              exitLabel={language === "ko" ? "운영 화면" : "Operational view"}
            />
          )}
        >
          <DebugWorkspace
            language={language}
            onExit={runtimeMode === "debug"
              ? () => void requestRuntimeMode(lastMissionMode)
              : exitIntegratedDebugWorkspace}
          />
        </Suspense>
      </WorkspaceErrorBoundary>
    );
  }

  return (
    // Mission is the default entry point, so keep a render/HMR failure inside
    // the same recoverable surface as the lazy workspaces instead of leaving a
    // blank page with no way back to the planner.
    <WorkspaceErrorBoundary
      key="mission-workspace-boundary"
      errorMessage={language === "ko" ? "미션 화면을 표시하지 못했습니다." : "Could not render the Mission workspace."}
      reloadLabel={language === "ko" ? "페이지 다시 불러오기" : "Reload page"}
    >
      <MissionWorkspace
        runtimeMode={runtimeMode === "debug" ? "live" : runtimeMode}
        onRuntimeModeChange={requestRuntimeMode}
        runtimeTransition={runtimeTransition}
        language={language}
        onLanguageChange={setLanguage}
        optionalUiEnabled={optionalUiEnabled}
        onMonitor={optionalUiEnabled
          ? () => navigateWorkspace("monitor")
          : undefined}
        onIntegratedDebug={optionalUiEnabled
          ? () => navigateWorkspace("debug")
          : undefined}
      />
    </WorkspaceErrorBoundary>
  );
}

function MissionWorkspace({
  runtimeMode,
  onRuntimeModeChange,
  runtimeTransition,
  language,
  onLanguageChange,
  optionalUiEnabled,
  onMonitor,
  onIntegratedDebug,
}: {
  runtimeMode: Exclude<TaskplannerRuntimeMode, "debug">;
  onRuntimeModeChange: (
    mode: TaskplannerRuntimeMode,
    safety?: RuntimeTransitionSafety,
  ) => void | boolean | Promise<void | boolean>;
  runtimeTransition: ReturnType<typeof useRuntimeControl>["status"];
  language: Language;
  onLanguageChange: (language: Language) => void;
  optionalUiEnabled: boolean;
  onMonitor?: () => void;
  onIntegratedDebug?: () => void;
}) {
  const rosBridgeReady =
    runtimeTransition.phase === "idle" &&
    runtimeTransition.activeMode === runtimeMode;
  const runtimeProfileMismatch =
    runtimeTransition.diagnosticCode === "runtime_profile_mismatch";
  const ros = useRosBridge(
    runtimeMode,
    rosBridgeReady,
    runtimeTransition.phase === "checking" || runtimeTransition.phase === "starting",
    runtimeProfileMismatch,
    missionObservationProfile(runtimeTransition.activeMode),
  );
  const [stageAspectRatio, setStageAspectRatio] = useState(1.55);

  useEffect(() => {
    const nextUrl = runtimeBridgeUrl(runtimeMode);
    if (ros.url !== nextUrl) {
      ros.setUrl(nextUrl);
    }
  }, [runtimeMode, ros.url]);

  const vm = useDigitalTwinViewModel({
    language,
    activeBundle: ros.activeBundle,
    simulationState: ros.simulationState,
    bedRobotArms: ros.bedRobotArms,
    skillStatus: ros.skillStatus,
    surgeonState: ros.surgeonState,
    events: ros.events,
    vlmHealth: ros.vlmHealth,
    vlmResult: ros.vlmResult,
    vlmHealthReceivedAt: ros.vlmHealthReceivedAt,
    vlmResultReceivedAt: ros.vlmResultReceivedAt,
    stageAspectRatio,
  });
  const handHandoverSignal = useMemo<HandHandoverSignal>(
    () => ({
      active: ros.worldState.implicit_request_visible,
      handPose: ros.worldState.implicit_request_hand_pose,
      confidence: ros.worldState.implicit_request_confidence,
      stabilitySec: ros.worldState.implicit_request_stability_sec,
      generation: ros.worldState.implicit_request_generation,
    }),
    [ros.worldState],
  );
  const vlmOperationObservations = useMemo(
    () => deriveVlmOperationObservations(ros.vlmResult),
    [ros.vlmResult],
  );
  const executionDispatchEvents = useMemo<ExecutionDispatchEvent[]>(
    () => ros.executionTraces.flatMap((trace) => {
      const commandId = trace.command_id.trim();
      const status = commandId ? ros.skillStatusByCommand[commandId] : undefined;
      const toolId = status?.instrument_id?.trim() ?? "";
      const event = executionDispatchEventFromTrace(trace, language, {
        toolId,
        toolLabel: toolId ? vm.displayToolName(toolId) : "",
        toolInstanceId: status?.instrument_instance_id,
        sourceLocationId: status?.source_location_id,
        sourceLocationType: status?.source_location_type,
        targetLocationId: status?.target_location_id,
        targetLocationType: status?.target_location_type,
        targetOwner: status?.target_owner,
      });
      return event ? [event] : [];
    }),
    [language, ros.executionTraces, ros.skillStatusByCommand, vm],
  );
  const latestExecutionDispatch = useMemo(
    () => latestActualDispatch(executionDispatchEvents),
    [executionDispatchEvents],
  );
  const fusedSurgeonRequest = useMemo(
    () => ({
      confirmed:
        ros.worldState.running &&
        Boolean(ros.worldState.surgeon_request_tool),
      requestedTool: ros.worldState.surgeon_request_tool,
    }),
    [
      ros.worldState.running,
      ros.worldState.surgeon_request_tool,
    ],
  );
  const asrFinalSentence = useMemo(() => {
    const finals = ros.liveAsrStatus.finals;
    const final = finals[finals.length - 1];
    const text = final?.text.trim() ?? "";
    if (!text) return null;
    // A final ASR transcript is observer evidence only.  It is intentionally
    // not derived from a proposed/rejected intent and does not imply dispatch.
    const stamp = final?.stamp.trim() ?? "";
    return {
      eventKey: stamp ? `asr:${stamp}:${text}` : `asr-text:${text}`,
      text,
    };
  }, [ros.liveAsrStatus.finals]);

  useEffect(() => {
    const runtimeBusy =
      ros.simulationState.running ||
      ["starting", "running", "finishing"].includes(
        ros.simulationState.execution_state,
      );
    if (!runtimeBusy && !ros.actionPending && vm.defaultStartPhaseId) {
      ros.setStartPhase(vm.defaultStartPhaseId);
    }
  }, [
    ros.actionPending,
    ros.activeBundle,
    ros.simulationState.execution_state,
    ros.simulationState.running,
    vm.defaultStartPhaseId,
  ]);

  const shadowTransportActive =
    ros.shadowReplayState.running || ros.shadowReplayState.paused;
  const controlIsRunning =
    runtimeMode === "shadow"
      ? shadowTransportActive || ros.simulationState.running
      : ros.simulationState.running;
  const controlIsPaused =
    runtimeMode === "shadow"
      ? ros.shadowReplayState.paused
      : ros.simulationState.execution_state === "paused";
  const controlCanPauseResume =
    runtimeMode === "shadow"
      ? shadowTransportActive
      : ros.simulationState.running ||
        ros.simulationState.execution_state === "paused";
  const controlStartInFlight =
    ros.simulationState.execution_state === "starting" ||
    ros.actionPending.toLowerCase().includes("starting");
  // Server-route selection has its own stopped-state contract.  A completed
  // procedure is stopped and the ROS coordinator accepts it; do not inherit
  // the stricter bundle-picker affordance here.
  const liveExecutionRouteStopped =
    !ros.simulationState.running &&
    ["idle", "halted", "completed", "terminated"].includes(
      ros.simulationState.execution_state.trim().toLowerCase(),
    ) &&
    ros.simulationState.robot_state.trim().toLowerCase() === "idle" &&
    !ros.simulationState.active_robot_task_id &&
    !ros.simulationState.cleaner_busy &&
    ros.simulationState.pending_transition_tools.length === 0 &&
    ros.simulationState.active_recovery_tools.length === 0 &&
    !controlStartInFlight &&
    !ros.actionPending;
  const liveExecutionRouteInitializing =
    ros.executionRouteState?.initializationState === "initializing";
  const liveExecutionRouteSwitchAllowed =
    liveExecutionRouteStopped &&
    !controlIsRunning &&
    !controlIsPaused &&
    !ros.executionRouteState?.runEndpointSource &&
    !liveExecutionRouteInitializing;
  const executionRouteSourceReadiness = useMemo(() => {
    // This describes whether the server has enabled the stopped-only selector,
    // not remote Action/Service health. A fresh integration preflight remains
    // the authoritative readiness check after either source is selected.
    const selectable = ros.executionRouteState?.routeControlEnabled === true;
    return {
      external: selectable ? "ready" as const : "unknown" as const,
      virtual: selectable ? "ready" as const : "unknown" as const,
    };
  }, [ros.executionRouteState?.routeControlEnabled]);
  const executionRouteSourceHealth =
    ros.executionRouteState?.sourceEndpointReadiness ?? {};
  const executionRouteDisabledReason = !ros.executionRouteState
    ? language === "ko"
      ? "실행 서버 상태를 기다리는 중입니다."
      : "Waiting for the execution-server state."
    : !ros.executionRouteState.routeControlEnabled
      ? language === "ko"
        ? "현재 실제 통합 런타임은 정지 상태 실행 서버 전환을 제공하지 않습니다."
        : "The current live runtime does not expose stopped-state execution-server switching."
    : liveExecutionRouteInitializing
      ? language === "ko"
        ? "새 실행 서버 경로가 통합 시작 점검에 적용되기를 기다리는 중입니다."
        : "Waiting for the new execution-server route to be applied to the integration start check."
      : !liveExecutionRouteSwitchAllowed
        ? language === "ko"
          ? "실행을 완전히 정지하고 활성 작업·정리 작업이 없어야 서버를 바꿀 수 있습니다."
          : "Stop execution completely and clear active or cleanup work before changing the server."
        : "";

  const beginControl = useCallback((command: Parameters<typeof ros.control>[0]) => {
    void ros.control(command);
  }, [ros]);
  const runtimeTransitionSafety: RuntimeTransitionSafety = {
    isRunning: controlIsRunning,
    isPaused: controlIsPaused,
    startInFlight: controlStartInFlight,
    actionPending: Boolean(ros.actionPending),
  };
  return (
    <div className="app-shell mission-app-shell" data-slot="mission-workspace">
      <a className="skip-link" href="#mission-main">
        {language === "ko" ? "미션 본문으로 이동" : "Skip to mission content"}
      </a>
      <StatusRibbon
        vm={vm}
        connected={ros.connected}
        transportConnected={ros.transportConnected}
        runtimeAuthorityStatus={ros.runtimeAuthorityStatus}
        runtimeTransitionPhase={runtimeTransition.phase}
        language={language}
        onLanguageChange={onLanguageChange}
        modelOptions={ros.vlmModelOptions}
        providerStatuses={ros.vlmProviderStatuses}
        modelCatalogStatus={ros.vlmModelCatalogStatus}
        modelSelection={ros.vlmModelSelection}
        actionPending={ros.actionPending}
        onVlmModelChange={(selection) => void ros.setVlmModel(selection)}
        onVlmRuntimeAction={(selection, command) =>
          void ros.controlVlmModelRuntime(selection, command)
        }
        experimentalControlsEnabled={optionalUiEnabled}
        integratedDebugAvailable={
          optionalUiEnabled &&
          runtimeMode === "live" &&
          runtimeTransition.activeMode === "live"
        }
        onIntegratedDebug={onIntegratedDebug}
        onMonitor={onMonitor}
      />

      <main
        className={`mission-layout ${runtimeMode === "live" ? "live-stage-expanded" : ""}`}
        id="mission-main"
        tabIndex={-1}
      >
        <div
          aria-label={language === "ko" ? "수술실 디지털 트윈 상세 보기" : "Operating room digital twin detail"}
          className="stage-area"
          role="region"
          tabIndex={0}
        >
          <Suspense
            fallback={(
              <div className="stage-lazy-loading" role="status" aria-live="polite">
                <span>{language === "ko" ? "수술실 보기를 준비하고 있습니다." : "Preparing the operating-room view."}</span>
              </div>
            )}
          >
            <OperatingRoomStage
              vm={vm}
              cameraFrames={{
                cam1: ros.cam1Image,
                cam2: ros.cam2Image,
                cam3: ros.cam3Image,
                cam4: ros.cam4Image,
                flir: ros.flirImage,
              }}
              typedRfdetrToolDetections={
                optionalUiEnabled ? ros.typedRfdetrToolDetections : undefined
              }
              systemSurgeonRequest={fusedSurgeonRequest}
              asrFinalSentence={asrFinalSentence}
              handHandoverSignal={handHandoverSignal}
              vlmObservations={vlmOperationObservations}
              systemToolPredictions={ros.worldState.ranked_tool_predictions}
              executionDispatch={latestExecutionDispatch}
              onStageAspectChange={(ratio) => {
                setStageAspectRatio((current) => (Math.abs(current - ratio) > 0.01 ? ratio : current));
              }}
            />
          </Suspense>
        </div>

        {runtimeMode !== "live" ? (
          <div className="surgeon-area">
            <Suspense fallback={null}>
            {runtimeMode === "shadow" ? (
              <ShadowReplayDock
                vm={vm}
                language={language}
                state={ros.shadowReplayState}
                transcript={ros.shadowTranscript}
                connected={ros.connected}
                runtimeAuthorityStatus={ros.runtimeAuthorityStatus}
                actionPending={ros.actionPending}
                groundTruth={ros.shadowGroundTruth}
                onCaseChange={(caseId) => void ros.selectShadowCase(caseId)}
                onConfigure={(mode, playbackRate) =>
                  void ros.configureShadowReplay(mode, playbackRate)
                }
              />
            ) : (
              <SurgeonIntentDock
                vm={vm}
                language={language}
                llmDecision={ros.surgeonLlmDecision}
                actorEnabled={ros.actorEnabled}
                actorEnabledKnown={ros.actorEnabledKnown}
                modelOptions={ros.actorModelOptions}
                providerStatuses={ros.actorProviderStatuses}
                modelCatalogStatus={ros.actorModelCatalogStatus}
                modelSelection={ros.actorModelSelection}
                connected={ros.connected}
                actionPending={ros.actionPending}
                handHandoverSignal={handHandoverSignal}
                onActorEnabledChange={(enabled) => void ros.setActorEnabled(enabled)}
                onActorModelChange={(selection) => void ros.setActorModel(selection)}
                onActorRuntimeAction={(selection, command) =>
                  void ros.controlActorModelRuntime(selection, command)
                }
              />
            )}
            </Suspense>
          </div>
        ) : null}

        <div className="runtime-area">
          <ProcedureDock
            vm={vm}
            url={ros.url}
            runtimeMode={runtimeMode}
            onRuntimeModeChange={(mode) => {
              void onRuntimeModeChange(mode, runtimeTransitionSafety);
            }}
            allowRuntimeModeSelection={optionalUiEnabled}
            runtimeTransition={runtimeTransition}
            onRetryRuntimeMode={() =>
              void onRuntimeModeChange(
                runtimeTransition.diagnosticCode === "runtime_profile_mismatch"
                  ? runtimeMode
                  : runtimeTransition.requestedMode ?? runtimeMode,
                runtimeTransitionSafety,
              )
            }
            bundle={ros.bundle}
            onBundleChange={(nextBundle) => {
              ros.setBundleSelection(nextBundle);
            }}
            activeBundle={ros.activeBundle}
            scenarioRevision={ros.scenarioRevision}
            scenarioRevisionAdmission={ros.scenarioRevisionAdmission}
            onPreviewBundle={() => void ros.previewBundle()}
            onApplyBundle={() => void ros.applyBundle()}
            startPhase={ros.startPhase}
            setStartPhase={ros.setStartPhase}
            connected={ros.connected}
            runtimeAuthorityStatus={ros.runtimeAuthorityStatus}
            actionPending={ros.actionPending}
            actionMessage={ros.actionMessage}
            runtimeMessage={ros.runtimeMessage}
            runtimeReady={ros.simulationReady}
            integrationReadiness={ros.integrationReadiness}
            integrationReadinessReceivedAt={ros.integrationReadinessReceivedAt}
            executionRoute={{
              currentSource: ros.executionRouteState?.selectedSource ?? null,
              currentRetractionSource:
                ros.executionRouteState?.retractionSource ?? null,
              initializationState:
                ros.executionRouteState?.initializationState ?? null,
              sourceReadiness: executionRouteSourceReadiness,
              sourceHealth: executionRouteSourceHealth,
              transitionState: ros.executionRouteTransition.state,
              // Success/progress copy is localized inside the control. Keep
              // the server's bounded diagnostic only for a rejected change.
              transitionMessage:
                ros.executionRouteTransition.state === "failed"
                  ? ros.executionRouteTransition.message
                  : "",
              procedureStopped: liveExecutionRouteSwitchAllowed,
              disabledReason: executionRouteDisabledReason,
              onSourcesChange: (sources) => {
                void ros.configureExecutionRoute(sources);
              },
            }}
            executionState={ros.simulationState.execution_state}
            isRunning={controlIsRunning}
            isPaused={controlIsPaused}
            canPauseResume={controlCanPauseResume}
            onControl={beginControl}
            onOpenMonitor={optionalUiEnabled ? onMonitor : undefined}
          />
          {runtimeMode === "live" ? (
            <LiveAsrPanel
              status={ros.liveAsrStatus}
              statusReceivedAt={ros.liveAsrStatusReceivedAt}
              connected={ros.connected}
              pendingOperation={ros.liveAsrControlPending}
              controlMessage={ros.liveAsrControlMessage}
              language={language}
              onControl={ros.controlLiveAsr}
            />
          ) : null}
          <TypedRfdetrObservationStatus
            detections={ros.typedRfdetrToolDetections}
            language={language}
          />
          {optionalUiEnabled ? (
            <>
              <Suspense fallback={null}>
                <VlmStructuredToolDetectionEvidencePanel
                  evidence={ros.vlmRequestToolDetectionEvidence}
                  language={language}
                />
              </Suspense>
              <OperationExecutionDispatchFeed
                events={executionDispatchEvents}
                language={language}
              />
              <Suspense fallback={null}>
                <ObservabilityPanel
                  vm={vm}
                  language={language}
                  btDecision={ros.btDecision}
                  skillStatus={ros.skillStatus}
                  simulationState={ros.simulationState}
                  worldState={ros.worldState}
                  surgeonState={ros.surgeonState}
                  vlmHealth={ros.vlmHealth}
                  inputSourceStatuses={ros.inputSourceStatuses}
                  vlmResult={ros.vlmResult}
                  vlmReducerDecisions={ros.vlmReducerDecisions}
                  vlmImage={ros.vlmImage}
                  variant="decision"
                />
              </Suspense>
            </>
          ) : null}
        </div>

      </main>
    </div>
  );
}
