import { motion } from "framer-motion";

import type { LayoutEntity } from "../../../types";
import { entityStyle } from "../../../utils/stageGeometry";

export function HumanoidActor({ entity, active, label }: { entity: LayoutEntity; active: boolean; label: string }) {
  return (
    <div
      className={`stage-actor humanoid${active ? " active" : ""}`}
      style={entityStyle(entity)}
      data-layout-id={entity.id}
      data-layout-type={entity.type}
      aria-label="Humanoid assistant"
    >
      <motion.div
        className="stage-actor-motion"
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.42, ease: [0.22, 1, 0.36, 1] }}
      >
        <div className="actor-shadow" />
        <div className="robot-figure">
          <div className="robot-head">
            <span />
          </div>
          <div className="robot-shoulder-line" />
          <div className="robot-torso">
            <i />
          </div>
          <div className="robot-arm left" />
          <div className="robot-arm right" />
          <div className="robot-leg left" />
          <div className="robot-leg right" />
        </div>
        <div className="actor-label">{label}</div>
      </motion.div>
    </div>
  );
}
