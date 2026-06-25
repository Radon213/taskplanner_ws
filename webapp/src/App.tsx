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
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [modelCatalogStatus, setModelCatalogStatus] = useState("loading");
  const [vlmModelSelection, setVlmModelSelection] = useState("");
  const [actorModelSelection, setActorModelSelection] = useState("google/gemma-4-12b-qat");

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
    let disposed = false;
    async function refreshModels() {
      try {
        const response = await fetch("http://127.0.0.1:1234/v1/models");
        if (!response.ok) throw new Error(`model endpoint returned ${response.status}`);
        const payload = (await response.json()) as { data?: Array<{ id?: string }> };
        const ids = (payload.data ?? []).map((item) => String(item.id || "")).filter(Boolean);
        if (disposed) return;
        setModelOptions(ids);
        setModelCatalogStatus(ids.length ? "connected" : "empty");
      } catch (error) {
        if (disposed) return;
        setModelOptions([]);
        setModelCatalogStatus(error instanceof Error ? error.message : "model endpoint unavailable");
      }
    }
    void refreshModels();
    const timer = window.setInterval(() => void refreshModels(), 5000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, []);

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
    if (!modelOptions.length) return;
    setVlmModelSelection((current) => current || modelOptions.find((id) => id.toLowerCase().includes("qwen")) || modelOptions[0]);
    setActorModelSelection((current) => current || modelOptions.find((id) => id.toLowerCase().includes("gemma")) || modelOptions[0]);
  }, [modelOptions]);

  return (
    <div className="app-shell">
      <StatusRibbon
        vm={vm}
        connected={ros.connected}
        language={language}
        onLanguageChange={setLanguage}
        modelOptions={modelOptions}
        modelCatalogStatus={modelCatalogStatus}
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
            modelOptions={modelOptions}
            modelCatalogStatus={modelCatalogStatus}
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
