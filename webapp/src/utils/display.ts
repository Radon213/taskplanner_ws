export type Language = "ko" | "en";

const MAX_EVENT_DETAIL_JSON_CHARS = 64 * 1024;
const MAX_BOUNDED_JSON_COLLECTION_ITEMS = 256;
const MAX_BOUNDED_JSON_OBJECT_KEYS = 512;
const MAX_BOUNDED_JSON_STRING_CHARS = 64 * 1024;
const MAX_BOUNDED_JSON_DEPTH = 8;

function isBoundedJsonValue(value: unknown, depth = 0): boolean {
  if (depth > MAX_BOUNDED_JSON_DEPTH) return false;
  if (typeof value === "string") return value.length <= MAX_BOUNDED_JSON_STRING_CHARS;
  if (Array.isArray(value)) {
    return value.length <= MAX_BOUNDED_JSON_COLLECTION_ITEMS
      && value.every((item) => isBoundedJsonValue(item, depth + 1));
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    return entries.length <= MAX_BOUNDED_JSON_OBJECT_KEYS
      && entries.every(([key, item]) => key.length <= MAX_BOUNDED_JSON_STRING_CHARS
        && isBoundedJsonValue(item, depth + 1));
  }
  return true;
}

export function parseBoundedJson(raw: string, maxChars = MAX_EVENT_DETAIL_JSON_CHARS): unknown | null {
  if (!raw || raw.length > maxChars) return null;
  try {
    const parsed = JSON.parse(raw) as unknown;
    return isBoundedJsonValue(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function parseEventDetail(detail: string): Record<string, unknown> {
  const parsed = parseBoundedJson(detail);
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? parsed as Record<string, unknown>
    : {};
}

export function titleize(value: string): string {
  if (!value) {
    return "";
  }
  return value
    .replace(/_/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .split(" ")
    .filter(Boolean)
    .map((token) => token.charAt(0).toUpperCase() + token.slice(1))
    .join(" ");
}

export function displayToolName(instrumentId: string, language: Language): string {
  if (!instrumentId) {
    return language === "ko" ? "없음" : "None";
  }
  return language === "ko" ? instrumentId.replace(/_/g, " ") : titleize(instrumentId);
}

export function displayPhaseName(phaseId: string, language: Language): string {
  if (!phaseId) {
    return language === "ko" ? "알 수 없음" : "Unknown";
  }
  return language === "ko" ? phaseId.replace(/_/g, " ") : titleize(phaseId);
}

export function elapsedLabel(timestamp: number | null, language: Language): string {
  if (!timestamp) {
    return language === "ko" ? "없음" : "none";
  }
  const elapsedSec = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
  if (elapsedSec < 2) {
    return language === "ko" ? "방금" : "just now";
  }
  return `${elapsedSec}s ago`;
}
