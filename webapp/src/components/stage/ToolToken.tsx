import { motion, useReducedMotion } from "framer-motion";

import type { StageTool } from "../../hooks/useDigitalTwinViewModel";
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
      <motion.div
        className={`tool-token ${tool.tone}${tool.active ? " active" : ""}${tool.contaminated ? " contaminated" : ""}${
          tool.compact ? " compact" : ""
        }`}
        initial={{ opacity: 0, scale: 0.84 }}
        animate={{ opacity: 1, scale: tool.active ? 1.16 : 1 }}
        exit={{ opacity: 0, scale: 0.82 }}
        transition={
          reduceMotion
            ? { duration: 0.12 }
            : {
                type: "spring",
                stiffness: 260,
                damping: 28,
                mass: 0.8,
              }
        }
      >
        <span className="tool-token-core">{tool.shortLabel}</span>
        {!tool.compact && <span className="tool-token-label">{tool.lifecycle}</span>}
      </motion.div>
    </div>
  );
}
