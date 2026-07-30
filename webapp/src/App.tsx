import { useEffect, useMemo, useRef, useState } from "react";
import { motion } from "framer-motion";

import { ProcedureDock } from "./components/command/ProcedureDock";
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
import {
  initialRuntimeMode,
  persistRuntimeMode,
  runtimeBridgeUrl,
  type TaskplannerRuntimeMode,
} from "./runtimeModes";
import { type Language } from "./utils/display";

export default function App() {
  const [runtimeMode, setRuntimeMode] =
    useState<TaskplannerRuntimeMode>(initialRuntimeMode);
  const ros = useRosBridge(runtimeMode);
  const [language, setLanguage] = useState<Language>(() => {
    if (typeof window === "undefined") return "ko";
    return window.localStorage.getItem("taskplanner.language") === "en" ? "en" : "ko";
  });
  const [stageAspectRatio, setStageAspectRatio] = useState(1.55);
  const actorPolicyKeyRef = useRef("");

  useEffect(() => {
    window.localStorage.setItem("taskplanner.language", language);
  }, [language]);

  useEffect(() => {
    persistRuntimeMode(runtimeMode);
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

  return (
    <div className="app-shell">
      <StatusRibbon
        vm={vm}
        connected={ros.connected}
        language={language}
        onLanguageChange={setLanguage}
        modelOptions={ros.vlmModelOptions}
        providerStatuses={ros.vlmProviderStatuses}
        modelCatalogStatus={ros.vlmModelCatalogStatus}
        modelSelection={ros.vlmModelSelection}
        actionPending={ros.actionPending}
        onVlmModelChange={(selection) => void ros.setVlmModel(selection)}
        onVlmRuntimeAction={(selection, command) =>
          void ros.controlVlmModelRuntime(selection, command)
        }
      />

      <motion.main
        className="mission-layout"
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.38, ease: [0.22, 1, 0.36, 1] }}
      >
        <div className="stage-area">
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

        <div className="runtime-area">
          <ProcedureDock
            vm={vm}
            url={ros.url}
            runtimeMode={runtimeMode}
            onRuntimeModeChange={setRuntimeMode}
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
          <ObservabilityPanel
            vm={vm}
            language={language}
            btDecision={ros.btDecision}
            skillStatus={ros.skillStatus}
            simulationState={ros.simulationState}
            worldState={ros.worldState}
            surgeonState={ros.surgeonState}
            vlmHealth={ros.vlmHealth}
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
            vlmResult={ros.vlmResult}
            vlmReducerDecisions={ros.vlmReducerDecisions}
            vlmImage={ros.vlmImage}
            variant="timeline"
          />
        </div>
      </motion.main>
    </div>
  );
}
