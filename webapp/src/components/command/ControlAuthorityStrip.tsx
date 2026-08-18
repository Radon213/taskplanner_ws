import { LayoutGroup } from "framer-motion";
import * as m from "framer-motion/m";

import { silk } from "../../motion-system";
import type { BTDecision, SkillStatus, VLMHealth, WorldState } from "../../types";
import type { Language } from "../../utils/display";

type AuthorityTone = "ready" | "active" | "advisory" | "waiting" | "blocked" | "external";

type AuthorityStep = {
  id: string;
  label: string;
  status: string;
  detail: string;
  tone: AuthorityTone;
};

export function ControlAuthorityStrip({
  language,
  connected,
  procedure,
  phase,
  runtimeState,
  vlmHealth,
  worldState,
  btDecision,
  skillStatus,
}: {
  language: Language;
  connected: boolean;
  procedure: string;
  phase: string;
  runtimeState: string;
  vlmHealth: VLMHealth;
  worldState: WorldState;
  btDecision: BTDecision;
  skillStatus: SkillStatus;
}) {
  const korean = language === "ko";
  const procedureLabel = procedure.trim() || (korean ? "수술 정보 대기" : "Waiting for procedure");
  const phaseLabel = phase.trim() || (korean ? "단계 미확인" : "Phase unknown");
  const hasTwinState = Boolean(worldState.filtered_phase || worldState.procedure_id);
  const hasBtDecision = Boolean(
    btDecision.action || (btDecision.decision && btDecision.decision !== "idle"),
  );
  const hasActionTrace = Boolean(skillStatus.command_id);
  const currentStepIndex = hasActionTrace
    ? 4
    : hasBtDecision
      ? 3
      : hasTwinState
        ? 2
        : vlmHealth.connected
          ? 1
          : connected
            ? 0
            : -1;

  const steps: AuthorityStep[] = [
    {
      id: "observed-input",
      label: korean ? "1 · 관측 입력" : "1 · Observed input",
      status: connected
        ? korean ? "전송 연결됨" : "Transport connected"
        : korean ? "입력 확인 불가" : "Input unavailable",
      detail: korean ? "ROSBridge 전송 상태" : "ROSBridge transport state",
      tone: connected ? "ready" : "blocked",
    },
    {
      id: "vlm-advisory",
      label: korean ? "2 · VLM 조언" : "2 · VLM advisory",
      status: vlmHealth.connected && vlmHealth.healthy
        ? korean ? "조언 가능" : "Advisory available"
        : vlmHealth.connected
          ? korean ? "성능 저하" : "Degraded"
          : korean ? "조언 없음" : "No advisory",
      detail: vlmHealth.model_id || (korean ? "모델 상태 대기" : "Waiting for model state"),
      tone: vlmHealth.connected && vlmHealth.healthy
        ? "advisory"
        : vlmHealth.connected ? "waiting" : "blocked",
    },
    {
      id: "digital-twin",
      label: korean ? "3 · 디지털 트윈" : "3 · Digital twin",
      status: hasTwinState
        ? worldState.filtered_phase || (korean ? "상태 수신" : "State received")
        : korean ? "근거 대기" : "Waiting for evidence",
      detail: korean ? "관측 근거를 상태로 융합" : "Fuses observations into state",
      tone: hasTwinState ? "active" : "waiting",
    },
    {
      id: "bt-dispatch",
      label: korean ? "4 · BT 게이트" : "4 · BT gate",
      status: hasBtDecision
        ? btDecision.decision || btDecision.action
        : korean ? "디스패치 없음" : "No dispatch",
      detail: btDecision.blocking_guard || btDecision.decision_reason || (
        korean ? "안전 조건이 통과해야 명령 생성" : "Safety guards must pass before dispatch"
      ),
      tone: hasBtDecision ? (btDecision.handover_allowed ? "active" : "advisory") : "waiting",
    },
    {
      id: "action-contract",
      label: korean ? "5 · Action 계약" : "5 · Action contract",
      status: hasActionTrace
        ? skillStatus.state || (korean ? "요청 추적 중" : "Tracking request")
        : korean ? "수락 Goal 없음" : "No accepted goal observed",
      detail: hasActionTrace
        ? skillStatus.command_id
        : korean ? "Action 상태 수신 전" : "No Action state received",
      tone: hasActionTrace ? (skillStatus.success ? "ready" : "active") : "waiting",
    },
    {
      id: "external-controller",
      label: korean ? "6 · 물리 제어기" : "6 · Physical controller",
      status: korean ? "외부 권한" : "External authority",
      detail: korean
        ? "궤적·모터·안전 정지는 외부 제어기 소유"
        : "Trajectory, motor, and safety-stop authority stays external",
      tone: "external",
    },
  ];

  return (
    <section
      aria-labelledby="control-authority-title"
      className="authority-strip"
      data-slot="control-authority-strip"
    >
      <div className="authority-summary">
        <p>{korean ? "제어 권한 경계" : "Control authority boundary"}</p>
        <h2 id="control-authority-title">
          {procedureLabel} · {phaseLabel}
        </h2>
        <span>{runtimeState}</span>
      </div>
      <LayoutGroup id="control-authority-progress">
        <ol
          aria-label={korean ? "제어 권한 단계" : "Control authority stages"}
          className="authority-chain"
          tabIndex={0}
        >
          {steps.map((step, index) => {
            const current = index === currentStepIndex;
            return (
              <li
                aria-current={current ? "step" : undefined}
                className={`authority-step ${step.tone}`}
                key={step.id}
                title={step.detail}
              >
                {current ? (
                  <m.span
                    aria-hidden="true"
                    className="authority-focus"
                    layoutId="authority-current-step"
                    transition={silk.layout.transition}
                  />
                ) : null}
                <p>{step.label}</p>
                <strong>{step.status}</strong>
                <small>{step.detail}</small>
              </li>
            );
          })}
        </ol>
      </LayoutGroup>
    </section>
  );
}
