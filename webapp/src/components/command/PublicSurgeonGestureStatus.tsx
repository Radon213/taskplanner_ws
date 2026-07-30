import { Hand } from "lucide-react";

import { type Language } from "../../utils/display";

export type PublicSurgeonGesture = {
  eventType: string;
  handPose: string;
  confidence: number;
  requestedTool: string;
};

const REQUEST_EVENTS = new Set([
  "extend_hand_for_handover",
  "implicit_tool_request",
  "request_tool",
]);
const REQUEST_POSES = new Set([
  "hand_extending",
  "open_palm",
  "open_palm_receive",
  "open_receive",
  "palm_up",
]);

function normalize(value: string): string {
  return value.trim().toLocaleLowerCase().replace(/[\s-]+/g, "_");
}

export function isImplicitToolRequest(
  evidence: PublicSurgeonGesture,
): boolean {
  return (
    Number.isFinite(evidence.confidence) &&
    evidence.confidence >= 0.5 &&
    REQUEST_EVENTS.has(normalize(evidence.eventType)) &&
    REQUEST_POSES.has(normalize(evidence.handPose))
  );
}

export function PublicSurgeonGestureStatus({
  evidence,
  language,
  toolLabel,
  label,
}: {
  evidence: PublicSurgeonGesture;
  language: Language;
  toolLabel: string;
  label?: string;
}) {
  const active = isImplicitToolRequest(evidence);
  const confidence = active
    ? `${Math.round(evidence.confidence * 100)}%`
    : "";

  return (
    <div
      className={`public-gesture-status ${active ? "active" : "idle"}`}
      data-active={active ? "true" : "false"}
      data-event-type={normalize(evidence.eventType)}
      data-hand-pose={normalize(evidence.handPose)}
      role="status"
      aria-live="polite"
    >
      <Hand aria-hidden="true" size={16} strokeWidth={2.2} />
      <span>
        {label ??
          (language === "ko"
            ? "암묵적 도구 요청"
            : "Implicit tool request")}
      </span>
      <strong>
        {active
          ? language === "ko"
            ? "손 내미는 중"
            : "Hand extending"
          : language === "ko"
            ? "감지되지 않음"
            : "Not detected"}
      </strong>
      {active ? (
        <small>
          {[toolLabel, confidence].filter(Boolean).join(" · ")}
        </small>
      ) : null}
    </div>
  );
}
