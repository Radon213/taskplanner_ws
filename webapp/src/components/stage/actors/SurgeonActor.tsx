import { AnimatePresence, motion } from "framer-motion";

import type { LayoutEntity } from "../../../types";
import { entityStyle } from "../../../utils/stageGeometry";

export function SurgeonActor({
  entity,
  active,
  intentBubble,
  label,
}: {
  entity: LayoutEntity;
  active: boolean;
  intentBubble: string;
  label: string;
}) {
  return (
    <div
      className={`stage-actor surgeon${active ? " active" : ""}`}
      style={entityStyle(entity)}
      data-layout-id={entity.id}
      data-layout-type={entity.type}
      aria-label="Lead surgeon"
    >
      <motion.div
        className="stage-actor-motion"
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.42, ease: [0.22, 1, 0.36, 1] }}
      >
        <AnimatePresence>
          {intentBubble ? (
            <motion.div
              className="intent-bubble"
              initial={{ opacity: 0, y: 8, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6, scale: 0.96 }}
              transition={{ duration: 0.26 }}
            >
              {intentBubble}
            </motion.div>
          ) : null}
        </AnimatePresence>
        <div className="actor-shadow warm" />
        <div className="surgeon-figure">
          <div className="surgeon-cap" />
          <div className="surgeon-head">
            <span />
          </div>
          <div className="surgeon-body" />
          <div className="surgeon-arm left" />
          <div className="surgeon-arm right" />
        </div>
        <div className="actor-label">{label}</div>
      </motion.div>
    </div>
  );
}
