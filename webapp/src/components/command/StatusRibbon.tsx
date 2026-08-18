import { Bug, Camera, Languages, Radio } from "lucide-react";
import { ProviderModelSelect } from "./ProviderModelSelect";
import type { Language } from "../../utils/display";
import type { useDigitalTwinViewModel } from "../../hooks/useDigitalTwinViewModel";
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
  language,
  onLanguageChange,
  modelOptions,
  providerStatuses,
  modelCatalogStatus,
  modelSelection,
  actionPending,
  onVlmModelChange,
  onVlmRuntimeAction,
  debugModeDisabled,
  onDebugMode,
  onMulticamOps,
}: {
  vm: ViewModel;
  connected: boolean;
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
  debugModeDisabled: boolean;
  onDebugMode: () => void;
  onMulticamOps: () => void;
}) {
  const vlmSelectDisabled =
    !connected || Boolean(actionPending) || !modelOptions.some((entry) => entry.selectable);

  return (
    <header className="top-ribbon">
      <div className="brand-block">
        <div className="brand-mark">
          <OperatingRoomMark />
        </div>
        <div>
          <p>{vm.ui.eyebrow}</p>
          <h1>{vm.ui.appName}</h1>
          <span>{vm.ui.subtitle}</span>
        </div>
      </div>
      <div className="ribbon-cluster">
        <button className="debug-mode-entry" onClick={onMulticamOps} type="button">
          <Camera size={16} aria-hidden="true" />
          <span>{language === "ko" ? "멀티캠 관제" : "Multicam Ops"}</span>
        </button>
        <button
          aria-describedby={debugModeDisabled ? "debug-mode-lock-reason" : undefined}
          className="debug-mode-entry"
          disabled={debugModeDisabled}
          onClick={onDebugMode}
          title={
            debugModeDisabled
              ? language === "ko"
                ? "진행 상태를 보존하려면 먼저 실행을 정지해 주세요."
                : "Stop the run before entering standalone Debug mode."
              : undefined
          }
          type="button"
        >
          <Bug size={16} aria-hidden="true" />
          <span>{language === "ko" ? "디버그 모드" : "Debug Mode"}</span>
        </button>
        {debugModeDisabled ? (
          <span className="sr-only" id="debug-mode-lock-reason">
            {language === "ko"
              ? "실행 중이거나 일시정지 또는 시작 처리 중에는 디버그 모드로 전환할 수 없습니다. 먼저 실행을 정지해 주세요."
              : "Debug mode is unavailable while running, paused, or starting. Stop the run first."}
          </span>
        ) : null}
        <div className={`system-pill ${connected ? "ok" : "warn"}`}>
          <Radio size={16} />
          <span>{connected ? vm.ui.rosOnline : vm.ui.rosOffline}</span>
        </div>
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
        <div className="language-control" aria-label={vm.ui.language}>
          <Languages size={15} />
          <button className={language === "ko" ? "active" : ""} onClick={() => onLanguageChange("ko")} type="button">
            {vm.ui.korean}
          </button>
          <button className={language === "en" ? "active" : ""} onClick={() => onLanguageChange("en")} type="button">
            {vm.ui.english}
          </button>
        </div>
      </div>
    </header>
  );
}
