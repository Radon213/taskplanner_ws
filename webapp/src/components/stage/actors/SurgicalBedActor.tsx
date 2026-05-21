import { motion } from "framer-motion";

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
      <motion.div
        className="stage-actor-motion"
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: 0.4 }}
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
      </motion.div>
    </div>
  );
}
