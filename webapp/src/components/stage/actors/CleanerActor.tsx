import { motion } from "framer-motion";

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
      <motion.div
        className="stage-actor-motion"
        initial={{ opacity: 0, x: -12 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ duration: 0.34 }}
      >
        <div className="cleaner-halo" />
        <div className="cleaner-unit">
          <div className="cleaner-ring">
            <span>{busy ? countdown : ""}</span>
          </div>
          <div className="cleaner-label">{label}</div>
        </div>
      </motion.div>
    </div>
  );
}
