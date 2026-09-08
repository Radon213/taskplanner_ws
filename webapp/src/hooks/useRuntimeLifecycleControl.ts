import { useCallback, useEffect, useRef, useState } from "react";

import { parseBoundedJson } from "../utils/display";

export type RuntimeLifecycleOperation =
  | "ninfer_restart"
  | "qwen_load"
  | "qwen_unload"
  | "warm_restart"
  | "clean_restart";

export type RuntimeLifecyclePhase = "idle" | "queued" | "running" | "succeeded" | "failed";

export interface NInferLifecycleStatus {
  available: boolean;
  model_id: string | null;
  model_state: string;
  detail: string;
}

export interface RuntimeLifecycleStatus {
  accepted?: boolean;
  phase: RuntimeLifecyclePhase;
  generation: number;
  job_id: string | null;
  request_id: string | null;
  operation: RuntimeLifecycleOperation | null;
  active_mode: "live" | "llm-surgeon" | "replay" | "debug" | null;
  message: string;
  retryable: boolean;
  ninfer: NInferLifecycleStatus;
}

export interface RuntimeLifecycleNotice {
  tone: "success" | "error" | "info";
  message: string;
}

const BODY_MAX_CHARS = 64 * 1024;
const FIELD_MAX_CHARS = 512;
const REQUEST_TIMEOUT_MS = 8_000;
const REFRESH_INTERVAL_MS = 2_000;
const REQUEST_ID_PATTERN = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const ACTIVE_PHASES = new Set<RuntimeLifecyclePhase>(["queued", "running"]);
const OPERATIONS = new Set<RuntimeLifecycleOperation>([
  "ninfer_restart",
  "qwen_load",
  "qwen_unload",
  "warm_restart",
  "clean_restart",
]);
const PHASES = new Set<RuntimeLifecyclePhase>([
  "idle",
  "queued",
  "running",
  "succeeded",
  "failed",
]);
const MODES = new Set(["live", "llm-surgeon", "replay", "debug"] as const);

function boundedText(value: unknown): value is string {
  return typeof value === "string" && value.length <= FIELD_MAX_CHARS;
}

function isMode(value: unknown): value is RuntimeLifecycleStatus["active_mode"] {
  return value === null || (typeof value === "string" && MODES.has(value as "live"));
}

function isNInferStatus(value: unknown): value is NInferLifecycleStatus {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const status = value as Partial<NInferLifecycleStatus>;
  return (
    typeof status.available === "boolean"
    && (status.model_id === null || boundedText(status.model_id))
    && boundedText(status.model_state)
    && boundedText(status.detail)
  );
}

function readStatus(value: unknown): RuntimeLifecycleStatus | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const status = value as Partial<RuntimeLifecycleStatus>;
  const phase = status.phase;
  const operation = status.operation;
  if (
    typeof phase !== "string"
    || !PHASES.has(phase as RuntimeLifecyclePhase)
    || !(operation === null || (typeof operation === "string" && OPERATIONS.has(operation as RuntimeLifecycleOperation)))
    || !Number.isSafeInteger(status.generation)
    || (status.generation ?? -1) < 0
    || !(status.job_id === null || boundedText(status.job_id))
    || !(status.request_id === null || (typeof status.request_id === "string" && REQUEST_ID_PATTERN.test(status.request_id)))
    || !isMode(status.active_mode)
    || !boundedText(status.message)
    || typeof status.retryable !== "boolean"
    || !isNInferStatus(status.ninfer)
    || !(status.accepted === undefined || typeof status.accepted === "boolean")
  ) return null;
  return status as RuntimeLifecycleStatus;
}

async function readResponse(response: Response): Promise<RuntimeLifecycleStatus | null> {
  try {
    return readStatus(parseBoundedJson(await response.text(), BODY_MAX_CHARS));
  } catch {
    return null;
  }
}

function unavailableStatus(message = "host runtime-control에서 런타임 상태를 읽지 못했습니다."): RuntimeLifecycleStatus {
  return {
    phase: "failed",
    generation: 0,
    job_id: null,
    request_id: null,
    operation: null,
    active_mode: null,
    message,
    retryable: true,
    ninfer: {
      available: false,
      model_id: null,
      model_state: "unavailable",
      detail: "NInfer manager 상태를 읽지 못했습니다.",
    },
  };
}

function newRequestId(): string {
  if (typeof crypto.randomUUID !== "function") {
    throw new Error("이 브라우저는 런타임 제어 request ID 생성을 지원하지 않습니다.");
  }
  const requestId = crypto.randomUUID();
  if (!REQUEST_ID_PATTERN.test(requestId)) {
    throw new Error("브라우저가 올바른 런타임 제어 request ID를 만들지 못했습니다.");
  }
  return requestId;
}

function operationLabel(operation: RuntimeLifecycleOperation): string {
  if (operation === "ninfer_restart") return "NInfer 재시작";
  if (operation === "qwen_load") return "Qwen 로드";
  if (operation === "qwen_unload") return "Qwen 언로드";
  if (operation === "warm_restart") return "Warm 재시작";
  return "Clean 재시작";
}

/**
 * A small projection of host-owned lifecycle controls.  The dashboard sends
 * only one allowlisted operation and then renders host status; it never
 * models Docker, model credentials, or restart implementation details.
 */
export function useRuntimeLifecycleControl() {
  const [status, setStatus] = useState<RuntimeLifecycleStatus>(() => unavailableStatus());
  const [notice, setNotice] = useState<RuntimeLifecycleNotice | null>(null);
  const mountedRef = useRef(true);
  const refreshingRef = useRef(false);
  const requestInFlightRef = useRef(false);

  const refresh = useCallback(async () => {
    if (refreshingRef.current) return false;
    refreshingRef.current = true;
    try {
      const response = await fetch("/api/runtime/lifecycle", {
        cache: "no-store",
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });
      const next = await readResponse(response);
      if (!response.ok || !next) throw new Error("runtime lifecycle status unavailable");
      if (mountedRef.current) setStatus(next);
      return true;
    } catch {
      if (mountedRef.current) setStatus((current) => ({
        ...unavailableStatus(),
        generation: current.generation,
        ninfer: current.ninfer,
      }));
      return false;
    } finally {
      refreshingRef.current = false;
    }
  }, []);

  const request = useCallback(async (operation: RuntimeLifecycleOperation) => {
    if (requestInFlightRef.current) return false;
    let requestId: string;
    try {
      requestId = newRequestId();
    } catch (error) {
      if (mountedRef.current) {
        setNotice({
          tone: "error",
          message: error instanceof Error ? error.message : "런타임 제어 request ID를 만들지 못했습니다.",
        });
      }
      return false;
    }
    requestInFlightRef.current = true;
    if (mountedRef.current) {
      setNotice({ tone: "info", message: `${operationLabel(operation)} 요청을 호스트에 전달하고 있습니다.` });
    }
    try {
      let response: Response;
      try {
        response = await fetch("/api/runtime/lifecycle", {
          method: "POST",
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
            "X-Taskplanner-Request-Id": requestId,
          },
          body: JSON.stringify({ operation }),
          signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
        });
      } catch {
        if (mountedRef.current) {
          setNotice({
            tone: "error",
            message: "요청 응답을 받지 못했습니다. 중복 요청은 보내지 않았습니다. 상태를 다시 확인하세요.",
          });
        }
        void refresh();
        return false;
      }
      const next = await readResponse(response);
      if (!next || next.request_id !== requestId || next.operation !== operation) {
        if (mountedRef.current) setNotice({ tone: "error", message: "호스트 런타임 제어 응답을 확인할 수 없습니다." });
        return false;
      }
      if (mountedRef.current) setStatus(next);
      if (!response.ok || next.accepted !== true) {
        if (mountedRef.current) setNotice({ tone: "error", message: next.message || "호스트가 런타임 요청을 거부했습니다." });
        return false;
      }
      if (mountedRef.current) setNotice({ tone: "success", message: next.message || `${operationLabel(operation)} 요청을 수락했습니다.` });
      return true;
    } finally {
      requestInFlightRef.current = false;
    }
  }, [refresh]);

  useEffect(() => {
    mountedRef.current = true;
    void refresh();
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    const interval = window.setInterval(refreshWhenVisible, REFRESH_INTERVAL_MS);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      mountedRef.current = false;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [refresh]);

  return {
    status,
    notice,
    refresh,
    request,
    pending: requestInFlightRef.current || ACTIVE_PHASES.has(status.phase),
  };
}
