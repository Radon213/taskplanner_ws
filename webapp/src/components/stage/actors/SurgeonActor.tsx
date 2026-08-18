import { AnimatePresence } from "framer-motion";
import * as m from "framer-motion/m";

import { silk, statusSwap } from "../../../motion-system";
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
      <m.div
        className="stage-actor-motion"
        initial={silk.entrance.initial}
        animate={silk.entrance.animate}
      >
        <AnimatePresence>
          {intentBubble ? (
            <m.div
              className="intent-bubble"
              {...statusSwap}
            >
              {intentBubble}
            </m.div>
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
      </m.div>
    </div>
  );
}
