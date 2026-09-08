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

type ReadOnlyVlmRuntime = {
  provider: string;
  model: string;
  providerState: string;
  modelState: string;
  inferenceState: string;
  tone: "ok" | "warn" | "idle";
  title: string;
  loadState: string;
};

function modelLoadStateCopy(loadState: string, language: Language): string {
  const normalized = loadState.trim().toLowerCase();
  const korean = language === "ko";
  switch (normalized) {
    case "loaded":
    case "ready":
    case "running":
      return korean ? "모델 로드됨" : "Model loaded";
    case "unloaded":
      return korean ? "모델 언로드됨" : "Model unloaded";
    case "loading":
    case "starting":
    case "waking":
      return korean ? "모델 로드 중" : "Model loading";
    case "sleeping":
    case "sleep":
      return korean ? "모델 절전" : "Model sleeping";
    case "configured":
      return korean ? "모델 설정됨" : "Model configured";
    case "failed":
    case "error":
      return korean ? "모델 오류" : "Model error";
    default:
      return korean
        ? normalized ? `모델 상태: ${loadState}` : "모델 상태 확인 중"
        : normalized ? `Model state: ${loadState}` : "Checking model state";
  }
}

function buildReadOnlyVlmRuntime({
  vm,
  language,
  modelOptions,
  providerStatuses,
  modelCatalogStatus,
  modelSelection,
}: {
  vm: ViewModel;
  language: Language;
  modelOptions: ModelCatalogEntry[];
  providerStatuses: ModelProviderStatus[];
  modelCatalogStatus: string;
  modelSelection: ModelSelection | null;
}): ReadOnlyVlmRuntime {
  const healthModelId = vm.vlmStatus.modelId.trim();
  const selectedModel = modelSelection
    ? modelOptions.find((entry) =>
      entry.provider_id === modelSelection.provider_id
      && entry.model_id === modelSelection.model_id,
    )
    : modelOptions.find((entry) => entry.model_id === healthModelId);
  const selectedProviderId =
    modelSelection?.provider_id || selectedModel?.provider_id || "";
  const provider = providerStatuses.find((entry) => entry.provider_id === selectedProviderId)
    ?? providerStatuses.find((entry) => entry.provider_id === selectedModel?.provider_id);
  const providerName = provider?.provider_name
    || selectedModel?.provider_name
    || selectedProviderId
    || (language === "ko" ? "공급자 미확인" : "Provider unknown");
  const modelId = modelSelection?.model_id
    || selectedModel?.model_id
    || healthModelId
    || (language === "ko" ? "모델 미확인" : "Model unknown");
  const loadState = selectedModel?.load_state.trim() || "unknown";
  const modelState = selectedModel
    ? modelLoadStateCopy(loadState, language)
    : modelCatalogStatus === "loading"
      ? language === "ko" ? "모델 상태 확인 중" : "Checking model state"
      : language === "ko" ? "모델 상태 미확인" : "Model state unavailable";
  const providerState = !provider
    ? language === "ko" ? "공급자 상태 미확인" : "Provider unavailable"
    : provider.reachable
      ? language === "ko" ? `${providerName} 연결됨` : `${providerName} connected`
      : language === "ko" ? `${providerName} 연결 끊김` : `${providerName} disconnected`;
  const inferenceState = language === "ko"
    ? `추론: ${vm.vlmStatus.health}`
    : `Inference: ${vm.vlmStatus.health}`;
  const normalizedLoadState = loadState.toLowerCase();
  const tone = (provider !== undefined && !provider.reachable)
    || ["failed", "error"].includes(normalizedLoadState)
    || vm.vlmStatus.className === "warn"
      ? "warn"
      : provider?.reachable && ["loaded", "ready", "running"].includes(normalizedLoadState)
        ? "ok"
        : "idle";
  const title = [
    `${language === "ko" ? "공급자" : "Provider"}: ${providerName}`,
    `${language === "ko" ? "모델" : "Model"}: ${modelId}`,
    `${language === "ko" ? "공급자 상태" : "Provider status"}: ${providerState}`,
    `${language === "ko" ? "모델 상태" : "Model status"}: ${modelState}`,
    inferenceState,
    vm.vlmStatus.lastError
      ? `${language === "ko" ? "최근 오류" : "Last error"}: ${vm.vlmStatus.lastError}`
      : "",
    modelCatalogStatus ? `${language === "ko" ? "카탈로그" : "Catalog"}: ${modelCatalogStatus}` : "",
  ].filter(Boolean).join("\n");

  return {
    provider: providerName,
    model: modelId,
    providerState,
    modelState,
    inferenceState,
    tone,
    title,
    loadState: normalizedLoadState,
  };
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
  onOpenSurgiMate,
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
  onOpenSurgiMate: () => void;
}) {
  const vlmSelectDisabled =
    !transportConnected || Boolean(actionPending) || !modelOptions.some((entry) => entry.selectable);
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
  const readOnlyVlmRuntime = buildReadOnlyVlmRuntime({
    vm,
    language,
    modelOptions,
    providerStatuses,
    modelCatalogStatus,
    modelSelection,
  });

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
            className={`workspace-navigation with-surgimate ${integratedDebugAvailable ? "with-integrated-debug" : ""}`}
          >
            <span aria-current="page" className="workspace-navigation-current">
              <Activity aria-hidden="true" size={16} />
              {language === "ko" ? "미션" : "Mission"}
            </span>
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
            <button
              aria-label={language === "ko" ? "독립 SurgiMate로 이동" : "Go to standalone SurgiMate"}
              onClick={onOpenSurgiMate}
              title={language === "ko"
                ? "현재 탭에서 독립 SurgiMate 앱으로 이동합니다. Taskplanner 화면에는 포함하지 않습니다."
                : "Go to the standalone SurgiMate app in this tab. It is not embedded in Taskplanner."}
              type="button"
            >
              <Monitor aria-hidden="true" size={16} />
              <span>SurgiMate</span>
            </button>
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
              aria-atomic="true"
              aria-label={`${readOnlyVlmRuntime.provider}. ${readOnlyVlmRuntime.model}. ${readOnlyVlmRuntime.providerState}. ${readOnlyVlmRuntime.modelState}. ${readOnlyVlmRuntime.inferenceState}.`}
              aria-live="polite"
              className={`ribbon-model-control read-only ${readOnlyVlmRuntime.tone}`}
              data-slot="vlm-read-only-status"
              role="status"
              title={readOnlyVlmRuntime.title}
            >
              <span className="ribbon-model-label">VLM</span>
              <span
                className="vlm-runtime-readout"
                data-slot="vlm-runtime-readout"
                data-vlm-load-state={readOnlyVlmRuntime.loadState}
              >
                <span className="vlm-runtime-identity">
                  <span className="vlm-runtime-provider">{readOnlyVlmRuntime.provider}</span>
                  <strong className="vlm-runtime-model" title={readOnlyVlmRuntime.model}>
                    {readOnlyVlmRuntime.model}
                  </strong>
                </span>
                <span className="vlm-runtime-facts">
                  <span>{readOnlyVlmRuntime.providerState}</span>
                  <span aria-hidden="true">·</span>
                  <span>{readOnlyVlmRuntime.modelState}</span>
                  <span aria-hidden="true">·</span>
                  <span>{readOnlyVlmRuntime.inferenceState}</span>
                </span>
              </span>
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
