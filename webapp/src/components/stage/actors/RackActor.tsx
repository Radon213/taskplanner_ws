import { motion } from "framer-motion";

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
      <motion.div
        className="stage-actor-motion"
        initial={{ opacity: 0, x: -14 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ duration: 0.36 }}
      >
        <div className="rack-shell">
          <div className="rack-title">{entity.label}</div>
          <div className="rack-slots">
            {Array.from({ length: Math.max(slotCount, 6) }).map((_, index) => (
              <span key={index} />
            ))}
          </div>
        </div>
      </motion.div>
    </div>
  );
}
