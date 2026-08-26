import { useReducedMotion } from "framer-motion";
import * as m from "framer-motion/m";
import { CheckCircle2, CircleDashed, ShieldCheck, XCircle } from "lucide-react";

import type {
  VirtualDtSimulation,
  VirtualDtSimulationState,
} from "../../hooks/useVirtualEndpointDtSimulation";
import { MOTION_DURATION, SILK_EASE } from "../../motion-system";
import type { Language } from "../../utils/display";
import "./VirtualDtSimulationOverlay.css";

type VirtualDtSimulationOverlayProps = {
  simulation: VirtualDtSimulation | null;
  language: Language;
  displayToolName?: (toolId: string) => string;
  className?: string;
};

function displayLocation(value: string, fallback: string): string {
  const normalized = value.trim().replace(/[_-]+/g, " ").replace(/\s+/g, " ");
  return normalized || fallback;
}

function stateCopy(
  state: VirtualDtSimulationState,
  language: Language,
): string {
  const labels: Record<string, readonly [string, string]> = {
    dispatching: ["가상 요청 전송", "Virtual request sent"],
    accepted: ["가상 서버 수락", "Virtual server accepted"],
    executing: ["가상 경로 진행", "Virtual route in progress"],
    completed: ["가상 Action 완료", "Virtual Action completed"],
    rejected: ["가상 요청 거부", "Virtual request rejected"],
    failed: ["가상 요청 실패", "Virtual request failed"],
    canceled: ["가상 요청 취소", "Virtual request canceled"],
    unknown: ["가상 상태 미확인", "Virtual state unknown"],
  };
  return (labels[state] ?? labels.unknown)[language === "ko" ? 0 : 1];
}

function stateIcon(state: string) {
  if (state === "completed") return CheckCircle2;
  if (state === "rejected" || state === "failed" || state === "canceled" || state === "unknown") return XCircle;
  return CircleDashed;
}

/**
 * Small, observer-only route display for virtual Action/Service traffic.
 * It intentionally renders no physical robot pose and labels the source on
 * every state so it cannot be mistaken for external motion feedback.
 */
export function VirtualDtSimulationOverlay({
  simulation,
  language,
  displayToolName,
  className = "",
}: VirtualDtSimulationOverlayProps) {
  const reducedMotion = useReducedMotion();
  const command = simulation?.current;
  if (!command) return null;

  const StateIcon = stateIcon(command.state);
  const sourceLabel = displayLocation(
    command.route.sourceLocationId,
    language === "ko" ? "출발 위치" : "Source",
  );
  const targetLabel = displayLocation(
    command.route.targetLocationId,
    language === "ko" ? "도착 위치" : "Target",
  );
  const toolLabel = command.toolId
    ? displayToolName?.(command.toolId) || command.toolId
    : language === "ko"
      ? "도구 정보 없음"
      : "No tool metadata";
  const routeLabel = command.transport === "action"
    ? language === "ko" ? "가상 Action 시뮬레이션" : "Virtual Action simulation"
    : language === "ko" ? "가상 Service 접수 경로" : "Virtual Service admission path";
  const terminalProjectionReady = command.terminalProjection !== null;
  const safetyCopy = command.admissionOnly
    ? language === "ko"
      ? "Service 수락은 접수 결과이며 물리 실행·완료를 뜻하지 않습니다."
      : "Service acceptance is a receipt, not physical execution or completion."
    : terminalProjectionReady
      ? language === "ko"
        ? "가상 Action 종료 결과를 확인해 표시용 도구 위치만 갱신합니다."
        : "The terminal virtual Action result updates display-only tool placement."
      : language === "ko"
        ? "가상 경로 표현입니다. 실물 로봇 상태나 모션 증거가 아닙니다."
        : "This is a virtual route display, not physical robot state or motion evidence.";

  return (
    <m.section
      className={`virtual-dt-simulation-overlay ${command.state} ${className}`.trim()}
      data-slot="virtual-dt-simulation-overlay"
      data-command-id={command.commandId}
      data-endpoint-source="virtual"
      data-terminal-projection={terminalProjectionReady ? "ready" : "pending"}
      role="status"
      aria-atomic="true"
      aria-live="polite"
      initial={{ opacity: 0, y: reducedMotion ? 0 : 5 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: reducedMotion ? 0.01 : MOTION_DURATION.normal, ease: SILK_EASE }}
    >
      <div className="virtual-dt-simulation-heading">
        <span className="virtual-dt-simulation-kicker">
          <ShieldCheck aria-hidden="true" size={12} strokeWidth={2.2} />
          {routeLabel}
        </span>
        <span className="virtual-dt-simulation-state">
          <StateIcon aria-hidden="true" size={12} strokeWidth={2.2} />
          {stateCopy(command.state, language)}
        </span>
      </div>

      <div className="virtual-dt-simulation-route" aria-label={`${sourceLabel} to ${targetLabel}`}>
        <strong title={sourceLabel}>{sourceLabel}</strong>
        <div className="virtual-dt-simulation-progress" aria-hidden="true">
          <m.span
            animate={{ scaleX: command.progress }}
            initial={{ scaleX: 0 }}
            transition={{ duration: reducedMotion ? 0.01 : MOTION_DURATION.entrance, ease: SILK_EASE }}
          />
        </div>
        <strong title={targetLabel}>{targetLabel}</strong>
      </div>

      <div className="virtual-dt-simulation-detail">
        <span>{toolLabel}</span>
        <span>{Math.round(command.progress * 100)}%</span>
      </div>
      <p>{safetyCopy}</p>
    </m.section>
  );
}
