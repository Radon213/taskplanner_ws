import { useEffect, useState } from "react";
import { motion } from "framer-motion";

import { ProcedureDock } from "./components/command/ProcedureDock";
import { StatusRibbon } from "./components/command/StatusRibbon";
import { SurgeonIntentDock } from "./components/command/SurgeonIntentDock";
import { ObservabilityPanel } from "./components/observability/ObservabilityPanel";
import { OperatingRoomStage } from "./components/stage/OperatingRoomStage";
import { useDigitalTwinViewModel } from "./hooks/useDigitalTwinViewModel";
import { useRosBridge } from "./hooks/useRosBridge";
import { type Language } from "./utils/display";

export default function App() {
  const ros = useRosBridge();
  const [language, setLanguage] = useState<Language>(() => {
    if (typeof window === "undefined") return "ko";
    return window.localStorage.getItem("taskplanner.language") === "en" ? "en" : "ko";
  });
  const [stageAspectRatio, setStageAspectRatio] = useState(1.55);
  const [vlmModelSelection, setVlmModelSelection] = useState("");
  const [actorModelSelection, setActorModelSelection] = useState("");

  useEffect(() => {
    window.localStorage.setItem("taskplanner.language", language);
  }, [language]);

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

  useEffect(() => {
    if (ros.vlmHealth.model_id) {
      setVlmModelSelection(ros.vlmHealth.model_id);
    }
  }, [ros.vlmHealth.model_id]);

  useEffect(() => {
    if (ros.surgeonLlmDecision.model_id) {
      setActorModelSelection(ros.surgeonLlmDecision.model_id);
    }
  }, [ros.surgeonLlmDecision.model_id]);

  useEffect(() => {
    if (!ros.vlmModelOptions.length) return;
    setVlmModelSelection(
      (current) => current || ros.vlmModelOptions.find((id) => id.toLowerCase().includes("qwen")) || ros.vlmModelOptions[0],
    );
  }, [ros.vlmModelOptions]);

  useEffect(() => {
    if (!ros.actorModelOptions.length) return;
    setActorModelSelection(
      (current) => current || ros.actorModelOptions.find((id) => id.toLowerCase().includes("gemma")) || ros.actorModelOptions[0],
    );
  }, [ros.actorModelOptions]);

  return (
    <div className="app-shell">
      <StatusRibbon
        vm={vm}
        connected={ros.connected}
        language={language}
        onLanguageChange={setLanguage}
        modelOptions={ros.vlmModelOptions}
        modelCatalogStatus={ros.vlmModelCatalogStatus}
        vlmModel={vlmModelSelection}
        actionPending={ros.actionPending}
        onVlmModelChange={(modelId) => {
          setVlmModelSelection(modelId);
          void ros.setVlmModel(modelId);
        }}
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
            vlmImage={ros.vlmImage}
            onStageAspectChange={(ratio) => {
              setStageAspectRatio((current) => (Math.abs(current - ratio) > 0.01 ? ratio : current));
            }}
          />
        </div>

        <div className="surgeon-area">
          <SurgeonIntentDock
            vm={vm}
            language={language}
            llmDecision={ros.surgeonLlmDecision}
            actorEnabled={ros.actorEnabled}
            modelOptions={ros.actorModelOptions}
            modelCatalogStatus={ros.actorModelCatalogStatus}
            actorModel={actorModelSelection}
            connected={ros.connected}
            actionPending={ros.actionPending}
            onActorEnabledChange={(enabled) => void ros.setActorEnabled(enabled)}
            onActorModelChange={(modelId) => {
              setActorModelSelection(modelId);
              void ros.setActorModel(modelId);
            }}
          />
        </div>

        <div className="runtime-area">
          <ProcedureDock
            vm={vm}
            url={ros.url}
            setUrl={ros.setUrl}
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
            isRunning={ros.simulationState.running}
            isPaused={ros.simulationState.execution_state === "paused"}
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
