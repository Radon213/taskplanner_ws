import { useCallback, useEffect, useRef, useState } from "react";

import { parseBoundedJson } from "../utils/display";

// Keep this in step with the runtime-owner registry.  Standalone Debug has
// its own independently restartable observer/control owners, so treating it
// as an inapplicable mode hid useful scoped recovery controls from Debug.
export type RuntimeOwnerMode = "live" | "llm-surgeon" | "replay" | "debug";

export interface RuntimeOwnerStatus {
  owner: string;
  mode: RuntimeOwnerMode;
  state: string;
  service: string;
  detail: string;
}

export type RuntimeOwnerSnapshotPhase = "loading" | "ready" | "unavailable" | "not_applicable";

export interface RuntimeOwnerSnapshot {
  phase: RuntimeOwnerSnapshotPhase;
  mode: RuntimeOwnerMode | null;
  owners: readonly RuntimeOwnerStatus[];
  message: string;
}

export interface RuntimeOwnerControlNotice {
  tone: "success" | "error" | "info";
  message: string;
}

interface RuntimeOwnerStatusResponse {
  mode: RuntimeOwnerMode;
  owners: RuntimeOwnerStatus[];
}

interface RuntimeOwnerRestartResponse {
  accepted: boolean;
  owner: string;
  mode: RuntimeOwnerMode;
  message?: string;
  error?: string;
}

const OWNER_STATUS_BODY_MAX_CHARS = 64 * 1024;
const OWNER_STATUS_REQUEST_MS = 8_000;
const OWNER_RESTART_REQUEST_MS = 80_000;
const OWNER_STATUS_REFRESH_MS = 5_000;
const OWNER_FIELD_MAX_CHARS = 512;
const OWNER_LIST_MAX_ITEMS = 64;
const REQUEST_ID_PATTERN = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;

function isRuntimeOwnerMode(value: unknown): value is RuntimeOwnerMode {
  return value === "live" || value === "llm-surgeon" || value === "replay" || value === "debug";
}

function isBoundedText(value: unknown): value is string {
  return typeof value === "string" && value.length <= OWNER_FIELD_MAX_CHARS;
}

function isRuntimeOwnerStatus(value: unknown): value is RuntimeOwnerStatus {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const owner = value as Partial<RuntimeOwnerStatus>;
  return (
    isBoundedText(owner.owner)
    && isRuntimeOwnerMode(owner.mode)
    && isBoundedText(owner.state)
    && isBoundedText(owner.service)
    && isBoundedText(owner.detail)
  );
}

function readOwnerStatusResponse(value: unknown): RuntimeOwnerStatusResponse | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const response = value as Partial<RuntimeOwnerStatusResponse>;
  if (!isRuntimeOwnerMode(response.mode) || !Array.isArray(response.owners)) return null;
  if (response.owners.length > OWNER_LIST_MAX_ITEMS || !response.owners.every(isRuntimeOwnerStatus)) return null;
  if (response.owners.some((owner) => owner.mode !== response.mode)) return null;
  return { mode: response.mode, owners: response.owners };
}

function readRestartResponse(value: unknown): RuntimeOwnerRestartResponse | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const response = value as Partial<RuntimeOwnerRestartResponse>;
  if (
    typeof response.accepted !== "boolean"
    || !isBoundedText(response.owner)
    || !isRuntimeOwnerMode(response.mode)
  ) return null;
  if (response.message !== undefined && !isBoundedText(response.message)) return null;
  if (response.error !== undefined && !isBoundedText(response.error)) return null;
  return response as RuntimeOwnerRestartResponse;
}

async function readResponseJson(response: Response): Promise<unknown | null> {
  try {
    return parseBoundedJson(await response.text(), OWNER_STATUS_BODY_MAX_CHARS);
  } catch {
    return null;
  }
}

function noApplicableOwnerSnapshot(): RuntimeOwnerSnapshot {
  return {
    phase: "not_applicable",
    mode: null,
    owners: [],
    message: "독립 Debug 모드에는 runtime owner 제어 대상이 없습니다.",
  };
}

function unavailableOwnerSnapshot(mode: RuntimeOwnerMode): RuntimeOwnerSnapshot {
  return {
    phase: "unavailable",
    mode,
    owners: [],
    message: "호스트 runtime-control에서 owner 상태를 받을 수 없습니다.",
  };
}

function loadingOwnerSnapshot(mode: RuntimeOwnerMode): RuntimeOwnerSnapshot {
  return { phase: "loading", mode, owners: [], message: "runtime owner 상태를 확인하고 있습니다." };
}

function newRequestId(): string {
  if (typeof crypto.randomUUID !== "function") {
    throw new Error("이 브라우저는 runtime owner 재시작 request ID 생성을 지원하지 않습니다.");
  }
  const requestId = crypto.randomUUID();
  if (!REQUEST_ID_PATTERN.test(requestId)) {
    throw new Error("브라우저가 올바른 runtime owner 재시작 request ID를 생성하지 못했습니다.");
  }
  return requestId;
}

/**
 * Host-owned runtime owner observation and scoped restart requests.
 *
 * The browser deliberately does not model Compose, launch, or eligibility
 * rules. It renders the host's read-only projection and sends one bounded
 * request; the host is the sole authority that can admit or reject it.
 */
export function useRuntimeOwnerControl(mode: RuntimeOwnerMode | null) {
  const [snapshot, setSnapshot] = useState<RuntimeOwnerSnapshot>(() =>
    mode ? loadingOwnerSnapshot(mode) : noApplicableOwnerSnapshot(),
  );
  const [pendingOwner, setPendingOwner] = useState<string | null>(null);
  const [notice, setNotice] = useState<RuntimeOwnerControlNotice | null>(null);
  const mountedRef = useRef(true);
  const refreshInFlightRef = useRef(false);
  const refreshGenerationRef = useRef(0);
  const pendingOwnerRef = useRef<string | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      refreshGenerationRef.current += 1;
      refreshInFlightRef.current = false;
      pendingOwnerRef.current = null;
    };
  }, []);

  const refresh = useCallback(async () => {
    if (!mode) {
      if (mountedRef.current) setSnapshot(noApplicableOwnerSnapshot());
      return false;
    }
    if (refreshInFlightRef.current) return false;
    refreshInFlightRef.current = true;
    const generation = refreshGenerationRef.current + 1;
    refreshGenerationRef.current = generation;
    try {
      const response = await fetch(`/api/runtime/owners?mode=${encodeURIComponent(mode)}`, {
        cache: "no-store",
        signal: AbortSignal.timeout(OWNER_STATUS_REQUEST_MS),
      });
      const payload = readOwnerStatusResponse(await readResponseJson(response));
      if (!mountedRef.current || generation !== refreshGenerationRef.current) return false;
      if (!response.ok || !payload || payload.mode !== mode) {
        throw new Error("runtime owner status unavailable");
      }
      setSnapshot({
        phase: "ready",
        mode: payload.mode,
        owners: payload.owners,
        message: "호스트 runtime-control 상태입니다.",
      });
      return true;
    } catch {
      if (mountedRef.current && generation === refreshGenerationRef.current) {
        setSnapshot(unavailableOwnerSnapshot(mode));
      }
      return false;
    } finally {
      if (generation === refreshGenerationRef.current) {
        refreshInFlightRef.current = false;
      }
    }
  }, [mode]);

  const restart = useCallback(async (owner: string) => {
    if (!mode || pendingOwnerRef.current !== null) return false;
    let requestId: string;
    try {
      requestId = newRequestId();
    } catch (error) {
      if (mountedRef.current) {
        setNotice({
          tone: "error",
          message: error instanceof Error ? error.message : "runtime owner 재시작 request ID를 만들지 못했습니다.",
        });
      }
      return false;
    }

    pendingOwnerRef.current = owner;
    if (mountedRef.current) {
      setPendingOwner(owner);
      setNotice({ tone: "info", message: `${owner} owner 재시작 요청을 호스트에 전달하고 있습니다.` });
    }
    try {
      let response: Response;
      try {
        response = await fetch("/api/runtime/owners/restart", {
          method: "POST",
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
            "X-Taskplanner-Request-Id": requestId,
          },
          body: JSON.stringify({ owner, mode }),
          signal: AbortSignal.timeout(OWNER_RESTART_REQUEST_MS),
        });
      } catch {
        if (mountedRef.current) {
          setNotice({
            tone: "error",
            message: "재시작 요청 응답을 받지 못했습니다. 중복 요청은 보내지 않았습니다. owner 상태를 다시 확인하세요.",
          });
        }
        void refresh();
        return false;
      }

      const payload = readRestartResponse(await readResponseJson(response));
      if (
        !payload
        || payload.owner !== owner
        || payload.mode !== mode
        || payload.accepted !== response.ok
      ) {
        if (mountedRef.current) {
          setNotice({ tone: "error", message: "호스트의 runtime owner 재시작 응답을 확인할 수 없습니다." });
        }
        return false;
      }
      if (!payload.accepted) {
        if (mountedRef.current) {
          setNotice({
            tone: "error",
            message: payload.error || "호스트가 runtime owner 재시작 요청을 거부했습니다.",
          });
        }
        return false;
      }
      if (mountedRef.current) {
        setNotice({ tone: "success", message: payload.message || `${owner} owner가 재시작되었습니다.` });
      }
      void refresh();
      return true;
    } finally {
      if (pendingOwnerRef.current === owner) {
        pendingOwnerRef.current = null;
        if (mountedRef.current) setPendingOwner(null);
      }
    }
  }, [mode, refresh]);

  useEffect(() => {
    // A response for the previous selected mode must not overwrite the
    // observation surface after a workspace/runtime transition.
    refreshGenerationRef.current += 1;
    refreshInFlightRef.current = false;
    if (!mode) {
      setSnapshot(noApplicableOwnerSnapshot());
      setNotice(null);
      return undefined;
    }
    setSnapshot(loadingOwnerSnapshot(mode));
    void refresh();
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    const interval = window.setInterval(refreshWhenVisible, OWNER_STATUS_REFRESH_MS);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [mode, refresh]);

  return { snapshot, pendingOwner, notice, refresh, restart };
}
