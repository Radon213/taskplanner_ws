import { GitBranch, Pause, Play, RadioTower, RotateCcw, Square, Wifi } from "lucide-react";

import type { ControlCommand } from "../../hooks/useRosBridge";
import type { useDigitalTwinViewModel } from "../../hooks/useDigitalTwinViewModel";
import type { TaskplannerRuntimeMode } from "../../runtimeModes";

type ViewModel = ReturnType<typeof useDigitalTwinViewModel>;

export function ProcedureDock({
  vm,
  url,
  runtimeMode,
  onRuntimeModeChange,
  bundle,
  onBundleChange,
  startPhase,
  setStartPhase,
  connected,
  actionPending,
  actionMessage,
  runtimeMessage,
  runtimeReady,
  executionState,
  isRunning,
  isPaused,
  canPauseResume,
  onControl,
}: {
  vm: ViewModel;
  url: string;
  runtimeMode: TaskplannerRuntimeMode;
  onRuntimeModeChange: (mode: TaskplannerRuntimeMode) => void;
  bundle: string;
  onBundleChange: (bundle: string) => void;
  startPhase: string;
  setStartPhase: (phaseId: string) => void;
  connected: boolean;
  actionPending: string;
  actionMessage: string;
  runtimeMessage: string;
  runtimeReady: boolean;
  executionState: string;
  isRunning: boolean;
  isPaused: boolean;
  canPauseResume: boolean;
  onControl: (command: ControlCommand) => void;
}) {
  const startInFlight = executionState === "starting" || actionPending.toLowerCase().includes("starting");
  const commandBusy = Boolean(actionPending);
  const disabled = !connected || !bundle;
  const formDisabled = disabled || commandBusy;
  const phaseSelectDisabled = disabled || commandBusy || isRunning || startInFlight;
  const startDisabled = disabled || commandBusy || !runtimeReady || isRunning || startInFlight;
  const pauseResumeDisabled =
    disabled || commandBusy || startInFlight || !canPauseResume;
  const interruptDisabled = disabled || (commandBusy && !startInFlight);
  const statusMessage = vm.runtime.statusMessage;
  const trimmedActionMessage = actionMessage.trim();
  const shouldShowActionMessage =
    Boolean(trimmedActionMessage) &&
    trimmedActionMessage !== "Ready." &&
    trimmedActionMessage !== "ROS bridge connected." &&
    trimmedActionMessage !== runtimeMessage;
  const actionMessageTone =
    /failed|error|cannot|unknown|unsupported|offline|timed out|paused;/i.test(trimmedActionMessage)
      ? "error"
      : actionPending
        ? "pending"
        : "normal";
  const runtimeModeOptions: Array<{
    id: TaskplannerRuntimeMode;
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
  const selectedRuntimeMode =
    runtimeModeOptions.find((option) => option.id === runtimeMode) ??
    runtimeModeOptions[1];

  return (
    <aside className="dock procedure-dock">
      <div className="dock-header">
        <div>
          <p className="section-kicker">{vm.ui.currentState}</p>
          <h2>{vm.runtime.stateLabel}</h2>
          {statusMessage ? <span className="dock-inline-status">{statusMessage}</span> : null}
        </div>
        <GitBranch size={18} />
      </div>

      {shouldShowActionMessage ? (
        <div className={["dock-action-message", actionMessageTone].join(" ")}>{trimmedActionMessage}</div>
      ) : null}

      <div className="control-stack">
        <label className="field">
          <span>{vm.language === "ko" ? "실행 모드" : "Runtime mode"}</span>
          <div className="runtime-mode-select">
            <RadioTower size={16} aria-hidden="true" />
            <select
              value={runtimeMode}
              onChange={(event) =>
                onRuntimeModeChange(event.target.value as TaskplannerRuntimeMode)
              }
            >
              {runtimeModeOptions.map((option) => (
                <option value={option.id} key={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
            <i className={connected ? "connected" : "offline"}>
              {connected
                ? vm.language === "ko"
                  ? "연결"
                  : "Online"
                : vm.language === "ko"
                  ? "대기"
                  : "Offline"}
            </i>
          </div>
          <small className="runtime-mode-detail">{selectedRuntimeMode.detail}</small>
          <small className="runtime-endpoint" title={url}>
            <Wifi size={12} aria-hidden="true" />
            {url}
          </small>
        </label>

        <label className="field">
          <span>{vm.ui.surgery}</span>
          <select value={bundle} disabled={formDisabled} onChange={(event) => onBundleChange(event.target.value)}>
            {vm.bundleOptions.map((option) => (
              <option value={option.id} key={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

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
      </div>

      <div className="transport-controls" aria-label={vm.ui.control}>
        <button className="button button-primary" disabled={startDisabled} onClick={() => onControl("start")} type="button">
          <Play size={17} />
          {runtimeReady ? vm.ui.start : vm.ui.preparing}
        </button>
        <button
          className="button button-secondary"
          disabled={pauseResumeDisabled}
          onClick={() => onControl(isPaused ? "resume" : "pause")}
          type="button"
        >
          {isPaused ? <Play size={17} /> : <Pause size={17} />}
          {isPaused ? vm.ui.resume : vm.ui.pause}
        </button>
        <button className="button button-secondary" disabled={interruptDisabled} onClick={() => onControl("reset")} type="button">
          <RotateCcw size={17} />
          {vm.ui.reset}
        </button>
        <button className="button button-quiet" disabled={interruptDisabled} onClick={() => onControl("stop")} type="button">
          <Square size={16} />
          {vm.ui.stop}
        </button>
      </div>

    </aside>
  );
}
