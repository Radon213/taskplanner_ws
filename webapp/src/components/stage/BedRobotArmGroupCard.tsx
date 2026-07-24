import { Activity, Move } from "lucide-react";

import type { StageBedRobotArmGroup } from "../../hooks/useDigitalTwinViewModel";

export function BedRobotArmGroupCard({ group }: { group: StageBedRobotArmGroup }) {
  const GroupIcon = group.groupId === "suction" ? Activity : Move;
  const statusSummary = `${group.connectionLabel}, ${group.stateLabel}`;

  return (
    <article
      className={`bed-robot-group-card tone-${group.statusTone}`}
      data-bed-robot-group-id={group.groupId}
      aria-label={`${group.title}: ${statusSummary}`}
    >
      <header className="bed-robot-group-header">
        <div className="bed-robot-group-identity">
          <span className="bed-robot-group-icon" aria-hidden="true">
            <GroupIcon size={18} strokeWidth={2.2} />
          </span>
          <div>
            <small>{group.eyebrow}</small>
            <h3>{group.title}</h3>
          </div>
        </div>
        <div
          className={`bed-robot-group-status tone-${group.statusTone}`}
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          <i aria-hidden="true" />
          <span>{statusSummary}</span>
        </div>
      </header>

      <dl className={`bed-robot-group-facts ${group.groupId}`}>
        <div>
          <dt>{group.labels.operation}</dt>
          <dd>{group.operationLabel}</dd>
        </div>
        {group.groupId === "retraction" ? (
          <>
            <div>
              <dt>{group.labels.direction}</dt>
              <dd>{group.directionLabel}</dd>
            </div>
            <div>
              <dt>{group.labels.distance}</dt>
              <dd className="bed-robot-distance">
                <strong>{group.distanceMm}</strong>
                <span>mm</span>
              </dd>
            </div>
          </>
        ) : null}
        <div>
          <dt>{group.labels.endEffector}</dt>
          <dd title={group.endEffectorLabel}>{group.endEffectorLabel}</dd>
        </div>
      </dl>

      <div className="bed-robot-group-progress">
        <div>
          <span>{group.labels.progress}</span>
          <strong>{group.progressLabel}</strong>
        </div>
        <progress value={group.progress} max={1} aria-label={`${group.title} ${group.labels.progress} ${group.progressLabel}`} />
      </div>

      {group.errorMessage ? (
        <p className="bed-robot-group-error" role="status" aria-live="polite">
          {group.errorMessage}
        </p>
      ) : null}
    </article>
  );
}
