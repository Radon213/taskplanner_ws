import * as m from "framer-motion/m";

import { silk } from "../../../motion-system";
import type { LayoutEntity } from "../../../types";
import { entityStyle } from "../../../utils/stageGeometry";

export function RackActor({ entity, slotCount }: { entity: LayoutEntity; slotCount: number }) {
  return (
    <div
      className="stage-actor instrument-rack"
      style={entityStyle(entity)}
      data-layout-id={entity.id}
      data-layout-type={entity.type}
      aria-label="Instrument rack"
    >
      <m.div
        className="stage-actor-motion"
        initial={silk.entrance.initial}
        animate={silk.entrance.animate}
      >
        <div className="rack-shell">
          <div className="rack-title">{entity.label}</div>
          <div className="rack-slots">
            {Array.from({ length: Math.max(slotCount, 6) }).map((_, index) => (
              <span key={index} />
            ))}
          </div>
        </div>
      </m.div>
    </div>
  );
}
