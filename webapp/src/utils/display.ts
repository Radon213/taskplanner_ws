export type Language = "ko" | "en";

export function parseEventDetail(detail: string): Record<string, unknown> {
  try {
    return detail ? (JSON.parse(detail) as Record<string, unknown>) : {};
  } catch {
    return {};
  }
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
