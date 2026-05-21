import type { CSSProperties } from "react";

import type { LayoutAnchor, LayoutEntity } from "../types";

export const STAGE_SCENE_WIDTH = 100;
export const STAGE_SCENE_HEIGHT = 78;

export type ScenePoint = {
  x: number;
  y: number;
};

export type SceneRect = ScenePoint & {
  width: number;
  height: number;
};

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function yToCssPercent(y: number): string {
  return `${(y / STAGE_SCENE_HEIGHT) * 100}%`;
}

export function projectAnchor(anchor: LayoutAnchor): ScenePoint {
  return {
    x: anchor.x,
    y: anchor.y,
  };
}

export function projectEntity(entity: LayoutEntity): SceneRect {
  const width = clamp(entity.width, 0, STAGE_SCENE_WIDTH);
  const height = clamp(entity.height, 0, STAGE_SCENE_HEIGHT);
  return {
    x: clamp(entity.x, 0, STAGE_SCENE_WIDTH - width),
    y: clamp(entity.y, 0, STAGE_SCENE_HEIGHT - height),
    width,
    height,
  };
}

export function scenePointStyle(point: ScenePoint): CSSProperties {
  return {
    left: `${point.x}%`,
    top: yToCssPercent(point.y),
  };
}

export function entityStyle(entity: LayoutEntity): CSSProperties {
  const rect = projectEntity(entity);
  return {
    left: `${rect.x}%`,
    top: yToCssPercent(rect.y),
    width: `${rect.width}%`,
    height: yToCssPercent(rect.height),
  };
}

export function fanOutAnchorPoint(anchor: LayoutAnchor, index = 0, compact = false): ScenePoint {
  const point = projectAnchor(anchor);
  if (compact) return point;

  const xOffset = index % 2 === 0 ? -4.6 : 4.6;
  const yOffset = Math.floor(index / 2) * 3.2;
  return {
    x: clamp(point.x + xOffset, 1.5, STAGE_SCENE_WIDTH - 1.5),
    y: clamp(point.y + yOffset, 1.5, STAGE_SCENE_HEIGHT - 1.5),
  };
}

export function anchorInsideEntity(anchor: LayoutAnchor, entity: LayoutEntity): CSSProperties {
  const rect = projectEntity(entity);
  const rawX = rect.width > 0 ? ((anchor.x - rect.x) / rect.width) * 100 : 50;
  const rawY = rect.height > 0 ? ((anchor.y - rect.y) / rect.height) * 100 : 50;
  return {
    left: `${clamp(rawX, 18, 82)}%`,
    top: `${clamp(rawY, 18, 82)}%`,
  };
}

export function routePath(source: LayoutAnchor, target: LayoutAnchor): string {
  const dx = target.x - source.x;
  const lift = Math.max(4.6, Math.min(10, Math.abs(dx) * 0.18));
  const c1x = source.x + dx * 0.32;
  const c2x = target.x - dx * 0.32;
  return `M ${source.x} ${source.y} C ${c1x} ${source.y - lift}, ${c2x} ${target.y - lift}, ${target.x} ${target.y}`;
}

export function routeRunnerPoint(source: LayoutAnchor, target: LayoutAnchor, progress: number): ScenePoint {
  const clamped = clamp(progress || 0.55, 0, 1);
  const x = source.x + (target.x - source.x) * clamped;
  const baseY = source.y + (target.y - source.y) * clamped;
  const arc = Math.sin(clamped * Math.PI) * 5.5;
  return {
    x: clamp(x, 1.5, STAGE_SCENE_WIDTH - 1.5),
    y: clamp(baseY - arc, 1.5, STAGE_SCENE_HEIGHT - 1.5),
  };
}
