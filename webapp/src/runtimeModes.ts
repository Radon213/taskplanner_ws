export type TaskplannerRuntimeMode = "live" | "llm" | "shadow" | "debug";

const STORAGE_KEY_PREFIX = "taskplanner.runtimeMode";
const LAST_MISSION_STORAGE_KEY_PREFIX = "taskplanner.lastMissionMode";

function isRuntimeMode(value: string | null | undefined): value is TaskplannerRuntimeMode {
  return value === "live" || value === "llm" || value === "shadow" || value === "debug";
}

function configuredDefaultRuntimeMode(): TaskplannerRuntimeMode {
  const configuredMode = import.meta.env.VITE_DEFAULT_RUNTIME_MODE?.trim();
  return isRuntimeMode(configuredMode) ? configuredMode : "llm";
}

function scopedStorageKey(prefix: string): string {
  // Do not migrate the legacy unscoped preference: it may have been saved by
  // a different deployment profile (for example, an earlier Shadow replay).
  return `${prefix}.${configuredDefaultRuntimeMode()}`;
}

function runtimeModeStorageKey(): string {
  return scopedStorageKey(STORAGE_KEY_PREFIX);
}

export function lastMissionModeStorageKey(): string {
  return scopedStorageKey(LAST_MISSION_STORAGE_KEY_PREFIX);
}

function browserProtocol(): "ws:" | "wss:" {
  if (typeof window === "undefined") return "ws:";
  return window.location.protocol === "https:" ? "wss:" : "ws:";
}

function browserHostname(): string {
  if (typeof window === "undefined") return "127.0.0.1";
  return window.location.hostname || "127.0.0.1";
}

function websocketHostname(): string {
  const hostname = browserHostname();
  return hostname.includes(":") && !hostname.startsWith("[") ? `[${hostname}]` : hostname;
}

function isLoopbackHostname(hostname: string): boolean {
  const normalized = hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (normalized === "localhost" || normalized.endsWith(".localhost") || normalized === "::1") {
    return true;
  }
  const octets = normalized.split(".");
  return octets.length === 4 && octets[0] === "127" && octets.every((octet) => {
    const value = Number(octet);
    return Number.isInteger(value) && value >= 0 && value <= 255;
  });
}

type RemotePathRouter = { port: string; path: string };

function remotePathRouter(mode: TaskplannerRuntimeMode): RemotePathRouter | null {
  if (mode === "debug" || isLoopbackHostname(browserHostname())) return null;
  const genericPort = import.meta.env.VITE_ROSBRIDGE_TAILSCALE_PORT?.trim() || "9091";
  if (mode === "live") {
    const port = import.meta.env.VITE_ROSBRIDGE_LIVE_TAILSCALE_PORT?.trim() || genericPort;
    const path = import.meta.env.VITE_ROSBRIDGE_LIVE_TAILSCALE_PATH?.trim() || "/live";
    return { port, path };
  }
  const path = mode === "llm"
    ? import.meta.env.VITE_ROSBRIDGE_LLM_TAILSCALE_PATH?.trim() || "/llm"
    : import.meta.env.VITE_ROSBRIDGE_SHADOW_TAILSCALE_PATH?.trim() || "/shadow";
  return { port: genericPort, path };
}

function configuredUrl(mode: TaskplannerRuntimeMode): string {
  if (mode === "debug") {
    return import.meta.env.VITE_ROSBRIDGE_DEBUG_URL?.trim() || "";
  }
  if (mode === "live") {
    return import.meta.env.VITE_ROSBRIDGE_LIVE_URL?.trim() || "";
  }
  if (mode === "shadow") {
    return import.meta.env.VITE_ROSBRIDGE_SHADOW_URL?.trim() || "";
  }
  return import.meta.env.VITE_ROSBRIDGE_LLM_URL?.trim() || "";
}

function configuredPort(mode: TaskplannerRuntimeMode): string {
  const sharedPort = import.meta.env.VITE_ROSBRIDGE_PORT?.trim() || "9090";
  if (mode === "debug") {
    return import.meta.env.VITE_ROSBRIDGE_DEBUG_PORT?.trim() || "9091";
  }
  if (mode === "live") {
    return import.meta.env.VITE_ROSBRIDGE_LIVE_PORT?.trim() || sharedPort;
  }
  if (mode === "shadow") {
    return import.meta.env.VITE_ROSBRIDGE_SHADOW_PORT?.trim() || "9099";
  }
  return import.meta.env.VITE_ROSBRIDGE_LLM_PORT?.trim() || sharedPort;
}

export function runtimeBridgeUrl(mode: TaskplannerRuntimeMode): string {
  const explicitUrl = configuredUrl(mode);
  if (explicitUrl) return explicitUrl;
  const router = remotePathRouter(mode);
  if (router) return `${browserProtocol()}//${websocketHostname()}:${router.port}${router.path}`;
  return `${browserProtocol()}//${websocketHostname()}:${configuredPort(mode)}`;
}

export function multicamBridgeUrl(): string {
  const explicitUrl = import.meta.env.VITE_MULTICAM_ROSBRIDGE_URL?.trim();
  if (explicitUrl) return explicitUrl;
  const port =
    import.meta.env.VITE_MULTICAM_ROSBRIDGE_PORT?.trim() ||
    import.meta.env.VITE_ROSBRIDGE_TAILSCALE_PORT?.trim() ||
    configuredPort("debug");
  return `${browserProtocol()}//${websocketHostname()}:${port}/multicam`;
}

export function initialRuntimeMode(): TaskplannerRuntimeMode {
  if (typeof window !== "undefined") {
    const storedMode = window.localStorage.getItem(runtimeModeStorageKey());
    if (isRuntimeMode(storedMode)) return storedMode;
  }
  return configuredDefaultRuntimeMode();
}

export function persistRuntimeMode(mode: TaskplannerRuntimeMode): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(runtimeModeStorageKey(), mode);
}
