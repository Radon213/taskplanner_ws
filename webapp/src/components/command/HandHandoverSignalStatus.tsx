import { type ComponentPropsWithoutRef } from "react";
import { Hand } from "lucide-react";

import { type Language } from "../../utils/display";
import "./hand-handover-signal.css";

export type HandHandoverSignal = {
  active: boolean;
  handPose: string;
  confidence: number;
  stabilitySec: number;
  generation: number;
};

type HandHandoverSignalStatusProps = Omit<
  ComponentPropsWithoutRef<"div">,
  "children"
> & {
  signal: HandHandoverSignal;
  language: Language;
  label?: string;
};

type HandHandoverSignalPopupProps = Omit<
  ComponentPropsWithoutRef<"section">,
  "children"
> & {
  signal: HandHandoverSignal;
  language: Language;
  /** Observation may be visible while scenario execution remains inactive. */
  observationOnly?: boolean;
};

function normalizeToken(value: string): string {
  return value.trim().toLocaleLowerCase().replace(/[\s-]+/g, "_");
}

function normalizedConfidence(value: number): number {
  return Number.isFinite(value) && value >= 0 && value <= 1 ? value : 0;
}

function normalizedStability(value: number): number {
  return Number.isFinite(value) && value >= 0 ? value : 0;
}

function normalizedGeneration(value: number): number {
  return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

function signalDetails(
  signal: HandHandoverSignal,
  language: Language,
): string {
  if (!signal.active) return "";
  const confidence = normalizedConfidence(signal.confidence);
  const stabilitySec = normalizedStability(signal.stabilitySec);
  return [
    language === "ko"
      ? `${stabilitySec.toFixed(1)}초 유지`
      : `held ${stabilitySec.toFixed(1)}s`,
    confidence > 0 ? `${Math.round(confidence * 100)}%` : "",
  ].filter(Boolean).join(" · ");
}

export function HandHandoverSignalStatus({
  signal,
  language,
  label,
  className = "",
  ...props
}: HandHandoverSignalStatusProps) {
  return (
    <div
      {...props}
      className={`hand-handover-signal-status ${signal.active ? "active" : "idle"} ${className}`.trim()}
      data-slot="hand-handover-signal-status"
      data-active={signal.active ? "true" : "false"}
      data-source="reducer-world-state"
      data-hand-pose={normalizeToken(signal.handPose)}
      data-generation={normalizedGeneration(signal.generation)}
      role="status"
      aria-live="polite"
    >
      <Hand aria-hidden="true" size={16} strokeWidth={2.2} />
      <span>
        {label ?? (language === "ko" ? "손 전달 신호" : "Hand handover signal")}
      </span>
      <strong>
        {signal.active
          ? language === "ko"
            ? "오른손 펼침 · 손바닥 위"
            : "Right hand open · palm up"
          : language === "ko"
            ? "신호 대기"
            : "Waiting for signal"}
      </strong>
      {signal.active ? (
        <small>{signalDetails(signal, language)}</small>
      ) : null}
    </div>
  );
}

export function HandHandoverSignalPopup({
  signal,
  language,
  observationOnly = false,
  className = "",
  ...props
}: HandHandoverSignalPopupProps) {
  if (!signal.active) return null;
  const details = signalDetails(signal, language);
  return (
    <section
      {...props}
      className={`operation-hand-handover-popup ${className}`.trim()}
      data-slot="hand-handover-signal-popup"
      data-source="reducer-world-state"
      data-hand-pose={normalizeToken(signal.handPose)}
      data-confidence={normalizedConfidence(signal.confidence).toFixed(3)}
      data-stability-sec={normalizedStability(signal.stabilitySec).toFixed(3)}
      data-generation={normalizedGeneration(signal.generation)}
      role="status"
      aria-live="polite"
    >
      <Hand aria-hidden="true" size={20} strokeWidth={2.2} />
      <div>
        <span>
          {language === "ko"
            ? observationOnly
              ? "암묵 전달 요청 · 관찰"
              : "암묵 전달 요청 확인"
            : observationOnly
              ? "Implicit handover request · observing"
              : "Implicit handover request confirmed"}
        </span>
        <strong>
          {language === "ko"
            ? "오른손 펼침 · 손바닥 위"
            : "Right hand open · palm up"}
        </strong>
        <small>
          {[
            details,
            observationOnly
              ? language === "ko"
                ? "관찰 전용 · 시나리오 대기"
                : "Observation only · scenario inactive"
              : language === "ko"
                ? "리듀서 게이트 통과"
                : "Reducer gate passed",
          ]
            .filter(Boolean)
            .join(" · ")}
        </small>
      </div>
    </section>
  );
}
