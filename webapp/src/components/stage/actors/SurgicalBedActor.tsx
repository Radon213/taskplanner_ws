import * as m from "framer-motion/m";

import { silk } from "../../../motion-system";
import type { LayoutAnchor, LayoutEntity } from "../../../types";
import { anchorInsideEntity, entityStyle } from "../../../utils/stageGeometry";

export function SurgicalBedActor({
  entity,
  fieldAnchor,
  fieldLabel,
  active,
}: {
  entity: LayoutEntity;
  fieldAnchor?: LayoutAnchor;
  fieldLabel: string;
  active: boolean;
}) {
  return (
    <div
      className={`stage-actor surgical-bed${active ? " active" : ""}`}
      style={entityStyle(entity)}
      data-layout-id={entity.id}
      data-layout-type={entity.type}
      aria-label="Surgical bed"
    >
      <m.div
        className="stage-actor-motion"
        initial={silk.entrance.initial}
        animate={silk.entrance.animate}
      >
        <div className="bed-platform">
          <div className="bed-mattress">
            <div className="bed-headrest" />
            <div className="bed-field" style={fieldAnchor ? anchorInsideEntity(fieldAnchor, entity) : undefined}>
              <span>{fieldLabel}</span>
            </div>
          </div>
        </div>
        <div className="bed-base" />
      </m.div>
    </div>
  );
}
