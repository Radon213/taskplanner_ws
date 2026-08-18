import { useState } from "react";
import { AnimatePresence, useReducedMotion } from "framer-motion";
import * as m from "framer-motion/m";
import { GitBranch, Pause, Play, RadioTower, RotateCcw, Square, Wifi } from "lucide-react";

import { SafetyConfirmationDialog } from "../common/SafetyConfirmationDialog";
import type { ControlCommand } from "../../hooks/useRosBridge";
import type { useDigitalTwinViewModel } from "../../hooks/useDigitalTwinViewModel";
import type { RuntimeTransitionStatus } from "../../hooks/useRuntimeControl";
import { shimmer, statusSwap } from "../../motion-system";
import type { TaskplannerRuntimeMode } from "../../runtimeModes";

type ViewModel = ReturnType<typeof useDigitalTwinViewModel>;
type MissionRuntimeMode = Exclude<TaskplannerRuntimeMode, "debug">;

export function ProcedureDock({
  vm,
  url,
  runtimeMode,
  onRuntimeModeChange,
  runtimeTransition,
  onRetryRuntimeMode,
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
  onRuntimeModeChange: (mode: TaskplannerRuntimeMode) => void | Promise<void>;
  runtimeTransition: RuntimeTransitionStatus;
  onRetryRuntimeMode: () => void;
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
  const [resetConfirmationOpen, setResetConfirmationOpen] = useState(false);
  const reducedMotion = useReducedMotion();
  const runtimeSwitchPending = runtimeTransition.phase === "starting";
  const startInFlight = executionState === "starting" || actionPending.toLowerCase().includes("starting");
  const commandBusy = Boolean(actionPending);
  const runtimeModeLocked = runtimeSwitchPending || isRunning || isPaused || startInFlight || commandBusy;
  const disabled = !connected || !bundle || runtimeSwitchPending;
  const formDisabled = disabled || commandBusy;
  const phaseSelectDisabled = disabled || commandBusy || isRunning || startInFlight;
  const startDisabled = disabled || commandBusy || !runtimeReady || isRunning || startInFlight;
  const pauseResumeDisabled =
    disabled || commandBusy || startInFlight || !canPauseResume;
  const resetDisabled = disabled || commandBusy || startInFlight;
  const stopDisabled =
    !connected || runtimeSwitchPending || (!isRunning && !isPaused && !startInFlight && !commandBusy);
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
  const displayedRuntimeMode = runtimeSwitchPending
    ? runtimeTransition.requestedMode ?? runtimeMode
    : runtimeMode;
  const selectedRuntimeMode =
    runtimeModeOptions.find((option) => option.id === displayedRuntimeMode) ??
    runtimeModeOptions[1];
  const apiTransitionMessage = runtimeTransition.message.trim();
  const noActiveRuntime =
    runtimeTransition.phase === "idle" && runtimeTransition.activeMode === null;
  const transitionCopy =
    runtimeTransition.phase === "checking"
      ? vm.language === "ko"
        ? "자동 시작 서비스를 확인하는 중입니다."
        : "Checking the runtime starter."
      : runtimeTransition.phase === "starting"
        ? vm.language === "ko"
          ? `${selectedRuntimeMode.label}을 시작하는 중입니다. ROS 연결이 자동으로 재개됩니다.`
          : `Starting ${selectedRuntimeMode.label}. ROS will reconnect automatically.`
        : runtimeTransition.phase === "blocked"
          ? apiTransitionMessage || (vm.language === "ko"
            ? "현재 실행 상태를 안전하게 확인할 수 없어 런타임 전환이 차단되었습니다."
            : "The runtime switch was blocked because the active state could not be verified safely.")
        : runtimeTransition.phase === "failed"
          ? vm.language === "ko"
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
        <div className={["dock-action-message", actionMessageTone].join(" ")}>
          {trimmedActionMessage}
        </div>
      ) : null}

      <AnimatePresence initial={false}>
        {operationBusy ? (
          <m.div
            {...statusSwap}
            aria-label={operationLabel}
            aria-valuetext={operationLabel}
            className="operation-progress"
            key="operation-progress"
            role="progressbar"
          >
            <m.span
              animate={reducedMotion ? undefined : shimmer.animate}
              aria-hidden="true"
              className="operation-progress-bar"
              transition={reducedMotion ? undefined : shimmer.transition}
            />
          </m.div>
        ) : null}
      </AnimatePresence>

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
              {commandBusy
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
              role={transitionTone === "error" ? "alert" : "status"}
            >
              <span>{transitionCopy}</span>
              {runtimeTransition.retryable || noActiveRuntime ? (
                <button className="runtime-transition-retry" onClick={onRetryRuntimeMode} type="button">
                  {noActiveRuntime
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
          <Play aria-hidden="true" size={17} />
          {runtimeReady ? vm.ui.start : vm.ui.preparing}
        </button>
        <button
          className="button button-secondary"
          disabled={pauseResumeDisabled}
          onClick={() => onControl(isPaused ? "resume" : "pause")}
          type="button"
        >
          {isPaused ? <Play aria-hidden="true" size={17} /> : <Pause aria-hidden="true" size={17} />}
          {isPaused ? vm.ui.resume : vm.ui.pause}
        </button>
        <button className="button button-secondary" disabled={resetDisabled} onClick={() => setResetConfirmationOpen(true)} type="button">
          <RotateCcw aria-hidden="true" size={17} />
          {vm.ui.reset}
        </button>
        <button className="button button-stop" disabled={stopDisabled} onClick={() => onControl("stop")} type="button">
          <Square aria-hidden="true" size={16} />
          {vm.ui.stop}
        </button>
        </div>
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
