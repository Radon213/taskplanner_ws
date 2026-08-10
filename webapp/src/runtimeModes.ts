export type TaskplannerRuntimeMode = "live" | "llm" | "shadow" | "debug";

const STORAGE_KEY = "taskplanner.runtimeMode";

function isRuntimeMode(value: string | null | undefined): value is TaskplannerRuntimeMode {
  return value === "live" || value === "llm" || value === "shadow" || value === "debug";
}

function browserProtocol(): "ws:" | "wss:" {
  if (typeof window === "undefined") return "ws:";
  return window.location.protocol === "https:" ? "wss:" : "ws:";
}

function browserHostname(): string {
  if (typeof window === "undefined") return "127.0.0.1";
  return window.location.hostname || "127.0.0.1";
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
  return `${browserProtocol()}//${browserHostname()}:${configuredPort(mode)}`;
}

export function initialRuntimeMode(): TaskplannerRuntimeMode {
  if (typeof window !== "undefined") {
    const storedMode = window.localStorage.getItem(STORAGE_KEY);
    if (isRuntimeMode(storedMode)) return storedMode;
  }
  const configuredMode = import.meta.env.VITE_DEFAULT_RUNTIME_MODE?.trim();
  return isRuntimeMode(configuredMode) ? configuredMode : "llm";
}

export function persistRuntimeMode(mode: TaskplannerRuntimeMode): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(STORAGE_KEY, mode);
}
