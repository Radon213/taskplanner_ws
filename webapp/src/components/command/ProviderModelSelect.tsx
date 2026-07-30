import {
  type CSSProperties,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import {
  Box,
  Check,
  ChevronDown,
  LoaderCircle,
  Monitor,
  Moon,
  Play,
  PowerOff,
  Search,
  ServerCog,
  Sparkles,
  Sun,
  X,
} from "lucide-react";

import type {
  ModelCatalogEntry,
  ModelProviderStatus,
  ModelRuntimeCommand,
  ModelSelection,
} from "../../types";
import type { Language } from "../../utils/display";

const KEY_SEPARATOR = "\u001f";
const TRANSITIONAL_STATES = new Set([
  "loading",
  "suspending",
  "waking",
  "unloading",
]);

export function modelSelectionKey(selection: ModelSelection | null): string {
  if (!selection) return "";
  return `${selection.provider_id}${KEY_SEPARATOR}${selection.model_id}`;
}

function entryKey(entry: ModelCatalogEntry): string {
  return modelSelectionKey({
    provider_id: entry.provider_id,
    model_id: entry.model_id,
  });
}

function providerStateLabel(
  provider: ModelProviderStatus,
  models: ModelCatalogEntry[],
  language: Language,
): string {
  if (provider.reachable) {
    const loadedCount = models.filter((entry) => entry.load_state === "loaded").length;
    return language === "ko"
      ? `연결됨 · 실행 ${loadedCount}/${provider.model_count}`
      : `Online · loaded ${loadedCount}/${provider.model_count}`;
  }
  if (provider.status === "auth_error") {
    return language === "ko" ? "인증 필요" : "Authentication required";
  }
  if (provider.status === "timeout") {
    return language === "ko" ? "응답 지연" : "Timed out";
  }
  return language === "ko" ? "연결 안 됨" : "Offline";
}

function loadStateLabel(entry: ModelCatalogEntry, language: Language): string {
  if (entry.load_state === "loaded") return language === "ko" ? "실행 중" : "Loaded";
  if (entry.load_state === "unloaded") return language === "ko" ? "언로드됨" : "Unloaded";
  if (entry.load_state === "loading") return language === "ko" ? "로딩 중" : "Loading";
  if (entry.load_state === "sleeping") return language === "ko" ? "절전" : "Sleeping";
  if (entry.load_state === "suspending") return language === "ko" ? "절전 전환 중" : "Suspending";
  if (entry.load_state === "waking") return language === "ko" ? "깨우는 중" : "Waking";
  if (entry.load_state === "unloading") return language === "ko" ? "언로드 중" : "Unloading";
  if (entry.load_state === "error") return language === "ko" ? "오류" : "Error";
  if (entry.load_state === "configured") return language === "ko" ? "현재 설정" : "Configured";
  return language === "ko" ? "상태 불명" : "Unknown";
}

function runtimeActionLabel(
  command: ModelRuntimeCommand,
  providerName: string,
  language: Language,
): string {
  const labels = {
    load:
      language === "ko"
        ? `${providerName} 모델 로드`
        : `Load ${providerName} model`,
    unload:
      language === "ko"
        ? `${providerName} 모델 언로드`
        : `Unload ${providerName} model`,
    sleep:
      language === "ko"
        ? `${providerName} 절전`
        : `Sleep ${providerName}`,
    wake:
      language === "ko"
        ? `${providerName} 깨우기`
        : `Wake ${providerName}`,
  };
  return labels[command];
}

function providerGlyph(providerId: string, size = 17) {
  const normalized = providerId.toLocaleLowerCase();
  if (normalized.includes("lmstudio")) {
    return <Monitor aria-hidden="true" size={size} strokeWidth={2} />;
  }
  if (normalized.includes("unsloth")) {
    return <Sparkles aria-hidden="true" size={size} strokeWidth={2} />;
  }
  if (normalized.includes("vllm")) {
    return <ServerCog aria-hidden="true" size={size} strokeWidth={2} />;
  }
  return <Box aria-hidden="true" size={size} strokeWidth={2} />;
}

function capabilityLabel(entry: ModelCatalogEntry, language: Language): string {
  const capability = entry.capability.trim();
  if (!capability || capability === "unknown") {
    return language === "ko" ? "OpenAI 호환 모델" : "OpenAI-compatible model";
  }
  return capability.replace(/_/g, " ");
}

function runtimeActionIcon(command: ModelRuntimeCommand) {
  if (command === "load") return <Play aria-hidden="true" size={15} />;
  if (command === "unload") return <PowerOff aria-hidden="true" size={15} />;
  if (command === "sleep") return <Moon aria-hidden="true" size={15} />;
  return <Sun aria-hidden="true" size={15} />;
}

export function ProviderModelSelect({
  ariaLabel,
  language,
  models,
  providers,
  selection,
  disabled,
  title,
  onChange,
  runtimePending = false,
  onRuntimeAction,
}: {
  ariaLabel: string;
  language: Language;
  models: ModelCatalogEntry[];
  providers: ModelProviderStatus[];
  selection: ModelSelection | null;
  disabled: boolean;
  title: string;
  onChange: (selection: ModelSelection) => void;
  runtimePending?: boolean;
  onRuntimeAction?: (
    selection: ModelSelection,
    command: ModelRuntimeCommand,
  ) => void;
}) {
  const panelId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeProviderId, setActiveProviderId] = useState("all");
  const [panelStyle, setPanelStyle] = useState<CSSProperties>({
    left: 12,
    top: 72,
    width: 480,
    maxHeight: 520,
  });

  const selectedKey = modelSelectionKey(selection);
  const selectedEntry =
    models.find((entry) => entryKey(entry) === selectedKey) || null;
  const selectedProvider = providers.find(
    (provider) => provider.provider_id === selectedEntry?.provider_id,
  );
  const selectedLoadLabel = selectedEntry
    ? loadStateLabel(selectedEntry, language)
    : language === "ko"
      ? "모델 선택"
      : "Select model";

  const providerRows: ModelProviderStatus[] = providers.length
    ? providers
    : [
        {
          provider_id: "legacy",
          provider_name: "OpenAI compatible",
          endpoint: "",
          reachable: models.length > 0,
          status: models.length > 0 ? "online" : "offline",
          detail: "",
          latency_sec: 0,
          model_count: models.length,
        },
      ];

  const filteredModels = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    return models.filter((entry) => {
      if (
        activeProviderId !== "all" &&
        entry.provider_id !== activeProviderId
      ) {
        return false;
      }
      if (!normalizedQuery) return true;
      return [
        entry.display_name,
        entry.model_id,
        entry.provider_name,
        entry.capability,
      ].some((value) => value.toLocaleLowerCase().includes(normalizedQuery));
    });
  }, [activeProviderId, models, query]);

  const updatePanelPosition = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger || typeof window === "undefined") return;
    const rect = trigger.getBoundingClientRect();
    const viewportPadding = 12;
    const gap = 8;
    const width = Math.min(520, window.innerWidth - viewportPadding * 2);
    const maxHeight = Math.min(520, window.innerHeight - viewportPadding * 2);
    const left = Math.min(
      window.innerWidth - width - viewportPadding,
      Math.max(viewportPadding, rect.left),
    );
    const roomBelow = window.innerHeight - rect.bottom - viewportPadding;
    const roomAbove = rect.top - viewportPadding;
    const openAbove = roomBelow < Math.min(360, maxHeight) && roomAbove > roomBelow;
    const top = openAbove
      ? Math.max(viewportPadding, rect.top - maxHeight - gap)
      : Math.min(
          window.innerHeight - maxHeight - viewportPadding,
          rect.bottom + gap,
        );
    setPanelStyle({ left, top, width, maxHeight });
  }, []);

  useEffect(() => {
    if (!open) return;
    updatePanelPosition();
    const focusFrame = window.requestAnimationFrame(() => {
      searchRef.current?.focus();
    });
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        triggerRef.current?.contains(target) ||
        panelRef.current?.contains(target)
      ) {
        return;
      }
      setOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    window.addEventListener("resize", updatePanelPosition);
    window.addEventListener("scroll", updatePanelPosition, true);
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("resize", updatePanelPosition);
      window.removeEventListener("scroll", updatePanelPosition, true);
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open, updatePanelPosition]);

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  const selectModel = (entry: ModelCatalogEntry) => {
    if (!entry.selectable && entryKey(entry) !== selectedKey) return;
    onChange({
      provider_id: entry.provider_id,
      model_id: entry.model_id,
    });
    setOpen(false);
    setQuery("");
    triggerRef.current?.focus();
  };

  const invokeRuntimeAction = (
    entry: ModelCatalogEntry,
    command: ModelRuntimeCommand,
  ) => {
    if (!onRuntimeAction) return;
    onRuntimeAction(
      {
        provider_id: entry.provider_id,
        model_id: entry.model_id,
      },
      command,
    );
  };

  const panel =
    open && typeof document !== "undefined"
      ? createPortal(
          <div
            ref={panelRef}
            id={panelId}
            className="model-browser-popover"
            data-slot="model-browser-popover"
            role="dialog"
            aria-modal="false"
            aria-label={ariaLabel}
            style={panelStyle}
          >
            <header className="model-browser-header">
              <div>
                <span>{language === "ko" ? "모델 카탈로그" : "Model catalog"}</span>
                <strong>{language === "ko" ? "모델 선택" : "Select a model"}</strong>
              </div>
              <button
                type="button"
                className="model-browser-close"
                aria-label={language === "ko" ? "모델 창 닫기" : "Close model browser"}
                onClick={() => setOpen(false)}
              >
                <X aria-hidden="true" size={17} />
              </button>
            </header>

            <div className="model-browser-search">
              <Search aria-hidden="true" size={17} />
              <input
                ref={searchRef}
                value={query}
                aria-label={
                  language === "ko"
                    ? "모델 또는 공급자 검색"
                    : "Search models or providers"
                }
                onChange={(event) => setQuery(event.target.value)}
                placeholder={
                  language === "ko"
                    ? "모델 또는 공급자 검색"
                    : "Search models or providers"
                }
              />
              {query ? (
                <button
                  type="button"
                  aria-label={language === "ko" ? "검색어 지우기" : "Clear search"}
                  onClick={() => {
                    setQuery("");
                    searchRef.current?.focus();
                  }}
                >
                  <X aria-hidden="true" size={14} />
                </button>
              ) : null}
            </div>

            <div
              className="model-provider-tabs"
              role="tablist"
              aria-label={language === "ko" ? "모델 공급자" : "Model provider"}
            >
              <button
                type="button"
                role="tab"
                aria-selected={activeProviderId === "all"}
                className={activeProviderId === "all" ? "active" : ""}
                onClick={() => setActiveProviderId("all")}
              >
                {language === "ko" ? "전체" : "All"}
                <span>{models.length}</span>
              </button>
              {providerRows.map((provider) => {
                const providerModels = models.filter(
                  (entry) => entry.provider_id === provider.provider_id,
                );
                return (
                  <button
                    type="button"
                    role="tab"
                    aria-selected={activeProviderId === provider.provider_id}
                    className={
                      activeProviderId === provider.provider_id ? "active" : ""
                    }
                    data-reachable={provider.reachable ? "true" : "false"}
                    key={provider.provider_id}
                    title={providerStateLabel(provider, providerModels, language)}
                    onClick={() => setActiveProviderId(provider.provider_id)}
                  >
                    <i />
                    {provider.provider_name}
                    <span>{providerModels.length}</span>
                  </button>
                );
              })}
            </div>

            <div className="model-browser-list-header">
              <span>
                {language === "ko"
                  ? `${filteredModels.length}개 모델`
                  : `${filteredModels.length} models`}
              </span>
              <span>
                {providerRows.filter((provider) => provider.reachable).length}/
                {providerRows.length} {language === "ko" ? "공급자 연결" : "providers online"}
              </span>
            </div>

            <div className="model-browser-list" role="list">
              {filteredModels.length ? (
                filteredModels.map((entry) => {
                  const key = entryKey(entry);
                  const isSelected = key === selectedKey;
                  const transitional = TRANSITIONAL_STATES.has(entry.load_state);
                  const runtimeManaged =
                    Boolean(entry.runtime_managed) && Boolean(onRuntimeAction);
                  const availableActions = new Set(entry.available_actions || []);
                  return (
                    <div
                      className={`model-browser-row${isSelected ? " selected" : ""}`}
                      data-load-state={entry.load_state}
                      role="listitem"
                      key={key}
                    >
                      <button
                        type="button"
                        className="model-browser-option"
                        disabled={!entry.selectable && !isSelected}
                        aria-current={isSelected ? "true" : undefined}
                        onClick={() => selectModel(entry)}
                      >
                        <span className="model-provider-glyph">
                          {providerGlyph(entry.provider_id)}
                        </span>
                        <span className="model-browser-copy">
                          <strong>{entry.display_name || entry.model_id}</strong>
                          <small>
                            <b>{entry.provider_name}</b>
                            <i aria-hidden="true">·</i>
                            {capabilityLabel(entry, language)}
                          </small>
                        </span>
                        <span
                          className="model-row-state"
                          data-state={entry.load_state}
                          title={entry.detail || loadStateLabel(entry, language)}
                        >
                          <i />
                          {loadStateLabel(entry, language)}
                        </span>
                        {isSelected ? (
                          <Check
                            className="model-selected-check"
                            aria-hidden="true"
                            size={18}
                            strokeWidth={2.5}
                          />
                        ) : null}
                      </button>

                      {runtimeManaged ? (
                        <div
                          className="model-row-actions"
                          aria-label={
                            language === "ko"
                              ? `${entry.display_name || entry.model_id} 실행 제어`
                              : `${entry.display_name || entry.model_id} runtime controls`
                          }
                        >
                          {transitional || runtimePending ? (
                            <LoaderCircle
                              className="model-runtime-spinner"
                              aria-label={loadStateLabel(entry, language)}
                              size={17}
                            />
                          ) : (
                            (["load", "wake", "sleep", "unload"] as ModelRuntimeCommand[])
                              .filter((command) => availableActions.has(command))
                              .map((command) => (
                                <button
                                  type="button"
                                  key={command}
                                  title={runtimeActionLabel(
                                    command,
                                    entry.provider_name,
                                    language,
                                  )}
                                  aria-label={runtimeActionLabel(
                                    command,
                                    entry.provider_name,
                                    language,
                                  )}
                                  onClick={() => invokeRuntimeAction(entry, command)}
                                >
                                  {runtimeActionIcon(command)}
                                </button>
                              ))
                          )}
                        </div>
                      ) : null}
                    </div>
                  );
                })
              ) : (
                <div className="model-browser-empty">
                  <Search aria-hidden="true" size={20} />
                  <strong>
                    {language === "ko" ? "일치하는 모델이 없습니다" : "No matching models"}
                  </strong>
                  <span>
                    {language === "ko"
                      ? "검색어나 공급자 필터를 바꿔보세요."
                      : "Try another search or provider filter."}
                  </span>
                </div>
              )}
            </div>
          </div>,
          document.body,
        )
      : null;

  return (
    <div
      className="provider-model-select-shell"
      data-slot="provider-model-select-control"
      data-load-state={selectedEntry?.load_state || "unknown"}
    >
      <button
        ref={triggerRef}
        type="button"
        className="provider-model-trigger"
        disabled={disabled}
        aria-label={ariaLabel}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        title={title}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="model-trigger-provider-icon">
          {providerGlyph(selectedEntry?.provider_id || "")}
        </span>
        <span className="model-trigger-copy">
          <strong>
            {selectedEntry?.display_name ||
              selectedEntry?.model_id ||
              (language === "ko" ? "모델 선택" : "Select model")}
          </strong>
          <small>
            {selectedEntry?.provider_name ||
              selectedProvider?.provider_name ||
              (language === "ko" ? "사용 가능한 공급자 없음" : "No provider available")}
          </small>
        </span>
        <span
          className="model-load-state"
          data-state={selectedEntry?.load_state || "unknown"}
        >
          <i />
          {selectedLoadLabel}
        </span>
        <ChevronDown
          className="model-trigger-chevron"
          aria-hidden="true"
          size={17}
        />
      </button>
      {panel}
    </div>
  );
}
