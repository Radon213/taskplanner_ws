import { runtimeModeIsAvailable } from "./runtimeFeatures";

export type TaskplannerRuntimeMode = "live" | "llm" | "shadow" | "debug";

const STORAGE_KEY_PREFIX = "taskplanner.runtimeMode";
const LAST_MISSION_STORAGE_KEY_PREFIX = "taskplanner.lastMissionMode";

function isRuntimeMode(value: string | null | undefined): value is TaskplannerRuntimeMode {
  return value === "live" || value === "llm" || value === "shadow" || value === "debug";
}

function configuredDefaultRuntimeMode(): TaskplannerRuntimeMode {
  const configuredMode = import.meta.env.VITE_DEFAULT_RUNTIME_MODE?.trim();
  return isRuntimeMode(configuredMode) && runtimeModeIsAvailable(configuredMode)
    ? configuredMode
    : "live";
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

/**
 * The standalone Debug profile owns the public loopback Debug port (9091).
 * Live/LLM integrated Debug is an observer beside an operational runtime; it
 * is intentionally bound to its private upstream (9093) so the optional LAN
 * path-router can keep serving `/live` independently.  Treating both as one
 * generic `debug` URL made a local integrated Debug workspace repeatedly
 * attempt a port that has no listener whenever the optional router was off.
 */
export function debugBridgeUrl(
  observedRuntimeMode: "live" | "llm-surgeon" | "replay" | null,
): string {
  if (observedRuntimeMode === "live" || observedRuntimeMode === "llm-surgeon") {
    const explicitUrl = import.meta.env.VITE_ROSBRIDGE_INTEGRATED_DEBUG_URL?.trim();
    if (explicitUrl) return explicitUrl;
    const port = import.meta.env.VITE_ROSBRIDGE_INTEGRATED_DEBUG_PORT?.trim() || "9093";
    return `${browserProtocol()}//${websocketHostname()}:${port}`;
  }
  return runtimeBridgeUrl("debug");
}

/**
 * Camera raster traffic has its own loopback-only bridge so high-rate JPEG
 * delivery cannot queue behind mission/control subscriptions on 9090.  A
 * deployment that terminates the browser elsewhere may override this one URL;
 * the default remains deliberately local.
 */
export function liveMediaBridgeUrl(): string {
  return import.meta.env.VITE_LIVE_MEDIA_BRIDGE_URL?.trim() || "ws://127.0.0.1:9095";
}

/**
 * SurgiMate is intentionally a separate local browser application. Keep its
 * default loopback-only; a remote viewer needs its own reviewed proxy rather
 * than inheriting Taskplanner's browser/ROS routes.
 */
export function surgimateUrl(): string {
  const explicitUrl = import.meta.env.VITE_SURGIMATE_URL?.trim();
  if (explicitUrl) return explicitUrl;
  const port = import.meta.env.VITE_SURGIMATE_PORT?.trim() || "5174";
  return `http://127.0.0.1:${port}`;
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
    if (isRuntimeMode(storedMode) && runtimeModeIsAvailable(storedMode)) return storedMode;
  }
  return configuredDefaultRuntimeMode();
}

/**
 * Pick the bridge that the deployment explicitly designates for an MCAP UI
 * replay, without inheriting a stale browser-local runtime preference.  The
 * isolated Replay profile owns the shadow bridge; a normal Live deployment
 * continues to replay its manually captured bag on the Live bridge.
 */
export function rosbagReplayRuntimeMode(): Extract<TaskplannerRuntimeMode, "live" | "shadow"> {
  return import.meta.env.VITE_DEFAULT_RUNTIME_MODE?.trim() === "shadow"
    ? "shadow"
    : "live";
}

export function persistRuntimeMode(mode: TaskplannerRuntimeMode): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(
    runtimeModeStorageKey(),
    runtimeModeIsAvailable(mode) ? mode : "live",
  );
}
