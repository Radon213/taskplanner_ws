import { Languages, Radio } from "lucide-react";
import type { Language } from "../../utils/display";
import type { useDigitalTwinViewModel } from "../../hooks/useDigitalTwinViewModel";

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
  modelCatalogStatus,
  vlmModel,
  actionPending,
  onVlmModelChange,
}: {
  vm: ViewModel;
  connected: boolean;
  language: Language;
  onLanguageChange: (language: Language) => void;
  modelOptions: string[];
  modelCatalogStatus: string;
  vlmModel: string;
  actionPending: string;
  onVlmModelChange: (modelId: string) => void;
}) {
  const vlmSelectDisabled = !connected || Boolean(actionPending) || !modelOptions.length;
  const selectedVlmModel = vlmModel || modelOptions[0] || "";

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
        <div className={`system-pill ${connected ? "ok" : "warn"}`}>
          <Radio size={16} />
          <span>{connected ? vm.ui.rosOnline : vm.ui.rosOffline}</span>
        </div>
        <div className={`ribbon-model-control ${vm.vlmStatus.className}`}>
          <span className="ribbon-model-label">VLM</span>
          <select
            value={selectedVlmModel}
            disabled={vlmSelectDisabled}
            title={modelCatalogStatus}
            onChange={(event) => onVlmModelChange(event.target.value)}
          >
            {modelOptions.length ? (
              modelOptions.map((modelId) => (
                <option value={modelId} key={modelId}>
                  {modelId}
                </option>
              ))
            ) : (
              <option value="">{modelCatalogStatus || vm.ui.none}</option>
            )}
          </select>
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
