import { Bug, Languages, Radio } from "lucide-react";
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
  onDebugMode,
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
  onDebugMode: () => void;
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
        <button className="debug-mode-entry" onClick={onDebugMode} type="button">
          <Bug size={16} aria-hidden="true" />
          <span>{language === "ko" ? "디버그 모드" : "Debug Mode"}</span>
        </button>
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
