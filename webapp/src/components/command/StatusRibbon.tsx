import { lazy, Suspense } from "react";
import { Activity, Languages, Monitor, Radio, ScanLine } from "lucide-react";
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

const ProviderModelSelect = lazy(() =>
  import("./ProviderModelSelect").then((module) => ({
    default: module.ProviderModelSelect,
  })),
);

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
  experimentalControlsEnabled,
  integratedDebugAvailable,
  onIntegratedDebug,
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
  experimentalControlsEnabled: boolean;
  integratedDebugAvailable: boolean;
  onIntegratedDebug?: () => void;
  onMonitor?: () => void;
}) {
  const vlmSelectDisabled =
    !connected || Boolean(actionPending) || !modelOptions.some((entry) => entry.selectable);
  const runtimeHandshakePending = runtimeTransitionPhase === "checking"
    || runtimeTransitionPhase === "starting";
  const displayedAuthorityStatus: RuntimeAuthorityStatus = runtimeAuthorityStatus === "blocked"
    ? "blocked"
    : runtimeHandshakePending
      ? runtimeTransitionPhase === "checking" ? "checking" : "connecting"
      : connected
        ? "ready"
        : transportConnected && runtimeAuthorityStatus === "offline"
          ? "waiting"
          : runtimeAuthorityStatus;
  const bridgeFeedback = runtimeAuthorityCopy(displayedAuthorityStatus, language);

  return (
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
            {experimentalControlsEnabled && onMonitor ? (
              <button onClick={onMonitor} type="button">
                <Monitor aria-hidden="true" size={16} />
                <span>{language === "ko" ? "수술 관제" : "SurgiMate"}</span>
              </button>
            ) : null}
            {integratedDebugAvailable && onIntegratedDebug ? (
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
          {experimentalControlsEnabled ? (
            <div
              className={`ribbon-model-control ${vm.vlmStatus.className}`}
              title={vm.vlmStatus.detail || modelCatalogStatus}
            >
              <span className="ribbon-model-label">VLM</span>
              <Suspense fallback={<span>{language === "ko" ? "모델 목록 로딩" : "Loading models"}</span>}>
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
              </Suspense>
              <strong>{vm.vlmStatus.health}</strong>
            </div>
          ) : (
            <div
              className={`ribbon-model-control read-only ${vm.vlmStatus.className}`}
              data-slot="vlm-read-only-status"
              title={vm.vlmStatus.detail}
            >
              <span className="ribbon-model-label">VLM</span>
              <strong>{vm.vlmStatus.health}</strong>
            </div>
          )}
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
  );
}
