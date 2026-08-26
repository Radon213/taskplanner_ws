export const MAX_ROS_JSON_PAYLOAD_CHARS = 256 * 1024;

export const MAX_ROS_PAYLOAD_COLLECTION_ITEMS = 256;
const MAX_ROS_PAYLOAD_OBJECT_KEYS = 512;
export const MAX_ROS_PAYLOAD_STRING_CHARS = 64 * 1024;
const MAX_ROS_PAYLOAD_DEPTH = 8;

export function finiteNumber(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function optionalFiniteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Bound untrusted rosbridge JSON before any contract-specific normalization.
 * This is deliberately structural only; individual message modules remain
 * responsible for schema, field, freshness, and cross-field validation.
 */
export function isBoundedRosPayload(value: unknown, depth = 0): boolean {
  if (depth > MAX_ROS_PAYLOAD_DEPTH) return false;
  if (typeof value === "string") {
    return value.length <= MAX_ROS_PAYLOAD_STRING_CHARS;
  }
  if (Array.isArray(value)) {
    return value.length <= MAX_ROS_PAYLOAD_COLLECTION_ITEMS
      && value.every((item) => isBoundedRosPayload(item, depth + 1));
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    return entries.length <= MAX_ROS_PAYLOAD_OBJECT_KEYS
      && entries.every(([key, item]) =>
        key.length <= MAX_ROS_PAYLOAD_STRING_CHARS
        && isBoundedRosPayload(item, depth + 1));
  }
  return true;
}
