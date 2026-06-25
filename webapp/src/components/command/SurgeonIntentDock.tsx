import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { BrainCircuit, Clock3, MessageSquareText, Power, PowerOff, UserRound } from "lucide-react";

import type { useDigitalTwinViewModel } from "../../hooks/useDigitalTwinViewModel";
import type { SurgeonLLMDecision } from "../../types";
import { type Language } from "../../utils/display";

type ViewModel = ReturnType<typeof useDigitalTwinViewModel>;
type SpeechLogItem = {
  id: string;
  text: string;
  action: string;
  tool: string;
  atMs: number;
};

const SPEECH_LOG_TTL_MS = 180_000;
const SPEECH_LOG_LIMIT = 24;

function parseActorPayload(rawJson: string): { nextDwellSec: number | null; reasonCode: string } {
  if (!rawJson) return { nextDwellSec: null, reasonCode: "" };
  try {
    const payload = JSON.parse(rawJson) as { next_dwell_sec?: unknown; reason_code?: unknown };
    const nextDwellSec = Number(payload.next_dwell_sec);
    return {
      nextDwellSec: Number.isFinite(nextDwellSec) ? nextDwellSec : null,
      reasonCode: String(payload.reason_code || ""),
    };
  } catch {
    return { nextDwellSec: null, reasonCode: "" };
  }
}

function parseOverlay(rawJson: string): { heldTool: string; mayoTools: string[] } {
  if (!rawJson) return { heldTool: "", mayoTools: [] };
  try {
    const payload = JSON.parse(rawJson) as { held_tool?: unknown; mayo?: unknown };
    return {
      heldTool: String(payload.held_tool || ""),
      mayoTools: Array.isArray(payload.mayo) ? payload.mayo.map((item) => String(item)).filter(Boolean) : [],
    };
  } catch {
    return { heldTool: "", mayoTools: [] };
  }
}

function relativeAgeLabel(atMs: number, nowMs: number): string {
  const elapsedSec = Math.max(0, Math.floor((nowMs - atMs) / 1000));
  if (elapsedSec < 2) return "now";
  if (elapsedSec < 60) return `${elapsedSec}s ago`;
  const elapsedMin = Math.floor(elapsedSec / 60);
  return `${elapsedMin}m ago`;
}

export function SurgeonIntentDock({
  vm,
  language,
  llmDecision,
  actorEnabled,
  modelOptions,
  modelCatalogStatus,
  actorModel,
  connected,
  actionPending,
  onActorEnabledChange,
  onActorModelChange,
}: {
  vm: ViewModel;
  language: Language;
  llmDecision: SurgeonLLMDecision;
  actorEnabled: boolean;
  modelOptions: string[];
  modelCatalogStatus: string;
  actorModel: string;
  connected: boolean;
  actionPending: string;
  onActorEnabledChange: (enabled: boolean) => void;
  onActorModelChange: (modelId: string) => void;
}) {
  const [speechLog, setSpeechLog] = useState<SpeechLogItem[]>([]);
  const [nowMs, setNowMs] = useState(Date.now());
  const lastSpeechSignatureRef = useRef("");
  const speechLogListRef = useRef<HTMLDivElement>(null);
  const payload = parseActorPayload(llmDecision.raw_json);
  const overlay = parseOverlay(llmDecision.overlay_json);
  const selectedActorModel = actorModel || llmDecision.model_id || modelOptions[0] || "";
  const controlsDisabled = !connected || Boolean(actionPending);
  const modelDisabled = controlsDisabled || !modelOptions.length;
  const phaseLabel = llmDecision.hidden_phase ? vm.displayPhaseName(llmDecision.hidden_phase) : vm.ui.none;
  const toolLabel = llmDecision.tool ? vm.displayToolName(llmDecision.tool) : vm.ui.none;
  const heldToolLabel = overlay.heldTool ? vm.displayToolName(overlay.heldTool) : vm.ui.none;
  const mayoLabel = overlay.mayoTools.length ? overlay.mayoTools.map(vm.displayToolName).join(", ") : vm.ui.none;
  const nextDwellLabel =
    payload.nextDwellSec === null
      ? vm.ui.none
      : language === "ko"
        ? `${payload.nextDwellSec.toFixed(1)}초`
        : `${payload.nextDwellSec.toFixed(1)}s`;
  const decisionStatus = llmDecision.accepted
    ? language === "ko"
      ? "수락됨"
      : "accepted"
    : llmDecision.reject_reason || (language === "ko" ? "대기" : "waiting");

  useEffect(() => {
    const timer = window.setInterval(() => {
      const nextNow = Date.now();
      setNowMs(nextNow);
      setSpeechLog((current) => current.filter((item) => nextNow - item.atMs <= SPEECH_LOG_TTL_MS));
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const speech = llmDecision.speech.trim();
    if (!speech) return;
    const signature = [speech, llmDecision.action, llmDecision.tool, llmDecision.request_mode].join("|");
    if (signature === lastSpeechSignatureRef.current) return;
    lastSpeechSignatureRef.current = signature;
    const atMs = Date.now();
    const item: SpeechLogItem = {
      id: `${atMs}-${Math.random().toString(36).slice(2, 8)}`,
      text: speech,
      action: llmDecision.action,
      tool: llmDecision.tool,
      atMs,
    };
    setNowMs(atMs);
    setSpeechLog((current) => [item, ...current.filter((entry) => atMs - entry.atMs <= SPEECH_LOG_TTL_MS)].slice(0, SPEECH_LOG_LIMIT));
  }, [llmDecision.action, llmDecision.request_mode, llmDecision.speech, llmDecision.tool]);

  useEffect(() => {
    speechLogListRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  }, [speechLog[0]?.id]);

  return (
    <aside className="dock surgeon-dock llm-surgeon-dock">
      <div className="dock-header">
        <div>
          <p className="section-kicker">{language === "ko" ? "LLM 집도의" : "LLM Surgeon"}</p>
          <h2>{actorEnabled ? (language === "ko" ? "활성" : "Active") : language === "ko" ? "비활성" : "Off"}</h2>
        </div>
        <UserRound size={18} />
      </div>

      <div className="llm-control-row">
        <button
          className={`actor-toggle ${actorEnabled ? "active" : ""}`}
          disabled={controlsDisabled}
          onClick={() => onActorEnabledChange(!actorEnabled)}
          type="button"
        >
          {actorEnabled ? <Power size={16} /> : <PowerOff size={16} />}
          {actorEnabled ? (language === "ko" ? "켜짐" : "On") : language === "ko" ? "꺼짐" : "Off"}
        </button>
        <label className="field compact model-select-field">
          <span>{vm.ui.model}</span>
          <select
            value={selectedActorModel}
            disabled={modelDisabled}
            title={modelCatalogStatus}
            onChange={(event) => onActorModelChange(event.target.value)}
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
        </label>
      </div>

      <div className="intent-state llm-intent-state">
        <article>
          <span>{language === "ko" ? "내부 단계" : "Internal phase"}</span>
          <strong>{phaseLabel}</strong>
        </article>
        <article>
          <span>{language === "ko" ? "다음 요청 판단까지" : "Next request decision"}</span>
          <strong>{nextDwellLabel}</strong>
        </article>
        <article>
          <span>{language === "ko" ? "LLM 행동" : "LLM action"}</span>
          <strong>{llmDecision.action || vm.ui.none}</strong>
        </article>
        <article>
          <span>{vm.ui.requestedTool}</span>
          <strong>{toolLabel}</strong>
        </article>
        <article>
          <span>{language === "ko" ? "요청 방식" : "Request mode"}</span>
          <strong>{llmDecision.request_mode || vm.ui.none}</strong>
        </article>
        <article>
          <span>{language === "ko" ? "판단 상태" : "Decision status"}</span>
          <strong>{decisionStatus}</strong>
        </article>
        <article>
          <span>{language === "ko" ? "보유 도구" : "Held tool"}</span>
          <strong>{heldToolLabel}</strong>
        </article>
        <article>
          <span>{language === "ko" ? "Mayo 위 도구" : "Mayo tools"}</span>
          <strong>{mayoLabel}</strong>
        </article>
      </div>

      <div className="llm-speech-log">
        <div className="llm-speech-log-header">
          <span>
            <MessageSquareText size={15} />
            {language === "ko" ? "최근 발화" : "Recent speech"}
          </span>
          <small>{speechLog.length ? `${speechLog.length}` : vm.ui.none}</small>
        </div>
        <div className="llm-speech-log-list" aria-live="polite" ref={speechLogListRef}>
          <AnimatePresence initial={false}>
            {speechLog.map((item, index) => (
              <motion.article
                key={item.id}
                className="llm-speech-log-item"
                layout
                initial={{ opacity: 0, y: -10, scale: 0.98 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: 8, scale: 0.98 }}
                transition={{
                  opacity: { duration: 0.18 },
                  y: { duration: 0.22 },
                  scale: { duration: 0.22 },
                  layout: { duration: 0.24, ease: [0.22, 1, 0.36, 1] },
                }}
                data-latest={index === 0 ? "true" : "false"}
              >
                <div>
                  <strong>{item.text}</strong>
                  <small>
                    {relativeAgeLabel(item.atMs, nowMs)}
                    {item.action ? ` · ${item.action}` : ""}
                    {item.tool ? ` · ${vm.displayToolName(item.tool)}` : ""}
                  </small>
                </div>
              </motion.article>
            ))}
          </AnimatePresence>
          {speechLog.length === 0 ? (
            <div className="llm-speech-log-empty">{language === "ko" ? "최근 발화 없음" : "No recent speech"}</div>
          ) : null}
        </div>
      </div>

      <div className="llm-meta-row">
        <span>
          <BrainCircuit size={14} />
          {llmDecision.model_id || selectedActorModel || vm.ui.none}
        </span>
        <span>
          <Clock3 size={14} />
          {llmDecision.latency_sec ? `${llmDecision.latency_sec.toFixed(2)}s` : vm.ui.none}
        </span>
      </div>
      {payload.reasonCode ? <div className="llm-reason">{payload.reasonCode}</div> : null}
    </aside>
  );
}
