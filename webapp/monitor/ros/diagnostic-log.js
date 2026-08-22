const DEFAULT_CAPACITY = 2000;
const LEVELS = new Set(["debug", "info", "warn", "error"]);
const PRIVATE_KEYS = /^(?:authorization|body|content|cookie|data|image|message|password|payload|raw|text|token|transcript)$/i;

function clone(value) {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

function safeUrl(value) {
  try {
    const url = new URL(String(value));
    url.username = "";
    url.password = "";
    url.search = "";
    url.hash = "";
    return url.toString();
  } catch {
    return String(value || "").slice(0, 256);
  }
}

function sanitize(value, key = "", depth = 0) {
  if (PRIVATE_KEYS.test(key)) return "[redacted]";
  if (value === null || value === undefined) return value ?? null;
  if (typeof value === "string") {
    return /url|address|endpoint/i.test(key) ? safeUrl(value) : value.slice(0, 256);
  }
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "boolean") return value;
  if (depth >= 4) return "[truncated]";
  if (Array.isArray(value)) return value.slice(0, 32).map((item) => sanitize(item, "", depth + 1));
  if (typeof value !== "object") return String(value).slice(0, 256);

  const output = {};
  Object.entries(value).slice(0, 64).forEach(([entryKey, entryValue]) => {
    output[entryKey] = sanitize(entryValue, entryKey, depth + 1);
  });
  return output;
}

function normalizedLevel(level) {
  const candidate = String(level || "info").toLowerCase();
  return LEVELS.has(candidate) ? candidate : "info";
}

function normalizedEvent(event) {
  const candidate = String(event || "diagnostic").trim();
  return candidate ? candidate.slice(0, 96) : "diagnostic";
}

export function createDiagnosticLog({
  capacity = DEFAULT_CAPACITY,
  now = () => Date.now(),
  consoleRef = globalThis.console,
} = {}) {
  const maximum = Math.max(100, Math.min(10000, Math.trunc(Number(capacity) || DEFAULT_CAPACITY)));
  const records = [];
  let sequence = 0;

  function record(level, event, details = {}) {
    const entry = Object.freeze({
      sequence: ++sequence,
      timestamp: new Date(now()).toISOString(),
      level: normalizedLevel(level),
      event: normalizedEvent(event),
      details: sanitize(details),
    });
    records.push(entry);
    if (records.length > maximum) records.splice(0, records.length - maximum);

    const method = entry.level === "debug"
      ? "debug"
      : entry.level === "warn"
        ? "warn"
        : entry.level === "error"
          ? "error"
          : "info";
    try {
      consoleRef?.[method]?.(`[SurgiMate] ${entry.event}`, entry);
    } catch {
      // Diagnostics must never interrupt the monitoring UI.
    }
    return entry;
  }

  return Object.freeze({
    record,
    entries() { return clone(records); },
    clear() { records.length = 0; },
    exportJson() {
      return JSON.stringify({
        schema: "surgimate.connection-diagnostics.v1",
        exportedAt: new Date(now()).toISOString(),
        entries: records,
      }, null, 2);
    },
    get size() { return records.length; },
    get capacity() { return maximum; },
  });
}
