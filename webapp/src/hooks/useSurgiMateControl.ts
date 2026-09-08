import { useCallback, useEffect, useRef, useState } from "react";

import { parseBoundedJson } from "../utils/display";

export type SurgiMateAction = "start" | "stop" | "restart";
export type SurgiMateControlPhase = "loading" | "ready" | "unavailable";
export type SurgiMateOwnerMode = "debug" | "live";

export interface SurgiMateOwnerStatus {
  owner: "surgimate";
  mode: SurgiMateOwnerMode;
  state: string;
  service: string;
  detail: string;
}

export interface SurgiMateControlStatus {
  phase: SurgiMateControlPhase;
  activeMode: string | null;
  owner: SurgiMateOwnerStatus | null;
  message: string;
}

export interface SurgiMateControlNotice {
  tone: "success" | "error" | "info";
  message: string;
}

interface SurgiMateStatusResponse {
  active_mode: string | null;
  status: SurgiMateOwnerStatus;
}

interface SurgiMateActionResponse {
  accepted: boolean;
  action: SurgiMateAction;
  message?: string;
  error?: string;
}

const RESPONSE_MAX_CHARS = 16 * 1024;
const STATUS_REQUEST_MS = 8_000;
const ACTION_REQUEST_MS = 35_000;
const REFRESH_MS = 5_000;
const FIELD_MAX_CHARS = 512;
const REQUEST_ID_PATTERN = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;

function isBoundedText(value: unknown): value is string {
  return typeof value === "string" && value.length <= FIELD_MAX_CHARS;
}

function isAction(value: unknown): value is SurgiMateAction {
  return value === "start" || value === "stop" || value === "restart";
}

function isOwnerMode(value: unknown): value is SurgiMateOwnerMode {
  return value === "debug" || value === "live";
}

function isOwnerStatus(value: unknown): value is SurgiMateOwnerStatus {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const candidate = value as Partial<SurgiMateOwnerStatus>;
  return candidate.owner === "surgimate"
    && isOwnerMode(candidate.mode)
    && isBoundedText(candidate.state)
    && isBoundedText(candidate.service)
    && isBoundedText(candidate.detail);
}

function readStatusResponse(value: unknown): SurgiMateStatusResponse | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as Partial<SurgiMateStatusResponse>;
  if (candidate.active_mode !== null && !isBoundedText(candidate.active_mode)) return null;
  if (!isOwnerStatus(candidate.status)) return null;
  return { active_mode: candidate.active_mode, status: candidate.status };
}

function readActionResponse(value: unknown): SurgiMateActionResponse | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as Partial<SurgiMateActionResponse>;
  if (typeof candidate.accepted !== "boolean" || !isAction(candidate.action)) return null;
  if (candidate.message !== undefined && !isBoundedText(candidate.message)) return null;
  if (candidate.error !== undefined && !isBoundedText(candidate.error)) return null;
  return candidate as SurgiMateActionResponse;
}

async function readResponseJson(response: Response): Promise<unknown | null> {
  try {
    return parseBoundedJson(await response.text(), RESPONSE_MAX_CHARS);
  } catch {
    return null;
  }
}

function initialStatus(): SurgiMateControlStatus {
  return {
    phase: "loading",
    activeMode: null,
    owner: null,
    message: "SurgiMate sidecar 상태를 확인하고 있습니다.",
  };
}

function unavailableStatus(): SurgiMateControlStatus {
  return {
    phase: "unavailable",
    activeMode: null,
    owner: null,
    message: "host runtime-control에서 SurgiMate 상태를 받을 수 없습니다.",
  };
}

function newRequestId(): string {
  if (typeof crypto.randomUUID !== "function") {
    throw new Error("이 브라우저는 SurgiMate 제어 request ID 생성을 지원하지 않습니다.");
  }
  const requestId = crypto.randomUUID();
  if (!REQUEST_ID_PATTERN.test(requestId)) {
    throw new Error("브라우저가 올바른 SurgiMate 제어 request ID를 생성하지 못했습니다.");
  }
  return requestId;
}

/**
 * Host-owned observation and fixed lifecycle control for the independent
 * SurgiMate sidecar. The browser never receives a Compose command or a ROS
 * credential.  Runtime-control remains the authority for the current Debug
 * workspace's reviewed Start/Stop/Restart boundary.
 */
export function useSurgiMateControl() {
  const [status, setStatus] = useState<SurgiMateControlStatus>(initialStatus);
  const [pendingAction, setPendingAction] = useState<SurgiMateAction | null>(null);
  const [notice, setNotice] = useState<SurgiMateControlNotice | null>(null);
  const mountedRef = useRef(true);
  const refreshInFlightRef = useRef(false);
  const refreshGenerationRef = useRef(0);
  const pendingActionRef = useRef<SurgiMateAction | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      refreshGenerationRef.current += 1;
      refreshInFlightRef.current = false;
      pendingActionRef.current = null;
    };
  }, []);

  const refresh = useCallback(async () => {
    if (refreshInFlightRef.current) return false;
    refreshInFlightRef.current = true;
    const generation = refreshGenerationRef.current + 1;
    refreshGenerationRef.current = generation;
    try {
      const response = await fetch("/api/runtime/surgimate", {
        cache: "no-store",
        signal: AbortSignal.timeout(STATUS_REQUEST_MS),
      });
      const payload = readStatusResponse(await readResponseJson(response));
      if (!mountedRef.current || generation !== refreshGenerationRef.current) return false;
      if (!response.ok || !payload) throw new Error("SurgiMate status unavailable");
      setStatus({
        phase: "ready",
        activeMode: payload.active_mode,
        owner: payload.status,
        message: payload.active_mode === "debug" || payload.active_mode === "live"
          ? "현재 Debug workspace에서 SurgiMate sidecar를 제어할 수 있습니다."
          : "상태는 읽을 수 있지만 현재 runtime에서는 SurgiMate 제어가 허용되지 않습니다.",
      });
      return true;
    } catch {
      if (mountedRef.current && generation === refreshGenerationRef.current) {
        setStatus(unavailableStatus());
      }
      return false;
    } finally {
      if (generation === refreshGenerationRef.current) refreshInFlightRef.current = false;
    }
  }, []);

  const request = useCallback(async (action: SurgiMateAction) => {
    if (pendingActionRef.current !== null) return false;
    let requestId: string;
    try {
      requestId = newRequestId();
    } catch (error) {
      if (mountedRef.current) {
        setNotice({
          tone: "error",
          message: error instanceof Error ? error.message : "SurgiMate 제어 request ID를 만들지 못했습니다.",
        });
      }
      return false;
    }

    pendingActionRef.current = action;
    if (mountedRef.current) {
      setPendingAction(action);
      setNotice({ tone: "info", message: `SurgiMate ${action} 요청을 호스트에 전달하고 있습니다.` });
    }
    try {
      let response: Response;
      try {
        response = await fetch("/api/runtime/surgimate", {
          method: "POST",
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
            "X-Taskplanner-Request-Id": requestId,
          },
          body: JSON.stringify({ action }),
          signal: AbortSignal.timeout(ACTION_REQUEST_MS),
        });
      } catch {
        if (mountedRef.current) {
          setNotice({
            tone: "error",
            message: "SurgiMate 요청 응답을 받지 못했습니다. 중복 요청은 보내지 않았습니다. 상태를 다시 확인하세요.",
          });
        }
        void refresh();
        return false;
      }
      const payload = readActionResponse(await readResponseJson(response));
      if (!payload || payload.action !== action || payload.accepted !== response.ok) {
        if (mountedRef.current) {
          setNotice({ tone: "error", message: "호스트의 SurgiMate 제어 응답을 확인할 수 없습니다." });
        }
        return false;
      }
      if (!payload.accepted) {
        if (mountedRef.current) {
          setNotice({ tone: "error", message: payload.error || "호스트가 SurgiMate 요청을 거부했습니다." });
        }
        return false;
      }
      if (mountedRef.current) {
        setNotice({ tone: "success", message: payload.message || `SurgiMate ${action} 요청이 완료되었습니다.` });
      }
      void refresh();
      return true;
    } finally {
      if (pendingActionRef.current === action) {
        pendingActionRef.current = null;
        if (mountedRef.current) setPendingAction(null);
      }
    }
  }, [refresh]);

  useEffect(() => {
    void refresh();
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    const interval = window.setInterval(refreshWhenVisible, REFRESH_MS);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [refresh]);

  return { status, pendingAction, notice, refresh, request };
}
