import { Move } from "lucide-react";

import type { StageBedRobotArm } from "../../hooks/useDigitalTwinViewModel";

export function BedRobotArmCard({ arm }: { arm: StageBedRobotArm }) {
  const statusSummary = arm.stateLabel;

  return (
    <article
      className={`bed-robot-arm-card tone-${arm.statusTone}`}
      data-slot="bed-robot-arm-card"
      data-bed-robot-arm-id={arm.armId}
      aria-label={`${arm.title} ${arm.armId}: ${statusSummary}`}
    >
      <header className="bed-robot-arm-header">
        <div className="bed-robot-arm-identity">
          <span className="bed-robot-arm-icon" aria-hidden="true">
            <Move size={18} strokeWidth={2.2} />
          </span>
          <div>
            <small>{arm.eyebrow}</small>
            <h3>{arm.title}</h3>
          </div>
        </div>
        <div
          className={`bed-robot-arm-status tone-${arm.statusTone}`}
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          <i aria-hidden="true" />
          <span>{statusSummary}</span>
        </div>
      </header>

      <dl className="bed-robot-arm-facts">
        <div>
          <dt>{arm.labels.armId}</dt>
          <dd>{arm.armId}</dd>
        </div>
        <div>
          <dt>{arm.labels.roleInstance}</dt>
          <dd title={arm.roleInstanceLabel}>{arm.roleInstanceLabel}</dd>
        </div>
        <div>
          <dt>{arm.labels.directTeach}</dt>
          <dd>{arm.directTeachLabel}</dd>
        </div>
        <div>
          <dt>{arm.labels.reasonCode}</dt>
          <dd title={arm.reasonCodeLabel}>{arm.reasonCodeLabel}</dd>
        </div>
      </dl>
    </article>
  );
}
