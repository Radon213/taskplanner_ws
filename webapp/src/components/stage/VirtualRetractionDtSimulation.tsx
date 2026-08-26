import { useReducedMotion } from "framer-motion";
import * as m from "framer-motion/m";
import { CircleDashed, ShieldCheck } from "lucide-react";

import type { VirtualDtSimulation } from "../../hooks/useVirtualEndpointDtSimulation";
import { MOTION_DURATION, SILK_EASE } from "../../motion-system";
import type { Language } from "../../utils/display";
import "./VirtualRetractionDtSimulation.css";

type VirtualRetractionDtSimulationProps = {
  simulation: VirtualDtSimulation | null;
  language: Language;
};

function displayLabel(value: string, fallback: string): string {
  const normalized = value.trim().replace(/[_-]+/g, " ").replace(/\s+/g, " ");
  return normalized || fallback;
}

/**
 * Browser-only retraction scene projection for the isolated virtual Service.
 * A Service V1 acceptance is deliberately shown as an admission-driven scene
 * animation, never as controller telemetry, physical motion, or completion.
 */
export function VirtualRetractionDtSimulation({
  simulation,
  language,
}: VirtualRetractionDtSimulationProps) {
  const reducedMotion = useReducedMotion();
  const command = simulation?.current;
  if (!command || command.transport !== "service") return null;

  const source = displayLabel(
    command.route.sourceLocationId,
    language === "ko" ? "베드 로봇" : "Bed robot",
  );
  const target = displayLabel(
    command.route.targetLocationId,
    language === "ko" ? "수술야" : "Surgical field",
  );
  const commandLabel = displayLabel(
    command.action || command.route.route,
    language === "ko" ? "리트랙션" : "Retraction",
  );
  const receiptLabel = language === "ko"
    ? "가상 Service 접수"
    : "Virtual Service received";
  const sceneLabel = language === "ko"
    ? "화면 DT 장면 전환"
    : "Display DT scene transition";
  const safetyCopy = language === "ko"
    ? "접수에 맞춘 화면 애니메이션입니다. 실제 로봇 실행·완료·위치 상태는 갱신하지 않습니다."
    : "This is a receipt-driven display animation. It does not update physical execution, completion, or pose state.";

  return (
    <m.article
      className="virtual-retraction-dt-simulation"
      data-slot="virtual-retraction-dt-simulation"
      data-command-id={command.commandId}
      data-endpoint-source="virtual"
      data-admission-only="true"
      role="status"
      aria-live="polite"
      aria-atomic="true"
      initial={{ opacity: 0, y: reducedMotion ? 0 : 5 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: reducedMotion ? 0.01 : MOTION_DURATION.normal, ease: SILK_EASE }}
    >
      <header className="virtual-retraction-dt-heading">
        <span className="virtual-retraction-dt-kicker">
          <ShieldCheck aria-hidden="true" size={14} strokeWidth={2.2} />
          {receiptLabel}
        </span>
        <span className="virtual-retraction-dt-state">
          <CircleDashed aria-hidden="true" size={13} strokeWidth={2.2} />
          {sceneLabel}
        </span>
      </header>

      <div className="virtual-retraction-dt-track" aria-hidden="true">
        <span className="virtual-retraction-dt-anchor virtual-retraction-dt-anchor-source" />
        <m.span
          key={command.commandId}
          className="virtual-retraction-dt-carriage"
          initial={{ left: 14, opacity: 0.5 }}
          animate={{ left: "calc(100% - 36px)", opacity: 1 }}
          transition={{
            duration: reducedMotion ? 0.01 : Math.max(MOTION_DURATION.entrance, 0.72),
            ease: SILK_EASE,
          }}
        />
        <span className="virtual-retraction-dt-anchor virtual-retraction-dt-anchor-target" />
      </div>

      <div className="virtual-retraction-dt-route">
        <strong title={commandLabel}>{commandLabel}</strong>
        <span title={`${source} → ${target}`}>{source} → {target}</span>
      </div>
      <p>{safetyCopy}</p>
    </m.article>
  );
}
