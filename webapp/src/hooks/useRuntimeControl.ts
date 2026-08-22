import { useCallback, useEffect, useRef, useState } from "react";

import type { TaskplannerRuntimeMode } from "../runtimeModes";
import { parseBoundedJson } from "../utils/display";

type LauncherRuntimeMode = "live" | "llm-surgeon" | "replay" | "debug";
export type RuntimeTransitionPhase =
  | "checking"
  | "idle"
  | "starting"
  | "blocked"
  | "failed"
  | "unavailable";

export interface RuntimeTransitionStatus {
  phase: RuntimeTransitionPhase;
  activeMode: TaskplannerRuntimeMode | null;
  requestedMode: TaskplannerRuntimeMode | null;
  message: string;
  retryable: boolean;
}

interface RuntimeControlApiStatus {
  phase: "idle" | "starting" | "failed";
  active_mode: LauncherRuntimeMode | null;
  requested_mode: LauncherRuntimeMode | null;
  message?: string;
  retryable: boolean;
}

const MAX_RUNTIME_STATUS_BODY_CHARS = 128 * 1024;
const MAX_RUNTIME_STATUS_MESSAGE_CHARS = 4_096;
const MAX_RUNTIME_STATUS_REQUEST_MS = 8_000;
// Replay/Debug transition admission may include a fail-closed ROS state probe
// that itself is bounded at eight seconds. Keep enough margin for that probe
// and HTTP overhead, while still preventing an unbounded UI wait.
const MAX_RUNTIME_TRANSITION_REQUEST_MS = 15_000;

const launcherModeByUiMode: Record<TaskplannerRuntimeMode, LauncherRuntimeMode> = {
  live: "live",
  llm: "llm-surgeon",
  shadow: "replay",
  debug: "debug",
};

function uiModeFromLauncher(mode: LauncherRuntimeMode | null | undefined): TaskplannerRuntimeMode | null {
  if (mode === "live") return "live";
  if (mode === "llm-surgeon") return "llm";
  if (mode === "replay") return "shadow";
  if (mode === "debug") return "debug";
  return null;
}

function isApiStatus(value: unknown): value is RuntimeControlApiStatus {
  if (!value || typeof value !== "object") return false;
  const status = value as Partial<RuntimeControlApiStatus>;
  return (
    (status.phase === "idle" || status.phase === "starting" || status.phase === "failed") &&
    (status.active_mode === null || status.active_mode === undefined ||
      status.active_mode === "live" || status.active_mode === "llm-surgeon" ||
      status.active_mode === "replay" || status.active_mode === "debug") &&
    (status.requested_mode === null || status.requested_mode === undefined ||
      status.requested_mode === "live" || status.requested_mode === "llm-surgeon" ||
      status.requested_mode === "replay" || status.requested_mode === "debug") &&
    (status.message === undefined || (typeof status.message === "string" && status.message.length <= MAX_RUNTIME_STATUS_MESSAGE_CHARS)) &&
    typeof status.retryable === "boolean"
  );
}

function fromApiStatus(status: RuntimeControlApiStatus): RuntimeTransitionStatus {
  return {
    phase: status.phase,
    activeMode: uiModeFromLauncher(status.active_mode),
    requestedMode: uiModeFromLauncher(status.requested_mode),
    message: status.message?.trim() || "",
    retryable: status.retryable,
  };
}

function unavailableStatus(): RuntimeTransitionStatus {
  return {
    phase: "unavailable",
    activeMode: null,
    requestedMode: null,
    message: "",
    retryable: true,
  };
}

async function readApiStatus(response: Response): Promise<RuntimeControlApiStatus | null> {
  try {
    const raw = await response.text();
    const payload = parseBoundedJson(raw, MAX_RUNTIME_STATUS_BODY_CHARS);
    return isApiStatus(payload) ? payload : null;
  } catch {
    return null;
  }
}

export function useRuntimeControl() {
  const [status, setStatus] = useState<RuntimeTransitionStatus>({
    phase: "checking",
    activeMode: null,
    requestedMode: null,
    message: "",
    retryable: false,
  });
  const transitionGenerationRef = useRef(0);
  const transitionInFlightRef = useRef<TaskplannerRuntimeMode | null>(null);
  const transitionEpochRef = useRef(0);
  const refreshRunIdRef = useRef(0);
  const refreshInFlightRef = useRef(false);

  useEffect(() => {
    return () => {
      // A workspace can disappear while the launcher request is still
      // resolving. Invalidate that response before the hook is discarded so a
      // late status cannot leak into a newly mounted Mission instance.
      transitionGenerationRef.current += 1;
      transitionEpochRef.current += 1;
      refreshRunIdRef.current += 1;
      refreshInFlightRef.current = false;
      transitionInFlightRef.current = null;
    };
  }, []);

  const refresh = useCallback(async () => {
    if (refreshInFlightRef.current) return false;
    refreshInFlightRef.current = true;
    const refreshRunId = refreshRunIdRef.current + 1;
    refreshRunIdRef.current = refreshRunId;
    const generation = transitionGenerationRef.current;
    const transitionEpoch = transitionEpochRef.current;
    try {
      const response = await fetch("/api/runtime/status", {
        cache: "no-store",
        signal: AbortSignal.timeout(MAX_RUNTIME_STATUS_REQUEST_MS),
      });
      const payload = await readApiStatus(response);
      if (
        refreshRunId !== refreshRunIdRef.current ||
        generation !== transitionGenerationRef.current ||
        transitionEpoch !== transitionEpochRef.current
      ) return false;
      if (!response.ok || !payload) throw new Error("runtime control status unavailable");
      const nextStatus = fromApiStatus(payload);
      // While the launcher POST is unresolved, an older status snapshot must
      // not release the UI lock or reconnect Mission to the previous mode.
      if (transitionInFlightRef.current !== null && nextStatus.phase !== "starting") {
        return true;
      }
      setStatus(nextStatus);
      return true;
    } catch {
      if (
        refreshRunId !== refreshRunIdRef.current ||
        generation !== transitionGenerationRef.current ||
        transitionEpoch !== transitionEpochRef.current
      ) return false;
      setStatus(unavailableStatus());
      return false;
    } finally {
      if (refreshRunId === refreshRunIdRef.current) {
        refreshInFlightRef.current = false;
      }
    }
  }, []);

  const requestTransition = useCallback(async (mode: TaskplannerRuntimeMode) => {
    if (transitionInFlightRef.current !== null) return false;
    transitionInFlightRef.current = mode;
    const transitionEpoch = transitionEpochRef.current + 1;
    transitionEpochRef.current = transitionEpoch;
    // Do not leave a pre-transition status request occupying the refresh
    // slot. Its response is invalidated by both the run id and epoch checks.
    refreshRunIdRef.current += 1;
    refreshInFlightRef.current = false;
    const generation = transitionGenerationRef.current + 1;
    transitionGenerationRef.current = generation;
    setStatus((current) => ({
      phase: "starting",
      activeMode: current.activeMode,
      requestedMode: mode,
      message: "",
      retryable: false,
    }));
    try {
      const response = await fetch("/api/runtime/transition", {
        method: "POST",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: launcherModeByUiMode[mode] }),
        signal: AbortSignal.timeout(MAX_RUNTIME_TRANSITION_REQUEST_MS),
      });
      const payload = await readApiStatus(response);
      if (generation !== transitionGenerationRef.current) return false;
      if (!response.ok) {
        if (payload) {
          const rejectedStatus = fromApiStatus(payload);
          setStatus({
            ...rejectedStatus,
            phase: response.status === 409 ? "blocked" : "failed",
            requestedMode: rejectedStatus.requestedMode ?? mode,
          });
        } else {
          setStatus(unavailableStatus());
        }
        return false;
      }
      if (payload) setStatus(fromApiStatus(payload));
      return true;
    } catch {
      if (generation === transitionGenerationRef.current) {
        // The host may have admitted the transition even if its HTTP response
        // was lost. Keep the UI fail-closed, then reconcile immediately from
        // the authoritative status endpoint instead of offering a duplicate
        // transition request based on transport uncertainty alone.
        setStatus((current) => ({
          phase: "checking",
          activeMode: current.activeMode,
          requestedMode: mode,
          message: "Runtime transition response was not received. Checking host state.",
          retryable: false,
        }));
        refreshRunIdRef.current += 1;
        refreshInFlightRef.current = false;
        window.setTimeout(() => void refresh(), 0);
      }
      return false;
    } finally {
      if (generation === transitionGenerationRef.current) {
        if (transitionEpochRef.current === transitionEpoch) {
          transitionEpochRef.current += 1;
        }
        transitionInFlightRef.current = null;
      }
    }
  }, [refresh]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    const interval = window.setInterval(
      refreshWhenVisible,
      status.phase === "starting" ? 1400 : 10_000,
    );
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [refresh, status.phase]);

  return { status, requestTransition, refresh };
}
