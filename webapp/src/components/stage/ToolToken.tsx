import { useReducedMotion } from "framer-motion";
import * as m from "framer-motion/m";

import type { StageTool } from "../../hooks/useDigitalTwinViewModel";
import { MOTION_DURATION, SILK_EASE } from "../../motion-system";
import { scenePointStyle } from "../../utils/stageGeometry";

export function ToolToken({ tool }: { tool: StageTool }) {
  const reduceMotion = useReducedMotion();
  return (
    <div
      className={`tool-token-anchor${tool.compact ? " compact" : ""}`}
      style={scenePointStyle(tool.point)}
      data-anchor-id={tool.anchorId}
      data-tool-id={tool.id}
      title={`${tool.label} · ${tool.lifecycle}`}
    >
      <m.div
        className={`tool-token ${tool.tone}${tool.active ? " active" : ""}${tool.contaminated ? " contaminated" : ""}${
          tool.compact ? " compact" : ""
        }`}
        initial={{ opacity: 0, scale: 0.94 }}
        animate={{ opacity: 1, scale: tool.active ? 1.06 : 1 }}
        exit={{ opacity: 0, scale: 0.96 }}
        transition={
          reduceMotion
            ? { duration: MOTION_DURATION.instant }
            : {
                duration: MOTION_DURATION.moderate,
                ease: SILK_EASE,
              }
        }
      >
        <span className="tool-token-core">{tool.shortLabel}</span>
        {!tool.compact && <span className="tool-token-label">{tool.lifecycle}</span>}
      </m.div>
    </div>
  );
}
