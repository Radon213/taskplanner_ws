import { GitBranch, Pause, Play, RotateCcw, Square, Wifi } from "lucide-react";

import type { ControlCommand } from "../../hooks/useRosBridge";
import type { useDigitalTwinViewModel } from "../../hooks/useDigitalTwinViewModel";

type ViewModel = ReturnType<typeof useDigitalTwinViewModel>;

export function ProcedureDock({
  vm,
  url,
  setUrl,
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
  onControl,
}: {
  vm: ViewModel;
  url: string;
  setUrl: (url: string) => void;
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
  onControl: (command: ControlCommand) => void;
}) {
  const startInFlight = executionState === "starting" || actionPending.toLowerCase().includes("starting");
  const commandBusy = Boolean(actionPending);
  const disabled = !connected || !bundle;
  const formDisabled = disabled || commandBusy;
  const phaseSelectDisabled = disabled || commandBusy || isRunning || startInFlight;
  const startDisabled = disabled || commandBusy || !runtimeReady || isRunning || startInFlight;
  const pauseResumeDisabled = disabled || commandBusy || startInFlight || (!isRunning && !isPaused);
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
          <span>{vm.ui.bridgeUrl}</span>
          <div className="field-with-icon">
            <Wifi size={15} />
            <input value={url} onChange={(event) => setUrl(event.target.value)} />
          </div>
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
