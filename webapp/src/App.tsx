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
import { ProcedureDock } from "./components/command/ProcedureDock";
import { LiveAsrPanel } from "./components/command/LiveAsrPanel";
import {
  type HandHandoverSignal,
} from "./components/command/HandHandoverSignalStatus";
import { StatusRibbon } from "./components/command/StatusRibbon";
import { SurgeryRecordResponseModal } from "./components/observability/SurgeryRecordResponseModal";
import { ToolBeliefPanel } from "./components/stage/ToolBeliefPanel";
import { useDigitalTwinViewModel } from "./hooks/useDigitalTwinViewModel";
import { useRosBridge } from "./hooks/useRosBridge";
import { useRuntimeControl } from "./hooks/useRuntimeControl";
import { useRosbagUiAuditReplay } from "./hooks/useRosbagUiReplay";
import type { RuntimeOwnerMode } from "./hooks/useRuntimeOwnerControl";
import { deriveOperationPresentation } from "./presentation/operationPresentation";
import {
  initialRosbagUiReplayState,
  isRosbagPresentationReplayEnabled,
  reduceRosbagUiReplayState,
  type RosbagUiReplayState,
} from "./presentation/rosbagUiReplay";
import type {
  RosbagStageCameraId,
  RosbagStageCameraSlot,
  RosbagUiAuditEventName,
  RosbagUiPresentation,
} from "./ros/rosbagUiAuditMessages";
import {
  initialRuntimeMode,
  lastMissionModeStorageKey,
  persistRuntimeMode,
  rosbagReplayRuntimeMode,
  runtimeBridgeUrl,
  surgimateUrl,
  type TaskplannerRuntimeMode,
} from "./runtimeModes";
import {
  missionObservationProfile,
  OPTIONAL_OPERATIONS_UI_ENABLED,
  optionalOperationsUiEnabled,
  runtimeModeIsAvailable,
} from "./runtimeFeatures";
import { type Language } from "./utils/display";

type PrimaryWorkspace = "mission" | "multicam" | "debug";
type WorkspaceHistoryAction = "push" | "replace" | "none";
type MissionRuntimeMode = Exclude<TaskplannerRuntimeMode, "debug">;
type RuntimeTransitionSafety = {
  isRunning: boolean;
  isPaused: boolean;
  startInFlight: boolean;
  actionPending: boolean;
};

type RosbagUiAuditPublisher = (
  event: RosbagUiAuditEventName,
  presentation: RosbagUiPresentation,
  value?: string,
) => void;

function runtimeOwnerModeFor(runtimeMode: TaskplannerRuntimeMode): RuntimeOwnerMode | null {
  if (runtimeMode === "live") return "live";
  if (runtimeMode === "llm") return "llm-surgeon";
  if (runtimeMode === "shadow") return "replay";
  return "debug";
}

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
const OperationExecutionDispatchFeed = lazy(() =>
  import("./components/observability/OperationVlmObservability").then((module) => ({
    default: module.OperationExecutionDispatchFeed,
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
const TypedRfdetrObservationStatus = lazy(() =>
  import("./components/observability/TypedRfdetrObservationStatus").then((module) => ({
    default: module.TypedRfdetrObservationStatus,
  })),
);
const TtsPlaybackStatusCard = lazy(() =>
  import("./components/observability/TtsPlaybackStatusCard").then((module) => ({
    default: module.TtsPlaybackStatusCard,
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
              <div className="debug-skeleton-title" />
              <div className="debug-skeleton-row" />
              <div className="debug-skeleton-row short" />
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
  const pathname = window.location.pathname.replace(/\/+$/, "") || "/";
  if (pathname === "/debug") return "debug";
  const requested = new URLSearchParams(window.location.search).get("workspace");
  if (requested === "debug") return "debug";
  if (!optionalUiEnabled) return "mission";
  return requested === "multicam" ? requested : "mission";
}

export default function App() {
  // Replay uses the deployment's explicit replay bridge (shadow in the
  // isolated Replay profile, Live otherwise), never a stale Debug/LLM
  // browser preference. Both the screen projection and the audit listener
  // must observe the same bag playback graph.
  const [replayPresentationOnly] = useState(() => isRosbagPresentationReplayEnabled());
  const [replayRuntimeMode] = useState(() => rosbagReplayRuntimeMode());
  const [runtimeMode, setRuntimeMode] = useState<TaskplannerRuntimeMode>(() =>
    replayPresentationOnly ? replayRuntimeMode : initialRuntimeMode(),
  );
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
  const [rosbagUiReplayState, setRosbagUiReplayState] = useState<RosbagUiReplayState>(() =>
    initialRosbagUiReplayState({ language, workspace }),
  );
  const rosbagUiReplayStateRef = useRef(rosbagUiReplayState);
  const rosbagUiAuditPublisherRef = useRef<RosbagUiAuditPublisher | null>(null);
  const [lastMissionMode, setLastMissionMode] = useState<MissionRuntimeMode>(() => {
    if (!OPTIONAL_OPERATIONS_UI_ENABLED) return "live";
    if (typeof window === "undefined") return "live";
    const stored = window.localStorage.getItem(lastMissionModeStorageKey());
    return stored === "live" || stored === "llm" || stored === "shadow" ? stored : "live";
  });

  const commitRosbagUiReplayState = useCallback((next: RosbagUiReplayState) => {
    rosbagUiReplayStateRef.current = next;
    setRosbagUiReplayState(next);
  }, []);

  const applyRosbagUiAuditReplay = useCallback((event: Parameters<typeof reduceRosbagUiReplayState>[1]) => {
    if (!replayPresentationOnly) return;
    const next = reduceRosbagUiReplayState(rosbagUiReplayStateRef.current, event);
    commitRosbagUiReplayState(next);
    setLanguage(next.presentation.language);
    setWorkspace(next.presentation.workspace);
  }, [commitRosbagUiReplayState, replayPresentationOnly]);

  useRosbagUiAuditReplay({
    enabled: replayPresentationOnly,
    url: runtimeBridgeUrl(replayRuntimeMode),
    onAudit: applyRosbagUiAuditReplay,
  });

  useEffect(() => {
    if (!replayPresentationOnly) {
      window.localStorage.setItem("taskplanner.language", language);
    }
    document.documentElement.lang = language;
  }, [language, replayPresentationOnly]);

  useEffect(() => {
    if (replayPresentationOnly) return;
    persistRuntimeMode(runtimeMode);
    if (runtimeMode !== "debug" && runtimeModeIsAvailable(runtimeMode)) {
      setLastMissionMode(runtimeMode);
      window.localStorage.setItem(lastMissionModeStorageKey(), runtimeMode);
    }
  }, [replayPresentationOnly, runtimeMode]);

  useEffect(() => {
    if (replayPresentationOnly) return;
    if (
      (workspace !== "mission" && workspace !== "debug") ||
      runtimeTransition.phase !== "idle" ||
      runtimeTransition.activeMode === null ||
      runtimeTransition.activeMode === runtimeMode
    ) {
      return;
    }
    setRuntimeMode(runtimeTransition.activeMode);
  }, [replayPresentationOnly, runtimeMode, runtimeTransition.activeMode, runtimeTransition.phase, workspace]);

  const registerRosbagUiAuditPublisher = useCallback((publisher: RosbagUiAuditPublisher | null) => {
    rosbagUiAuditPublisherRef.current = publisher;
  }, []);

  const recordUiPresentation = useCallback((
    event: RosbagUiAuditEventName,
    presentation: RosbagUiPresentation,
    value = "",
  ) => {
    if (replayPresentationOnly) return;
    commitRosbagUiReplayState({
      ...rosbagUiReplayStateRef.current,
      presentation,
    });
    rosbagUiAuditPublisherRef.current?.(event, presentation, value);
  }, [commitRosbagUiReplayState, replayPresentationOnly]);

  const updateRosbagUiReplaySelections = useCallback((update: {
    bundle?: string;
    startPhase?: string;
  }) => {
    const current = rosbagUiReplayStateRef.current;
    commitRosbagUiReplayState({
      ...current,
      ...update,
    });
  }, [commitRosbagUiReplayState]);

  const handleLanguageChange = useCallback((nextLanguage: Language) => {
    if (replayPresentationOnly) return;
    const nextPresentation = {
      ...rosbagUiReplayStateRef.current.presentation,
      language: nextLanguage,
    };
    setLanguage(nextLanguage);
    recordUiPresentation("language_selected", nextPresentation, nextLanguage);
  }, [recordUiPresentation, replayPresentationOnly]);

  const navigateWorkspace = useCallback((
    next: PrimaryWorkspace,
    historyAction: WorkspaceHistoryAction = "push",
  ) => {
    const availableNext = next === "mission" || next === "debug" || optionalUiEnabled
      ? next
      : "mission";
    if (!replayPresentationOnly) {
      const nextPresentation = {
        ...rosbagUiReplayStateRef.current.presentation,
        workspace: availableNext,
      };
      recordUiPresentation("workspace_selected", nextPresentation, availableNext);
    }
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
  }, [optionalUiEnabled, recordUiPresentation, replayPresentationOnly]);

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
    if (!optionalUiEnabled && workspace !== "mission" && workspace !== "debug") {
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
    if (replayPresentationOnly) return false;
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
    replayPresentationOnly,
  ]);

  const openSurgiMate = useCallback(() => {
    window.location.assign(surgimateUrl());
  }, []);

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

  if (runtimeMode === "debug" || (workspace === "debug" && runtimeMode === "live")) {
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
            runtimeOwnerMode={runtimeOwnerModeFor(runtimeMode)}
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
        runtimeMode={runtimeMode}
        onRuntimeModeChange={requestRuntimeMode}
        runtimeTransition={runtimeTransition}
        language={language}
        onLanguageChange={handleLanguageChange}
        optionalUiEnabled={optionalUiEnabled}
        onOpenSurgiMate={openSurgiMate}
        onIntegratedDebug={() => navigateWorkspace("debug")}
        replayPresentationOnly={replayPresentationOnly}
        rosbagUiReplayState={rosbagUiReplayState}
        onRosbagUiPresentationEvent={recordUiPresentation}
        onRosbagUiReplaySelectionsChange={updateRosbagUiReplaySelections}
        onRegisterRosbagUiAuditPublisher={registerRosbagUiAuditPublisher}
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
  onOpenSurgiMate,
  onIntegratedDebug,
  replayPresentationOnly,
  rosbagUiReplayState,
  onRosbagUiPresentationEvent,
  onRosbagUiReplaySelectionsChange,
  onRegisterRosbagUiAuditPublisher,
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
  onOpenSurgiMate: () => void;
  onIntegratedDebug?: () => void;
  replayPresentationOnly: boolean;
  rosbagUiReplayState: RosbagUiReplayState;
  onRosbagUiPresentationEvent: (
    event: RosbagUiAuditEventName,
    presentation: RosbagUiPresentation,
    value?: string,
  ) => void;
  onRosbagUiReplaySelectionsChange: (update: {
    bundle?: string;
    startPhase?: string;
  }) => void;
  onRegisterRosbagUiAuditPublisher: (publisher: RosbagUiAuditPublisher | null) => void;
}) {
  const rosBridgeReady =
    replayPresentationOnly || (
      runtimeTransition.phase === "idle" &&
      runtimeTransition.activeMode === runtimeMode
    );
  const runtimeProfileMismatch = !replayPresentationOnly &&
    runtimeTransition.diagnosticCode === "runtime_profile_mismatch";
  const ros = useRosBridge(
    runtimeMode,
    rosBridgeReady,
    !replayPresentationOnly && (
      runtimeTransition.phase === "checking" || runtimeTransition.phase === "starting"
    ),
    runtimeProfileMismatch,
    replayPresentationOnly
      ? "extended"
      : missionObservationProfile(runtimeTransition.activeMode),
    {
      replayPresentationOnly,
      rosbagUiPresentation: rosbagUiReplayState.presentation,
    },
  );
  useEffect(() => {
    if (replayPresentationOnly) {
      onRegisterRosbagUiAuditPublisher(null);
      return undefined;
    }
    const publisher: RosbagUiAuditPublisher = (event, presentation, value = "") => {
      ros.publishRosbagUiAudit(event, { presentation, value });
    };
    onRegisterRosbagUiAuditPublisher(publisher);
    return () => {
      onRegisterRosbagUiAuditPublisher(null);
    };
  }, [onRegisterRosbagUiAuditPublisher, replayPresentationOnly, ros.publishRosbagUiAudit]);
  const [stageAspectRatio, setStageAspectRatio] = useState(1.55);
  const handleStageAspectChange = useCallback((ratio: number) => {
    setStageAspectRatio((current) => (
      Math.abs(current - ratio) > 0.01 ? ratio : current
    ));
  }, []);

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
  const operationPresentation = useMemo(
    () => deriveOperationPresentation({
      executionTraces: ros.executionTraces,
      skillStatusByCommand: ros.skillStatusByCommand,
      asrFinals: ros.liveAsrStatus.finals,
      language,
      displayToolName: vm.displayToolName,
    }),
    [
      language,
      ros.executionTraces,
      ros.liveAsrStatus.finals,
      ros.skillStatusByCommand,
      vm.displayToolName,
    ],
  );
  useEffect(() => {
    if (replayPresentationOnly) return;
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
    replayPresentationOnly,
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
  const activeProcedureRunId =
    ros.simulationState.running
    && ros.simulationState.execution_state.trim().toLowerCase() === "running"
      ? ros.simulationState.procedure_run_id?.trim() || ""
      : "";
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
    const routeControlEnabled = ros.executionRouteState?.routeControlEnabled === true;
    const sourceHealth = ros.executionRouteState?.sourceEndpointReadiness ?? {};
    const readiness = (available: boolean | undefined) => {
      if (!routeControlEnabled || available === undefined) return "unknown" as const;
      return available ? "ready" as const : "unavailable" as const;
    };
    return {
      toolHandover: {
        external: readiness(sourceHealth.external?.actionServerReady),
        virtual: readiness(sourceHealth.virtual?.actionServerReady),
      },
      retraction: {
        external: readiness(sourceHealth.external?.retractionServiceReady),
        virtual: readiness(sourceHealth.virtual?.retractionServiceReady),
      },
    };
  }, [
    ros.executionRouteState?.routeControlEnabled,
    ros.executionRouteState?.sourceEndpointReadiness,
  ]);
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
  const updateRosbagPresentation = useCallback((
    event: RosbagUiAuditEventName,
    nextPresentation: RosbagUiPresentation,
    value = "",
  ) => {
    if (replayPresentationOnly) return;
    onRosbagUiPresentationEvent(event, nextPresentation, value);
  }, [onRosbagUiPresentationEvent, replayPresentationOnly]);
  const handleStageCameraPresentation = useCallback((
    event: Extract<
      RosbagUiAuditEventName,
      "stage_camera_selected" | "stage_camera_inspection_changed"
    >,
    slot: RosbagStageCameraSlot,
    camera: RosbagStageCameraId,
    inspecting: boolean,
  ) => {
    if (replayPresentationOnly) return;
    const current = rosbagUiReplayState.presentation;
    const nextPresentation: RosbagUiPresentation = slot === "surgical_bed"
      ? {
        ...current,
        stageSurgicalBedCamera: camera === "cam2" || camera === "flir"
          ? camera
          : current.stageSurgicalBedCamera,
        stageSurgicalBedInspecting: inspecting,
      }
      : slot === "independent"
        ? {
          ...current,
          stageIndependentCamera: camera === "cam1" || camera === "cam4"
            ? camera
            : current.stageIndependentCamera,
          stageIndependentInspecting: inspecting,
        }
        : {
          ...current,
          stageCam3Inspecting: inspecting,
        };
    updateRosbagPresentation(event, nextPresentation, `${slot}:${camera}:${inspecting ? "open" : "closed"}`);
  }, [replayPresentationOnly, rosbagUiReplayState.presentation, updateRosbagPresentation]);
  const handleSurgeryRecordPresentation = useCallback((
    event: Extract<
      RosbagUiAuditEventName,
      "surgery_record_opened" | "surgery_record_tab_selected" | "surgery_record_closed"
    >,
    visible: boolean,
    tab: RosbagUiPresentation["surgeryRecordTab"],
  ) => {
    if (replayPresentationOnly) return;
    const nextPresentation: RosbagUiPresentation = {
      ...rosbagUiReplayState.presentation,
      surgeryRecordVisible: visible,
      surgeryRecordTab: tab,
    };
    updateRosbagPresentation(event, nextPresentation, visible ? tab : "closed");
  }, [replayPresentationOnly, rosbagUiReplayState.presentation, updateRosbagPresentation]);
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
          runtimeMode === "live" &&
          runtimeTransition.activeMode === "live"
        }
        onIntegratedDebug={onIntegratedDebug}
        onOpenSurgiMate={onOpenSurgiMate}
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
              cameraMediaStore={runtimeMode === "live" && !replayPresentationOnly
                ? ros.cameraMediaStore
                : undefined}
              cameraPreviewContracts={ros.cameraPreviewContracts}
              typedRfdetrObservationStore={
                optionalUiEnabled ? ros.typedRfdetrObservationStore : undefined
              }
              toolBeliefs={runtimeMode === "live" ? ros.toolBeliefs : undefined}
              procedureRunning={ros.worldState.running}
              surgeonRequestedTool={ros.worldState.surgeon_request_tool}
              asrFinalSentence={operationPresentation.asrFinal}
              handHandoverSignal={handHandoverSignal}
              vlmResult={ros.vlmResult}
              systemToolPredictions={ros.worldState.ranked_tool_predictions}
              toolPolicyStatus={ros.toolPolicyStatus}
              onStageAspectChange={handleStageAspectChange}
              replayOnly={replayPresentationOnly}
              replayPresentation={rosbagUiReplayState.presentation}
              onReplayStageCameraChange={handleStageCameraPresentation}
            />
          </Suspense>
          <div
            className={`stage-observer-area ${runtimeMode === "live" ? "with-tool-beliefs" : ""}`}
            data-slot="stage-observer-area"
          >
            <div className="operation-dispatch-area" data-slot="operation-dispatch-area">
              <Suspense fallback={null}>
                <OperationExecutionDispatchFeed
                  className="stage-operation-dispatch-feed"
                  events={operationPresentation.dispatches.filter((event) => event.state !== "proposed")}
                  language={language}
                  maxEntries={1}
                />
              </Suspense>
            </div>
            {runtimeMode === "live" ? (
              <div className="tool-belief-area" data-slot="tool-belief-area">
                <ToolBeliefPanel language={vm.language} runtime={ros.toolBeliefs} />
              </div>
            ) : null}
          </div>
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
            bundle={replayPresentationOnly ? rosbagUiReplayState.bundle : ros.bundle}
            onBundleChange={(nextBundle) => {
              if (replayPresentationOnly) return;
              onRosbagUiReplaySelectionsChange({ bundle: nextBundle });
              ros.setBundleSelection(nextBundle);
            }}
            activeBundle={ros.activeBundle}
            scenarioRevision={ros.scenarioRevision}
            scenarioRevisionAdmission={ros.scenarioRevisionAdmission}
            onPreviewBundle={() => void ros.previewBundle()}
            onApplyBundle={() => void ros.applyBundle()}
            startPhase={replayPresentationOnly ? rosbagUiReplayState.startPhase : ros.startPhase}
            setStartPhase={(nextPhase) => {
              if (replayPresentationOnly) return;
              onRosbagUiReplaySelectionsChange({ startPhase: nextPhase });
              ros.setStartPhase(nextPhase);
            }}
            transportConnected={ros.transportConnected}
            connected={ros.connected}
            runtimeAuthorityStatus={ros.runtimeAuthorityStatus}
            actionPending={ros.actionPending}
            actionMessage={ros.actionMessage}
            runtimeMessage={ros.runtimeMessage}
            runtimeReady={ros.simulationReady}
            rosbagRecording={ros.rosbagRecording}
            rosbagRecordingControlPending={ros.rosbagRecordingControlPending}
            rosbagRecordingControlMessage={ros.rosbagRecordingControlMessage}
            onRosbagRecordingControl={(enabled) => void ros.controlRosbagRecording(enabled)}
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
          />
          {runtimeMode === "live" ? (
            <LiveAsrPanel
              status={ros.liveAsrStatus}
              statusReceivedAt={ros.liveAsrStatusReceivedAt}
              statusBridgeUrl={ros.url}
              statusObservationEnabled={rosBridgeReady}
              connected={ros.transportConnected}
              pendingOperation={ros.liveAsrControlPending}
              controlMessage={ros.liveAsrControlMessage}
              language={language}
              inputSourceStatuses={ros.inputSourceStatuses}
              onControl={ros.controlLiveAsr}
            />
          ) : null}
          {runtimeMode === "live" ? (
            <Suspense fallback={null}>
              <TtsPlaybackStatusCard status={ros.ttsPlaybackStatus} language={language} />
            </Suspense>
          ) : null}
          <Suspense fallback={null}>
            <TypedRfdetrObservationStatus
              observationStore={ros.typedRfdetrObservationStore}
              language={language}
            />
          </Suspense>
          {optionalUiEnabled ? (
            <>
              <Suspense fallback={null}>
                <VlmStructuredToolDetectionEvidencePanel
                  evidence={ros.vlmRequestToolDetectionEvidence}
                  language={language}
                />
              </Suspense>
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
      <SurgeryRecordResponseModal
        activeProcedureRunId={activeProcedureRunId}
        language={language}
        receipt={ros.surgeryRecordReceipt}
        replayOnly={replayPresentationOnly}
        replayPresentation={rosbagUiReplayState.presentation}
        onReplayPresentationChange={handleSurgeryRecordPresentation}
      />
    </div>
  );
}
