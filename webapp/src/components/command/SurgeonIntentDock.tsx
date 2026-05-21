import { Hand, Mic, Undo2, UserRound } from "lucide-react";

import type { OverridePayload } from "../../hooks/useRosBridge";
import type { useDigitalTwinViewModel } from "../../hooks/useDigitalTwinViewModel";
import { type Language } from "../../utils/display";

type ViewModel = ReturnType<typeof useDigitalTwinViewModel>;

export function SurgeonIntentDock({
  vm,
  language,
  overrideOptions,
  overrideTool,
  setOverrideTool,
  voiceText,
  setVoiceText,
  connected,
  actionPending,
  onOverride,
}: {
  vm: ViewModel;
  language: Language;
  overrideOptions: ViewModel["requestableTools"];
  overrideTool: string;
  setOverrideTool: (tool: string) => void;
  voiceText: string;
  setVoiceText: (text: string) => void;
  connected: boolean;
  actionPending: string;
  onOverride: (payload: OverridePayload) => void;
}) {
  const disabled = !connected || Boolean(actionPending) || !overrideTool;
  const toolLabel = vm.displayToolName(overrideTool);

  return (
    <aside className="dock surgeon-dock">
      <div className="dock-header">
        <div>
          <p className="section-kicker">{vm.ui.surgeon}</p>
          <h2>{vm.surgeon.intent}</h2>
        </div>
        <UserRound size={18} />
      </div>

      <div className="intent-state">
        <article>
          <span>{vm.ui.requestedTool}</span>
          <strong>{vm.surgeon.requestedTool}</strong>
        </article>
        <article>
          <span>{vm.ui.spoken}</span>
          <strong>{vm.surgeon.spoken}</strong>
        </article>
        <div className="ready-row">
          <span className={vm.surgeon.readyForHandover ? "ready" : ""}>{vm.ui.handover}: {vm.surgeon.readyForHandover ? vm.ui.yes : vm.ui.no}</span>
          <span className={vm.surgeon.readyForRetrieval ? "ready" : ""}>{vm.ui.retrieval}: {vm.surgeon.readyForRetrieval ? vm.ui.yes : vm.ui.no}</span>
        </div>
      </div>

      <label className="field">
        <span>{vm.ui.overrideTool}</span>
        <select value={overrideTool} onChange={(event) => setOverrideTool(event.target.value)}>
          {overrideOptions.map((tool) => (
            <option value={tool.id} key={tool.id}>
              {tool.label}
            </option>
          ))}
        </select>
      </label>

      <label className="field">
        <span>{vm.ui.voiceText}</span>
        <input value={voiceText} onChange={(event) => setVoiceText(event.target.value)} />
      </label>

      <div className="override-grid">
        <button
          className="button button-primary"
          disabled={disabled}
          onClick={() =>
            onOverride({
              eventType: "request_tool",
              requestedTool: overrideTool,
              voiceText,
              toolLabel,
            })
          }
          type="button"
        >
          <Hand size={16} />
          {vm.ui.requestTool}
        </button>
        <button
          className="button button-secondary"
          disabled={disabled}
          onClick={() =>
            onOverride({
              eventType: "voice_request",
              requestedTool: overrideTool,
              voiceText,
              toolLabel,
            })
          }
          type="button"
        >
          <Mic size={16} />
          {vm.ui.voiceOverride}
        </button>
        <button
          className="button button-quiet full"
          disabled={disabled}
          onClick={() =>
            onOverride({
              eventType: "return_tool",
              requestedTool: overrideTool,
              voiceText,
              toolLabel,
            })
          }
          type="button"
        >
          <Undo2 size={16} />
          {vm.ui.returnTool}
        </button>
      </div>
    </aside>
  );
}
