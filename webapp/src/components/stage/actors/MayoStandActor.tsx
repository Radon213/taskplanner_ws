import * as m from "framer-motion/m";

import { silk } from "../../../motion-system";
import type { LayoutEntity } from "../../../types";
import { entityStyle } from "../../../utils/stageGeometry";

export function MayoStandActor({ entity }: { entity: LayoutEntity }) {
  return (
    <div
      className="stage-actor mayo-stand"
      style={entityStyle(entity)}
      data-layout-id={entity.id}
      data-layout-type={entity.type}
      aria-label="Mayo stand"
    >
      <m.div
        className="stage-actor-motion"
        initial={silk.entrance.initial}
        animate={silk.entrance.animate}
      >
        <div className="mayo-table">
          <div className="mayo-zone recovery">Recovery</div>
          <div className="mayo-zone reuse">Reuse</div>
        </div>
        <div className="mayo-leg left" />
        <div className="mayo-leg right" />
      </m.div>
    </div>
  );
}
