import { BrainCircuit, Clock3, MessageSquareText, Power, PowerOff, UserRound } from "lucide-react";

import type { useDigitalTwinViewModel } from "../../hooks/useDigitalTwinViewModel";
import type { SurgeonLLMDecision } from "../../types";
import { type Language } from "../../utils/display";

type ViewModel = ReturnType<typeof useDigitalTwinViewModel>;

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
        <label className="field compact">
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

      <div className="llm-speech-card">
        <span>
          <MessageSquareText size={15} />
          {vm.ui.spoken}
        </span>
        <strong>{llmDecision.speech || vm.surgeon.spoken}</strong>
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
