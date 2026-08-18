import * as m from "framer-motion/m";

import { silk } from "../../../motion-system";
import type { LayoutEntity } from "../../../types";
import { entityStyle } from "../../../utils/stageGeometry";

export function CleanerActor({
  entity,
  busy,
  countdown,
  label,
}: {
  entity: LayoutEntity;
  busy: boolean;
  countdown: number;
  label: string;
}) {
  return (
    <div
      className={`stage-actor cleaner${busy ? " busy" : ""}`}
      style={entityStyle(entity)}
      data-layout-id={entity.id}
      data-layout-type={entity.type}
      aria-label="Cleaner station"
    >
      <m.div
        className="stage-actor-motion"
        initial={silk.entrance.initial}
        animate={silk.entrance.animate}
      >
        <div className="cleaner-halo" />
        <div className="cleaner-unit">
          <div className="cleaner-ring">
            <span>{busy ? countdown : ""}</span>
          </div>
          <div className="cleaner-label">{label}</div>
        </div>
      </m.div>
    </div>
  );
}
