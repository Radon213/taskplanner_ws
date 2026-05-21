import { motion } from "framer-motion";

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
      <motion.div
        className="stage-actor-motion"
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.34 }}
      >
        <div className="mayo-table">
          <div className="mayo-zone recovery">Recovery</div>
          <div className="mayo-zone reuse">Reuse</div>
        </div>
        <div className="mayo-leg left" />
        <div className="mayo-leg right" />
      </motion.div>
    </div>
  );
}
