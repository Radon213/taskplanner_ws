import { useReducedMotion } from "framer-motion";
import * as m from "framer-motion/m";
import {
  CheckCircle2,
  CircleAlert,
  CircleX,
  MessageSquareText,
  RadioTower,
  Send,
} from "lucide-react";

import {
  operationDispatchFeedCopy,
  type OperationAsrFinalPresentation,
  type OperationDispatchPresentation,
  type OperationPresentationFact,
  type OperationRetractionPresentation,
  type OperationToolHandoverPresentation,
  type OperationToolFlowPresentation,
} from "../../presentation/operationPresentation";
import type { Language } from "../../utils/display";
import { MOTION_DURATION, SILK_EASE } from "../../motion-system";
import "./operation-vlm-observability.css";

export type {
  ToolVlmEvidence,
} from "../../presentation/toolCardPresentation";

const MAX_DISPLAY_DISPATCHES = 8;

/**
 * Observer-only notice for an ASR utterance that has reached the final
 * transcript boundary.  It deliberately carries no intent or dispatch claim:
 * a finalized sentence can still be rejected, ambiguous, or unrelated to a
 * control request.
 */
export function SurgeonFinalSentencePopup({
  sentence,
  className = "",
}: {
  sentence: OperationAsrFinalPresentation | null;
  className?: string;
}) {
  if (!sentence) return null;
  return (
    <section
      className={`operation-surgeon-final-popup ${className}`.trim()}
      data-slot="surgeon-asr-final-popup"
      data-asr-final-key={sentence.eventKey}
      role="status"
      aria-live="polite"
      aria-label={sentence.ariaLabel}
    >
      <MessageSquareText aria-hidden="true" size={18} strokeWidth={2.1} />
      <div>
        <span>{sentence.heading}</span>
        <strong title={sentence.text}>{sentence.text}</strong>
      </div>
    </section>
  );
}

function dispatchIcon(state: OperationDispatchPresentation["state"]) {
  if (state === "completed" || state === "accepted") return CheckCircle2;
  if (state === "rejected" || state === "failed") return CircleX;
  if (state === "unknown") return CircleAlert;
  return state === "sent" ? Send : RadioTower;
}

function DispatchFacts({
  facts,
  className,
}: {
  facts: readonly OperationPresentationFact[];
  className: string;
}) {
  return (
    <dl className={className}>
      {facts.map((fact) => (
        <div key={`${fact.label}:${fact.value}`}>
          <dt>{fact.label}</dt>
          <dd className={fact.valueStyle === "mono" ? "operation-dispatch-mono" : undefined}>
            {fact.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function RetractionPayload({ payload }: { payload: OperationRetractionPresentation }) {
  return (
    <DispatchFacts
      className="operation-dispatch-retraction-payload"
      facts={payload.facts}
    />
  );
}

function DispatchFlow({ flow }: { flow: OperationToolFlowPresentation }) {
  return (
    <div className="operation-dispatch-flow" data-slot="operation-dispatch-tool-flow">
      <div>
        <span>{flow.from.label}</span>
        <strong>{flow.from.value}</strong>
      </div>
      <span className="operation-dispatch-flow-arrow" aria-hidden="true">→</span>
      <div>
        <span>{flow.to.label}</span>
        <strong>{flow.to.value}</strong>
      </div>
    </div>
  );
}

type HandoverPipelineStepState = "pending" | "active" | "complete" | "failed";

function handoverPipelineStepStates(
  state: OperationDispatchPresentation["state"],
): readonly HandoverPipelineStepState[] {
  if (state === "completed") return ["complete", "complete", "complete"];
  if (state === "accepted") return ["complete", "active", "pending"];
  if (state === "rejected" || state === "failed") return ["failed", "pending", "pending"];
  return ["active", "pending", "pending"];
}

function handoverPipelineProgress(
  state: OperationDispatchPresentation["state"],
): number {
  if (state === "completed") return 83.333;
  if (state === "accepted") return 50;
  return 16.667;
}

function DispatchHandoverPipeline({
  handover,
  flow,
  state,
  stateLabel,
}: {
  handover: OperationToolHandoverPresentation;
  flow: OperationToolFlowPresentation;
  state: OperationDispatchPresentation["state"];
  stateLabel: string;
}) {
  const reduceMotion = useReducedMotion();
  const stepStates = handoverPipelineStepStates(state);
  const steps = [flow.from, handover.carrier, flow.to] as const;
  return (
    <section
      className={`operation-handover-pipeline state-${state}`}
      data-slot="operation-tool-handover-pipeline"
      data-handover-state={state}
      aria-label={`${handover.pipelineLabel}: ${stateLabel}`}
    >
      <header>
        <span>{handover.pipelineLabel}</span>
        <em>{stateLabel}</em>
      </header>
      <div className="operation-handover-pipeline-track">
        <span className="operation-handover-pipeline-line" aria-hidden="true" />
        <m.span
          aria-hidden="true"
          className="operation-handover-pipeline-runner"
          initial={false}
          animate={{ left: `${handoverPipelineProgress(state)}%` }}
          transition={{
            duration: reduceMotion ? 0 : MOTION_DURATION.moderate,
            ease: SILK_EASE,
          }}
        />
        <ol>
          {steps.map((step, index) => (
            <li className={stepStates[index]} key={`${step.label}:${step.value}`}>
              <span aria-hidden="true">{index + 1}</span>
              <div>
                <small>{step.label}</small>
                <strong title={step.value}>{step.value}</strong>
              </div>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}

function DispatchMetadata({ facts }: { facts: readonly OperationPresentationFact[] }) {
  return <DispatchFacts className="operation-dispatch-metadata" facts={facts} />;
}

function DispatchRow({
  event,
}: {
  event: OperationDispatchPresentation;
}) {
  const Icon = dispatchIcon(event.state);
  return (
    <li
      className={`operation-dispatch-row state-${event.state}`}
      data-slot="operation-execution-dispatch-row"
      data-dispatch-id={event.id}
      data-dispatch-kind={event.kind}
      data-dispatch-state={event.state}
      data-command-id={event.commandId ?? ""}
      data-tool-instance-id={event.toolInstanceId ?? ""}
      data-retraction-command={event.retraction?.command ?? ""}
      data-retraction-target-side={event.retraction?.targetSide ?? ""}
      data-retraction-distance-cm={event.retraction?.distanceCm ?? ""}
    >
      <Icon aria-hidden="true" size={16} strokeWidth={2.2} />
      <div>
        <span>{event.kindLabel}</span>
        {event.toolHandover ? (
          <strong className="operation-handover-title">{event.toolHandover.title}</strong>
        ) : null}
        {event.subject ? <strong className="operation-dispatch-subject">{event.subject}</strong> : null}
        {event.retraction
          ? <RetractionPayload payload={event.retraction} />
          : event.toolHandover && event.toolFlow
            ? (
              <DispatchHandoverPipeline
                handover={event.toolHandover}
                flow={event.toolFlow}
                state={event.state}
                stateLabel={event.stateLabel}
              />
            )
            : event.toolFlow
            ? <DispatchFlow flow={event.toolFlow} />
            : null}
        <small className="operation-dispatch-endpoint">{event.endpoint}</small>
        {event.detail ? <small>{event.detail}</small> : null}
        <DispatchMetadata facts={event.metadata} />
      </div>
      <em>{event.stateLabel}</em>
    </li>
  );
}

/**
 * Observer-only runtime feed. It never creates a dispatch and labels a VLM
 * proposal as unsent until a separate bridge transport event says otherwise.
 */
export function OperationExecutionDispatchFeed({
  events,
  language,
  maxEntries = MAX_DISPLAY_DISPATCHES,
  className = "",
}: {
  events: readonly OperationDispatchPresentation[];
  language: Language;
  maxEntries?: number;
  className?: string;
}) {
  const copy = operationDispatchFeedCopy(language);
  const visibleEvents = [...events]
    .sort((left, right) => right.occurredAt - left.occurredAt)
    .slice(0, Math.max(1, Math.min(MAX_DISPLAY_DISPATCHES, maxEntries)));
  return (
    <section
      className={`operation-dispatch-feed ${className}`.trim()}
      data-slot="operation-execution-dispatch-feed"
      aria-label={copy.ariaLabel}
    >
      <div className="operation-dispatch-header">
        <div>
          <span>{copy.heading}</span>
          <strong>{copy.title}</strong>
        </div>
        <RadioTower aria-hidden="true" size={18} strokeWidth={2.1} />
      </div>
      {visibleEvents.length ? (
        <ol aria-live="polite">
          {visibleEvents.map((event) => <DispatchRow event={event} key={event.id} />)}
        </ol>
      ) : (
        <p className="operation-dispatch-empty">
          {copy.empty}
        </p>
      )}
    </section>
  );
}
