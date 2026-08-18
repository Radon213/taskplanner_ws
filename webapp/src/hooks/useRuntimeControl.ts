import { useCallback, useEffect, useRef, useState } from "react";

import type { TaskplannerRuntimeMode } from "../runtimeModes";

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
    (status.message === undefined || typeof status.message === "string") &&
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
    const payload: unknown = await response.json();
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

  const refresh = useCallback(async () => {
    const generation = transitionGenerationRef.current;
    try {
      const response = await fetch("/api/runtime/status", { cache: "no-store" });
      const payload = await readApiStatus(response);
      if (generation !== transitionGenerationRef.current) return false;
      if (!response.ok || !payload) throw new Error("runtime control status unavailable");
      setStatus(fromApiStatus(payload));
      return true;
    } catch {
      if (generation !== transitionGenerationRef.current) return false;
      setStatus(unavailableStatus());
      return false;
    }
  }, []);

  const requestTransition = useCallback(async (mode: TaskplannerRuntimeMode) => {
    if (transitionInFlightRef.current !== null) return false;
    transitionInFlightRef.current = mode;
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
        setStatus(unavailableStatus());
      }
      return false;
    } finally {
      if (generation === transitionGenerationRef.current) {
        transitionInFlightRef.current = null;
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const interval = window.setInterval(
      () => void refresh(),
      status.phase === "starting" ? 1400 : 10_000,
    );
    return () => window.clearInterval(interval);
  }, [refresh, status.phase]);

  return { status, requestTransition, refresh };
}
