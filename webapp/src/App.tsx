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

import { ControlAuthorityStrip } from "./components/command/ControlAuthorityStrip";
import { ProcedureDock } from "./components/command/ProcedureDock";
import { LiveAsrPanel } from "./components/command/LiveAsrPanel";
import {
  type PublicSurgeonGesture,
} from "./components/command/PublicSurgeonGestureStatus";
import { StatusRibbon } from "./components/command/StatusRibbon";
import { ShadowReplayDock } from "./components/command/ShadowReplayDock";
import { SurgeonIntentDock } from "./components/command/SurgeonIntentDock";
import { ObservabilityPanel } from "./components/observability/ObservabilityPanel";
import { OperatingRoomStage } from "./components/stage/OperatingRoomStage";
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
import { type Language } from "./utils/display";
import { shimmer } from "./motion-system";

type PrimaryWorkspace = "mission" | "multicam";
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

function WorkspaceLoading({
  label,
  error = "",
  onRetry,
  retryLabel = "Retry",
}: {
  label: string;
  error?: string;
  onRetry?: () => void;
  retryLabel?: string;
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
        />
      );
    }
    return this.props.children;
  }
}

function workspaceFromLocation(): PrimaryWorkspace {
  if (typeof window === "undefined") return "mission";
  return new URLSearchParams(window.location.search).get("workspace") === "multicam"
    ? "multicam"
    : "mission";
}

export default function App() {
  const [runtimeMode, setRuntimeMode] = useState<TaskplannerRuntimeMode>(initialRuntimeMode);
  const {
    status: runtimeTransition,
    refresh: refreshRuntimeControl,
    requestTransition,
  } = useRuntimeControl();
  const [workspace, setWorkspace] = useState<PrimaryWorkspace>(workspaceFromLocation);
  const [language, setLanguage] = useState<Language>(() => {
    if (typeof window === "undefined") return "ko";
    return window.localStorage.getItem("taskplanner.language") === "en" ? "en" : "ko";
  });
  const [lastMissionMode, setLastMissionMode] = useState<MissionRuntimeMode>(() => {
    if (typeof window === "undefined") return "llm";
    const stored = window.localStorage.getItem(lastMissionModeStorageKey());
    return stored === "live" || stored === "llm" || stored === "shadow" ? stored : "llm";
  });

  useEffect(() => {
    window.localStorage.setItem("taskplanner.language", language);
    document.documentElement.lang = language;
  }, [language]);

  useEffect(() => {
    persistRuntimeMode(runtimeMode);
    if (runtimeMode !== "debug") {
      setLastMissionMode(runtimeMode);
      window.localStorage.setItem(lastMissionModeStorageKey(), runtimeMode);
    }
  }, [runtimeMode]);

  useEffect(() => {
    if (
      workspace !== "mission" ||
      runtimeTransition.phase !== "idle" ||
      runtimeTransition.activeMode === null ||
      runtimeTransition.activeMode === runtimeMode
    ) {
      return;
    }
    setRuntimeMode(runtimeTransition.activeMode);
  }, [runtimeMode, runtimeTransition.activeMode, runtimeTransition.phase, workspace]);

  const navigateWorkspace = useCallback((next: PrimaryWorkspace, pushHistory = true) => {
    if (typeof window !== "undefined") {
      const location = new URL(window.location.href);
      if (next === "multicam") location.searchParams.set("workspace", "multicam");
      else location.searchParams.delete("workspace");
      if (pushHistory) window.history.pushState({}, "", location);
    }
    setWorkspace(next);
  }, []);

  useEffect(() => {
    const onPopState = () => {
      navigateWorkspace(workspaceFromLocation(), false);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [navigateWorkspace]);

  const requestRuntimeMode = useCallback(async (
    mode: TaskplannerRuntimeMode,
    safety?: RuntimeTransitionSafety,
  ) => {
    if (
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
  ]);

  if (workspace === "multicam") {
    return (
      <WorkspaceErrorBoundary
        errorMessage={language === "ko" ? "멀티캠 관제 화면을 불러오지 못했습니다." : "Could not load multicamera operations."}
        reloadLabel={language === "ko" ? "페이지 다시 불러오기" : "Reload page"}
      >
        <Suspense fallback={<WorkspaceLoading label={language === "ko" ? "멀티캠 관제 화면을 불러오는 중입니다." : "Loading multicamera operations."} />}>
          <MulticamOpsWorkspace language={language} onExit={() => navigateWorkspace("mission")} />
        </Suspense>
      </WorkspaceErrorBoundary>
    );
  }

  if (runtimeMode === "debug") {
    return (
      <WorkspaceErrorBoundary
        errorMessage={language === "ko" ? "디버그 화면을 불러오지 못했습니다." : "Could not load the Debug workspace."}
        reloadLabel={language === "ko" ? "페이지 다시 불러오기" : "Reload page"}
      >
        <Suspense fallback={<WorkspaceLoading label={language === "ko" ? "디버그 화면을 불러오는 중입니다." : "Loading debug workspace."} />}>
          <DebugWorkspace language={language} onExit={() => void requestRuntimeMode(lastMissionMode)} />
        </Suspense>
      </WorkspaceErrorBoundary>
    );
  }

  return (
    <MissionWorkspace
      runtimeMode={runtimeMode}
      onRuntimeModeChange={requestRuntimeMode}
      runtimeTransition={runtimeTransition}
      language={language}
      onLanguageChange={setLanguage}
      onMulticamOps={() => navigateWorkspace("multicam")}
    />
  );
}

function MissionWorkspace({
  runtimeMode,
  onRuntimeModeChange,
  runtimeTransition,
  language,
  onLanguageChange,
  onMulticamOps,
}: {
  runtimeMode: Exclude<TaskplannerRuntimeMode, "debug">;
  onRuntimeModeChange: (
    mode: TaskplannerRuntimeMode,
    safety?: RuntimeTransitionSafety,
  ) => void | boolean | Promise<void | boolean>;
  runtimeTransition: ReturnType<typeof useRuntimeControl>["status"];
  language: Language;
  onLanguageChange: (language: Language) => void;
  onMulticamOps: () => void;
}) {
  const ros = useRosBridge(runtimeMode);
  const [stageAspectRatio, setStageAspectRatio] = useState(1.55);
  const actorPolicyKeyRef = useRef("");

  useEffect(() => {
    const nextUrl = runtimeBridgeUrl(runtimeMode);
    if (ros.url !== nextUrl) {
      ros.setUrl(nextUrl);
    }
  }, [runtimeMode, ros.url]);

  useEffect(() => {
    if (!ros.connected || runtimeMode === "shadow") return;
    const policyKey = `${runtimeMode}:${ros.url}`;
    if (actorPolicyKeyRef.current === policyKey) return;
    actorPolicyKeyRef.current = policyKey;
    void ros.setActorEnabled(runtimeMode === "llm");
  }, [ros.connected, ros.url, runtimeMode]);

  const vm = useDigitalTwinViewModel({
    language,
    activeBundle: ros.activeBundle,
    simulationState: ros.simulationState,
    bedRobotArms: ros.bedRobotArms,
    skillStatus: ros.skillStatus,
    surgeonState: ros.surgeonState,
    events: ros.events,
    overrideAck: ros.overrideAck,
    vlmHealth: ros.vlmHealth,
    vlmResult: ros.vlmResult,
    vlmHealthReceivedAt: ros.vlmHealthReceivedAt,
    vlmResultReceivedAt: ros.vlmResultReceivedAt,
    stageAspectRatio,
  });
  const vlmSurgeonGesture = useMemo<PublicSurgeonGesture>(
    () => ({
      eventType: ros.vlmResult.gesture_event_type,
      handPose: ros.vlmResult.gesture_hand_pose,
      confidence: ros.vlmResult.gesture_confidence,
      requestedTool: ros.vlmResult.gesture_requested_tool,
    }),
    [ros.vlmResult],
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
  const runtimeTransitionSafety: RuntimeTransitionSafety = {
    isRunning: controlIsRunning,
    isPaused: controlIsPaused,
    startInFlight: controlStartInFlight,
    actionPending: Boolean(ros.actionPending),
  };
  const runtimeModeLocked =
    runtimeTransition.phase === "starting" ||
    controlIsRunning ||
    controlIsPaused ||
    controlStartInFlight ||
    Boolean(ros.actionPending);
  return (
    <div className="app-shell mission-app-shell" data-slot="mission-workspace">
      <a className="skip-link" href="#mission-main">
        {language === "ko" ? "미션 본문으로 이동" : "Skip to mission content"}
      </a>
      <StatusRibbon
        vm={vm}
        connected={ros.connected}
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
        debugModeDisabled={runtimeModeLocked}
        onDebugMode={() => void onRuntimeModeChange("debug", runtimeTransitionSafety)}
        onMulticamOps={onMulticamOps}
      />

      <main
        className={`mission-layout ${runtimeMode === "live" ? "live-stage-expanded" : ""}`}
        id="mission-main"
        tabIndex={-1}
      >
        <div className="authority-area">
          <ControlAuthorityStrip
            language={language}
            connected={ros.connected}
            procedure={vm.stage.procedureLabel}
            phase={vm.stage.phaseName}
            runtimeState={vm.runtime.stateLabel}
            vlmHealth={ros.vlmHealth}
            worldState={ros.worldState}
            btDecision={ros.btDecision}
            skillStatus={ros.skillStatus}
          />
        </div>

        <div
          aria-label={language === "ko" ? "수술실 디지털 트윈 상세 보기" : "Operating room digital twin detail"}
          className="stage-area"
          role="region"
          tabIndex={0}
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
            perceptionCameraFrames={{
              cam4: ros.cam4PerceptionImage,
              flir: ros.flirPerceptionImage,
            }}
            perceptionOverlayFrames={{
              cam4: ros.cam4PerceptionOverlay,
              flir: ros.flirPerceptionOverlay,
            }}
            perceptionHealth={ros.perceptionHealth}
            perceptionControlPending={ros.actionPending.includes(
              "object recognition",
            )}
            onPerceptionEnabledChange={(enabled) =>
              void ros.setPerceptionEnabled(enabled)
            }
            systemSurgeonRequest={fusedSurgeonRequest}
            onStageAspectChange={(ratio) => {
              setStageAspectRatio((current) => (Math.abs(current - ratio) > 0.01 ? ratio : current));
            }}
          />
        </div>

        {runtimeMode !== "live" ? (
          <div className="surgeon-area">
            {runtimeMode === "shadow" ? (
              <ShadowReplayDock
                vm={vm}
                language={language}
                state={ros.shadowReplayState}
                transcript={ros.shadowTranscript}
                connected={ros.connected}
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
                modelOptions={ros.actorModelOptions}
                providerStatuses={ros.actorProviderStatuses}
                modelCatalogStatus={ros.actorModelCatalogStatus}
                modelSelection={ros.actorModelSelection}
                connected={ros.connected}
                actionPending={ros.actionPending}
                publicSurgeonGesture={vlmSurgeonGesture}
                onActorEnabledChange={(enabled) => void ros.setActorEnabled(enabled)}
                onActorModelChange={(selection) => void ros.setActorModel(selection)}
                onActorRuntimeAction={(selection, command) =>
                  void ros.controlActorModelRuntime(selection, command)
                }
              />
            )}
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
            runtimeTransition={runtimeTransition}
            onRetryRuntimeMode={() =>
              void onRuntimeModeChange(
                runtimeTransition.requestedMode ?? runtimeMode,
                runtimeTransitionSafety,
              )
            }
            bundle={ros.bundle}
            onBundleChange={(nextBundle) => {
              ros.setBundleSelection(nextBundle);
              void ros.applyBundle(nextBundle);
            }}
            startPhase={ros.startPhase}
            setStartPhase={ros.setStartPhase}
            connected={ros.connected}
            actionPending={ros.actionPending}
            actionMessage={ros.actionMessage}
            runtimeMessage={ros.runtimeMessage}
            runtimeReady={ros.simulationReady}
            executionState={ros.simulationState.execution_state}
            isRunning={controlIsRunning}
            isPaused={controlIsPaused}
            canPauseResume={controlCanPauseResume}
            onControl={(command) => void ros.control(command)}
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
        </div>

        <div className="timeline-area">
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
            variant="timeline"
          />
        </div>
      </main>
    </div>
  );
}
