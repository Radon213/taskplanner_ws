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
  const [overrideTool, setOverrideTool] = useState("");
  const [voiceText, setVoiceText] = useState("");
  const [stageAspectRatio, setStageAspectRatio] = useState(1.55);

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

  const overrideOptions = vm.requestableTools;
  const overrideOptionSignature = overrideOptions.map((option) => `${option.id}:${option.voicePrompt}`).join("|");

  useEffect(() => {
    const first = overrideOptions[0];
    if (!first) return;
    if (!overrideOptions.some((option) => option.id === overrideTool)) {
      setOverrideTool(first.id);
      setVoiceText(first.voicePrompt);
    }
  }, [overrideOptionSignature, overrideOptions, overrideTool]);

  return (
    <div className="app-shell">
      <StatusRibbon vm={vm} connected={ros.connected} language={language} onLanguageChange={setLanguage} />

      <motion.main
        className="mission-layout"
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.38, ease: [0.22, 1, 0.36, 1] }}
      >
        <div className="board-column">
          <OperatingRoomStage
            vm={vm}
            onStageAspectChange={(ratio) => {
              setStageAspectRatio((current) => (Math.abs(current - ratio) > 0.01 ? ratio : current));
            }}
          />
          <ObservabilityPanel
            vm={vm}
            language={language}
            btDecision={ros.btDecision}
            skillStatus={ros.skillStatus}
            vlmHealth={ros.vlmHealth}
            vlmResult={ros.vlmResult}
            vlmReducerDecisions={ros.vlmReducerDecisions}
          />
        </div>

        <div className="side-column command-column">
          <ProcedureDock
            vm={vm}
            url={ros.url}
            setUrl={ros.setUrl}
            bundle={ros.bundle}
            setBundleSelection={ros.setBundleSelection}
            connected={ros.connected}
            actionPending={ros.actionPending}
            actionMessage={ros.actionMessage}
            runtimeMessage={ros.runtimeMessage}
            runtimeReady={ros.simulationReady}
            executionState={ros.simulationState.execution_state}
            isRunning={ros.simulationState.running}
            isPaused={ros.simulationState.execution_state === "paused"}
            onApplyBundle={() => void ros.applyBundle()}
            onControl={(command) => void ros.control(command)}
          />

          <SurgeonIntentDock
            vm={vm}
            language={language}
            overrideOptions={overrideOptions}
            overrideTool={overrideTool}
            setOverrideTool={(tool) => {
              setOverrideTool(tool);
              setVoiceText(overrideOptions.find((option) => option.id === tool)?.voicePrompt ?? `${tool} please`);
            }}
            voiceText={voiceText}
            setVoiceText={setVoiceText}
            connected={ros.connected}
            actionPending={ros.actionPending}
            onOverride={(payload) =>
              void ros.sendOverride({
                ...payload,
                toolLabel: vm.displayToolName(payload.requestedTool),
              })
            }
          />
        </div>
      </motion.main>
    </div>
  );
}
