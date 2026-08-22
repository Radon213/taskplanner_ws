import { useState } from "react";
import { Activity, Bug, Languages, Monitor, Radio, ScanLine } from "lucide-react";
import { SafetyConfirmationDialog } from "../common/SafetyConfirmationDialog";
import { ProviderModelSelect } from "./ProviderModelSelect";
import type { Language } from "../../utils/display";
import type { useDigitalTwinViewModel } from "../../hooks/useDigitalTwinViewModel";
import type { RuntimeTransitionPhase } from "../../hooks/useRuntimeControl";
import type { RuntimeAuthorityStatus } from "../../hooks/useRosBridge";
import { runtimeAuthorityCopy } from "../../utils/runtimeAuthorityCopy";
import type {
  ModelCatalogEntry,
  ModelProviderStatus,
  ModelRuntimeCommand,
  ModelSelection,
} from "../../types";

type ViewModel = ReturnType<typeof useDigitalTwinViewModel>;

function OperatingRoomMark() {
  return (
    <svg className="hospital-cross-icon" viewBox="0 0 42 42" aria-hidden="true" focusable="false">
      <path d="M17 7h8v10h10v8H25v10h-8V25H7v-8h10Z" />
    </svg>
  );
}

export function StatusRibbon({
  vm,
  connected,
  transportConnected,
  runtimeAuthorityStatus,
  runtimeTransitionPhase,
  language,
  onLanguageChange,
  modelOptions,
  providerStatuses,
  modelCatalogStatus,
  modelSelection,
  actionPending,
  onVlmModelChange,
  onVlmRuntimeAction,
  integratedDebugAvailable,
  onIntegratedDebug,
  debugModeDisabled,
  onDebugMode,
  onMonitor,
}: {
  vm: ViewModel;
  connected: boolean;
  transportConnected: boolean;
  runtimeAuthorityStatus: RuntimeAuthorityStatus;
  runtimeTransitionPhase: RuntimeTransitionPhase;
  language: Language;
  onLanguageChange: (language: Language) => void;
  modelOptions: ModelCatalogEntry[];
  providerStatuses: ModelProviderStatus[];
  modelCatalogStatus: string;
  modelSelection: ModelSelection | null;
  actionPending: string;
  onVlmModelChange: (selection: ModelSelection) => void;
  onVlmRuntimeAction: (
    selection: ModelSelection,
    command: ModelRuntimeCommand,
  ) => void;
  integratedDebugAvailable: boolean;
  onIntegratedDebug: () => void;
  debugModeDisabled: boolean;
  onDebugMode: () => void;
  onMonitor: () => void;
}) {
  const [debugConfirmationOpen, setDebugConfirmationOpen] = useState(false);
  const vlmSelectDisabled =
    !connected || Boolean(actionPending) || !modelOptions.some((entry) => entry.selectable);
  const runtimeHandshakePending = runtimeTransitionPhase === "checking"
    || runtimeTransitionPhase === "starting";
  const debugModeChecking = runtimeTransitionPhase === "checking";
  const debugModeStarting = runtimeTransitionPhase === "starting";
  const displayedAuthorityStatus: RuntimeAuthorityStatus = runtimeHandshakePending
    ? runtimeTransitionPhase === "checking" ? "checking" : "connecting"
    : connected
      ? "ready"
      : transportConnected && runtimeAuthorityStatus === "offline"
        ? "waiting"
        : runtimeAuthorityStatus;
  const bridgeFeedback = runtimeAuthorityCopy(displayedAuthorityStatus, language);

  return (
    <>
      <header className="top-ribbon" data-slot="mission-command-bar">
        <div className="brand-block">
          <div className="brand-mark">
            <OperatingRoomMark />
          </div>
          <div>
            <p>{vm.ui.eyebrow}</p>
            <h1>{vm.ui.appName}</h1>
          </div>
        </div>
        <div className="ribbon-cluster">
          <nav
            aria-label={language === "ko" ? "작업공간 탐색" : "Workspace navigation"}
            className={`workspace-navigation ${integratedDebugAvailable ? "with-integrated-debug" : ""}`}
          >
            <span aria-current="page" className="workspace-navigation-current">
              <Activity aria-hidden="true" size={16} />
              {language === "ko" ? "미션" : "Mission"}
            </span>
            <button onClick={onMonitor} type="button">
              <Monitor aria-hidden="true" size={16} />
              <span>{language === "ko" ? "수술 관제" : "SurgiMate"}</span>
            </button>
            {integratedDebugAvailable ? (
              <button
                aria-label={language === "ko" ? "통합 Debug 관측 열기" : "Open integrated Debug observation"}
                onClick={onIntegratedDebug}
                title={language === "ko"
                  ? "운영 런타임을 유지한 채 인식·토픽·멀티캠 관측을 엽니다."
                  : "Open perception, topic, and multicamera observation without replacing the operational runtime."}
                type="button"
              >
                <ScanLine aria-hidden="true" size={16} />
                <span>{language === "ko" ? "통합 관측" : "Integrated Observe"}</span>
              </button>
            ) : null}
          </nav>
          <div
            className={`ribbon-model-control ${vm.vlmStatus.className}`}
            title={vm.vlmStatus.detail || modelCatalogStatus}
          >
            <span className="ribbon-model-label">VLM</span>
            <ProviderModelSelect
              ariaLabel="VLM model provider and model"
              language={language}
              models={modelOptions}
              providers={providerStatuses}
              selection={modelSelection}
              disabled={vlmSelectDisabled}
              title={modelCatalogStatus}
              onChange={onVlmModelChange}
              runtimePending={actionPending.startsWith("Updating VLM runtime")}
              onRuntimeAction={onVlmRuntimeAction}
            />
            <strong>{vm.vlmStatus.health}</strong>
          </div>
          <div className="ribbon-status-actions">
            <div
              aria-atomic="true"
              aria-label={`${bridgeFeedback.label}. ${bridgeFeedback.detail}`}
              aria-live="polite"
              className={`system-pill ${bridgeFeedback.tone}`}
              data-authority-status={displayedAuthorityStatus}
              role="status"
              title={bridgeFeedback.detail}
            >
              <Radio aria-hidden="true" size={16} />
              <span>{bridgeFeedback.label}</span>
            </div>
            <button
              aria-describedby={debugModeDisabled ? "standalone-debug-lock-reason" : undefined}
              aria-expanded={debugConfirmationOpen}
              aria-haspopup="dialog"
              className="debug-mode-entry"
              disabled={debugModeDisabled}
              onClick={() => setDebugConfirmationOpen(true)}
              title={
                debugModeDisabled
                  ? debugModeChecking
                    ? language === "ko"
                        ? "현재 런타임 상태를 확인하는 동안에는 독립 Debug로 전환할 수 없습니다."
                      : "Runtime switching is unavailable while the active runtime is being checked."
                    : debugModeStarting
                      ? language === "ko"
                        ? "런타임 전환이 끝날 때까지 독립 Debug로 전환할 수 없습니다."
                        : "Runtime switching is unavailable until the current transition finishes."
                      : language === "ko"
                        ? "진행 상태를 보존하려면 먼저 실행을 정지한 뒤 독립 Debug로 전환해 주세요."
                        : "Stop the run before entering standalone Debug mode."
                  : undefined
              }
              type="button"
            >
              <Bug aria-hidden="true" size={16} />
              <span>{language === "ko" ? "독립 Debug" : "Standalone Debug"}</span>
            </button>
            {debugModeDisabled ? (
              <span className="sr-only" id="standalone-debug-lock-reason">
                {debugModeChecking
                  ? language === "ko"
                    ? "현재 런타임 상태를 확인하는 동안에는 독립 Debug로 전환할 수 없습니다."
                    : "Debug mode is unavailable while the active runtime is being checked."
                  : debugModeStarting
                    ? language === "ko"
                    ? "런타임 전환이 끝날 때까지 독립 Debug로 전환할 수 없습니다."
                      : "Debug mode is unavailable until the current runtime transition finishes."
                    : language === "ko"
                      ? "실행 중이거나 일시정지 상태에서는 독립 Debug로 전환할 수 없습니다. 먼저 실행을 정지해 주세요."
                      : "Debug mode is unavailable while running or paused. Stop the run first."}
              </span>
            ) : null}
            <div className="language-control" aria-label={vm.ui.language} role="group">
              <Languages aria-hidden="true" size={15} />
              <button
                aria-pressed={language === "ko"}
                className={language === "ko" ? "active" : ""}
                onClick={() => onLanguageChange("ko")}
                type="button"
              >
                {vm.ui.korean}
              </button>
              <button
                aria-pressed={language === "en"}
                className={language === "en" ? "active" : ""}
                onClick={() => onLanguageChange("en")}
                type="button"
              >
                {vm.ui.english}
              </button>
            </div>
          </div>
        </div>
      </header>
      <SafetyConfirmationDialog
        closeLabel={language === "ko" ? "닫기" : "Close"}
        confirmLabel={language === "ko" ? "독립 Debug 런타임 시작" : "Start standalone Debug runtime"}
        description={
          language === "ko"
            ? "미션 작업공간을 떠나 독립된 엔지니어링 런타임으로 전환합니다. 통합 관측 화면과 달리 운영 런타임을 교체하므로, 현재 미션 실행이 정지된 상태에서만 진행할 수 있습니다."
            : "Leave the mission workspace and switch to the isolated engineering runtime. The mission run must be stopped first."
        }
        note={
          language === "ko"
            ? "Taskplanner는 전환 후에도 궤적·모터·물리 안전 정지 권한을 갖지 않습니다."
            : "Taskplanner still does not own trajectory, motor, or physical safety-stop authority after the switch."
        }
        onClose={() => setDebugConfirmationOpen(false)}
        onConfirm={onDebugMode}
        open={debugConfirmationOpen}
        title={language === "ko" ? "독립 Debug 런타임으로 전환할까요?" : "Switch to the standalone Debug runtime?"}
      />
    </>
  );
}
