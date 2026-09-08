import { parseBoundedJson } from "../utils/display";
import type { LiveAsrControlResult, LiveAsrStatus } from "../types";

type AsrRestartPhase =
  | "idle"
  | "queued"
  | "preflighting"
  | "restarting"
  | "verifying"
  | "succeeded"
  | "failed";

export type AsrRestartApiStatus = {
  accepted?: boolean;
  phase: AsrRestartPhase;
  generation: number;
  job_id: string | null;
  request_id: string | null;
  message: string;
  retryable: boolean;
  // Older controllers may still include these diagnostics. The owner-restart
  // flow no longer needs them, so accept a minimal response as well.
  source_revision?: string | null;
  container_started_at?: string | null;
  before_pid?: number | null;
  after_pid?: number | null;
};

const STATUS_BODY_MAX_CHARS = 64 * 1024;
const STATUS_MESSAGE_MAX_CHARS = 4_096;
const HTTP_TIMEOUT_MS = 8_000;
const POLL_INTERVAL_MS = 350;
const RECONCILE_TIMEOUT_MS = 15_000;
// The backend delegates to the same small owner command as the terminal. Keep
// polling bounded, but do not model Docker/source/ROS-graph implementation
// details in the browser.
const COMPLETION_TIMEOUT_MS = 90_000;
const HEARTBEAT_TIMEOUT_MS = 20_000;
const CAPTURE_TIMEOUT_MS = 20_000;
const STATUS_FRESH_MS = 5_000;
const REQUIRED_OUTPUT_MODE = "typed_utterance";
const REQUIRED_OUTPUT_TOPIC = "/sensors/surgeon/utterance";
const REQUEST_ID_PATTERN = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const ACTIVE_PHASES = new Set<AsrRestartPhase>([
  "queued",
  "preflighting",
  "restarting",
  "verifying",
]);
const PHASES = new Set<AsrRestartPhase>([
  "idle",
  ...ACTIVE_PHASES,
  "succeeded",
  "failed",
]);
const RESTORE_CAS_OBSERVATION_PHASES = new Set<AsrRestartPhase>([
  "idle",
  "queued",
  "preflighting",
  "restarting",
]);

function optionalPositivePid(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const pid = Number(value);
  return Number.isSafeInteger(pid) && pid > 0 ? pid : null;
}

function normalizeStatus(value: unknown): AsrRestartApiStatus | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const phase = String(raw.phase ?? "") as AsrRestartPhase;
  const generation = Number(raw.generation);
  const jobId = raw.job_id;
  const requestId = raw.request_id ?? null;
  const message = raw.message;
  const sourceRevision = raw.source_revision ?? null;
  const containerStartedAt = raw.container_started_at ?? null;
  if (
    !PHASES.has(phase)
    || !Number.isSafeInteger(generation)
    || generation < 0
    || !(jobId === null || (typeof jobId === "string" && jobId.length > 0 && jobId.length <= 128))
    || !(requestId === null || (typeof requestId === "string" && REQUEST_ID_PATTERN.test(requestId)))
    || typeof message !== "string"
    || message.length > STATUS_MESSAGE_MAX_CHARS
    || typeof raw.retryable !== "boolean"
    || !(sourceRevision === null || (typeof sourceRevision === "string" && /^[a-f0-9]{64}$/.test(sourceRevision)))
    || !(containerStartedAt === null || (typeof containerStartedAt === "string" && containerStartedAt.length <= 128))
    || !(raw.accepted === undefined || typeof raw.accepted === "boolean")
  ) return null;
  return {
    accepted: raw.accepted as boolean | undefined,
    phase,
    generation,
    job_id: jobId,
    request_id: requestId,
    message,
    retryable: raw.retryable,
    source_revision: sourceRevision,
    container_started_at: containerStartedAt,
    before_pid: optionalPositivePid(raw.before_pid),
    after_pid: optionalPositivePid(raw.after_pid),
  };
}

async function readStatus(response: Response): Promise<AsrRestartApiStatus | null> {
  try {
    const raw = await response.text();
    return normalizeStatus(parseBoundedJson(raw, STATUS_BODY_MAX_CHARS));
  } catch {
    return null;
  }
}

async function fetchStatus(): Promise<AsrRestartApiStatus> {
  const response = await fetch("/api/runtime/asr/status", {
    cache: "no-store",
    signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
  });
  const status = await readStatus(response);
  if (!response.ok || !status) throw new Error("ASR 재시작 상태를 확인할 수 없습니다.");
  return status;
}

async function sleepWhileCurrent(delayMs: number, isCurrent: () => boolean) {
  await new Promise((resolve) => window.setTimeout(resolve, delayMs));
  if (!isCurrent()) throw new Error("ASR 재시작 확인 중 Live runtime 연결이 변경되었습니다.");
}

function newRestartRequestId(): string {
  if (typeof crypto.randomUUID !== "function") {
    throw new Error("이 브라우저는 안전한 ASR 재시작 request_id 생성을 지원하지 않습니다.");
  }
  const requestId = crypto.randomUUID();
  if (!REQUEST_ID_PATTERN.test(requestId)) {
    throw new Error("브라우저가 올바른 ASR 재시작 request_id를 생성하지 못했습니다.");
  }
  return requestId;
}

/**
 * Admit exactly one fixed ASR restart and reconcile it through idempotent GETs.
 * A missing/timeout POST response is intentionally never retried because the
 * host may already have accepted the mutation.
 */
export async function restartAsrNode(
  isCurrent: () => boolean,
  onBackendStatus: (status: AsrRestartApiStatus) => void = () => undefined,
): Promise<AsrRestartApiStatus> {
  const baseline = await fetchStatus();
  if (ACTIVE_PHASES.has(baseline.phase)) {
    throw new Error("호스트에서 다른 ASR 노드 재시작을 처리하고 있습니다. 완료 후 다시 시도하세요.");
  }
  const requestId = newRestartRequestId();

  let target: AsrRestartApiStatus | null = null;
  let postResponse: Response | null = null;
  try {
    postResponse = await fetch("/api/runtime/asr/restart", {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-Taskplanner-Request-Id": requestId,
      },
      body: "{}",
      signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
    });
  } catch {
    // The request may already have been admitted. Never repeat this uncertain
    // POST; reconcile it from the read-only status endpoint.
  }
  if (postResponse) {
    const submitted = await readStatus(postResponse);
    if (!postResponse.ok) {
      throw new Error(submitted?.message || "호스트가 ASR 노드 재시작 요청을 거부했습니다.");
    }
    if (
      submitted?.accepted === true
      && submitted.job_id
      && submitted.generation > baseline.generation
      && submitted.request_id === requestId
    ) {
      target = submitted;
      onBackendStatus(submitted);
    }
  }

  if (!target) {
    const deadline = Date.now() + RECONCILE_TIMEOUT_MS;
    while (Date.now() < deadline && !target) {
      if (!isCurrent()) throw new Error("ASR 재시작 확인 중 Live runtime 연결이 변경되었습니다.");
      try {
        const current = await fetchStatus();
        if (
          current.generation > baseline.generation
          && current.job_id
          && current.request_id === requestId
        ) {
          target = current;
          onBackendStatus(current);
        }
      } catch {
        // GET is safe to retry; the mutating POST above is never repeated.
      }
      if (!target) await sleepWhileCurrent(POLL_INTERVAL_MS, isCurrent);
    }
    if (!target) {
      throw new Error(
        "재시작 요청 응답을 확인하지 못했습니다. 중복 요청은 보내지 않았습니다. 호스트 상태를 확인한 뒤 다시 시도하세요.",
      );
    }
  }

  let completed = target;
  const deadline = Date.now() + COMPLETION_TIMEOUT_MS;
  while (completed.phase !== "succeeded" && completed.phase !== "failed") {
    if (Date.now() >= deadline) {
      throw new Error("호스트가 제한 시간 안에 ASR 노드 재시작을 완료하지 못했습니다.");
    }
    await sleepWhileCurrent(POLL_INTERVAL_MS, isCurrent);
    try {
      const current = await fetchStatus();
      if (
        current.generation === target.generation
        && current.job_id === target.job_id
        && current.request_id === requestId
      ) {
        onBackendStatus(current);
        completed = current;
      } else if (current.generation > target.generation) {
        throw new Error("요청한 ASR 재시작 작업이 다른 작업으로 대체되었습니다.");
      }
    } catch (error) {
      if (error instanceof Error && error.message.includes("다른 작업으로 대체")) throw error;
      // Transient GET failures are bounded by the overall completion deadline.
    }
  }
  if (completed.phase === "failed") {
    throw new Error(completed.message || "새 ASR 노드가 정상 상태에 도달하지 못했습니다.");
  }
  return completed;
}

type AsrRestoreControlOperation = "set_route_policy" | "start" | "start_recording";

type AsrHotRestartOptions = {
  requestedAt: number;
  previousStatus: LiveAsrStatus;
  previousStatusReceivedAt: number;
  isCurrent: () => boolean;
  getCurrentStatus: () => { status: LiveAsrStatus; receivedAt: number };
  requestControl: (
    operation: AsrRestoreControlOperation,
    deviceId: number,
    routePolicy: string,
  ) => Promise<LiveAsrControlResult>;
};

function hasLiveTypedOutputContract(status: LiveAsrStatus): boolean {
  return status.available === true
    && status.output_mode === REQUIRED_OUTPUT_MODE
    && status.output_topic === REQUIRED_OUTPUT_TOPIC
    && (!status.topic || status.topic === REQUIRED_OUTPUT_TOPIC);
}

function requireLiveTypedOutputContract(status: LiveAsrStatus): void {
  if (hasLiveTypedOutputContract(status)) return;
  throw new Error(
    "새 ASR 노드가 Live typed_utterance 출력 계약(/sensors/surgeon/utterance)을 충족하지 않습니다.",
  );
}

function hasSameRestoreSemantics(left: LiveAsrStatus, right: LiveAsrStatus): boolean {
  return left.state === right.state
    && left.connected === right.connected
    && left.route_policy === right.route_policy
    && left.device_id === right.device_id
    && left.recording_active === right.recording_active;
}

/** Confirm the new ASR owner process, then restore only fresh prior state. */
export async function hotRestartAsrNode({
  requestedAt,
  previousStatus,
  previousStatusReceivedAt,
  isCurrent,
  getCurrentStatus,
  requestControl,
}: AsrHotRestartOptions): Promise<{ status: LiveAsrStatus; message: string }> {
  const previousStatusFresh = previousStatusReceivedAt > 0
    && requestedAt - previousStatusReceivedAt <= STATUS_FRESH_MS;
  let restoreCasChanged = false;
  let restoreCasObservationOpen = previousStatusFresh;
  let legacyOldInstanceMatchOpen = !previousStatus.node_instance_id;
  let lastObservedOldStatusAt = previousStatusReceivedAt;
  const observeRestoreCas = (backendStatus: AsrRestartApiStatus) => {
    if (!restoreCasObservationOpen) return;
    const current = getCurrentStatus();
    if (current.receivedAt > lastObservedOldStatusAt) {
      const currentInstanceId = current.status.node_instance_id;
      if (!previousStatus.node_instance_id && currentInstanceId) {
        // A legacy click snapshot has no instance identity. Treat empty IDs as
        // the old process only until the first canonical UUID appears.
        legacyOldInstanceMatchOpen = false;
      } else {
        const sameOldInstance = previousStatus.node_instance_id
          ? currentInstanceId === previousStatus.node_instance_id
          : legacyOldInstanceMatchOpen && !currentInstanceId;
        if (sameOldInstance) {
          lastObservedOldStatusAt = current.receivedAt;
          if (!hasSameRestoreSemantics(previousStatus, current.status)) {
            restoreCasChanged = true;
          }
        }
      }
    }
    if (!RESTORE_CAS_OBSERVATION_PHASES.has(backendStatus.phase)) {
      restoreCasObservationOpen = false;
    }
  };
  const restoreDeviceId = previousStatus.device_id
    ?? previousStatus.devices.find((candidate) => candidate.default)?.id
    ?? previousStatus.devices[0]?.id
    ?? null;
  const completed = await restartAsrNode(isCurrent, observeRestoreCas);
  const restorationSuppressed = previousStatusFresh && restoreCasChanged;
  const restoreRoutePolicy = previousStatusFresh && !restorationSuppressed;
  const restoreCapture = restoreRoutePolicy && previousStatus.state === "LISTENING";
  const restoreRecording = restoreCapture && previousStatus.recording_active;

  const heartbeatDeadline = Date.now() + HEARTBEAT_TIMEOUT_MS;
  let restartedStatus: LiveAsrStatus | null = null;
  while (Date.now() < heartbeatDeadline) {
    if (!isCurrent()) throw new Error("ASR 재시작 확인 중 Live runtime 연결이 변경되었습니다.");
    const current = getCurrentStatus();
    const isNewInstance = Boolean(current.status.node_instance_id)
      && (!previousStatus.node_instance_id
        || current.status.node_instance_id !== previousStatus.node_instance_id);
    if (current.receivedAt >= requestedAt && isNewInstance) {
      restartedStatus = current.status;
      break;
    }
    await sleepWhileCurrent(100, isCurrent);
  }
  if (!restartedStatus) {
    throw new Error(
      "새 ASR node_instance_id heartbeat를 20초 안에 받지 못했습니다. 코드 오류나 시작 로그를 확인하세요.",
    );
  }
  requireLiveTypedOutputContract(restartedStatus);

  if (restoreRoutePolicy) {
    const routeResult = await requestControl(
      "set_route_policy",
      -1,
      previousStatus.route_policy,
    );
    if (!routeResult.accepted) {
      throw new Error(`이전 ASR 경로를 복원하지 못했습니다. ${routeResult.message}`);
    }
    const routeStatus = getCurrentStatus().status;
    requireLiveTypedOutputContract(routeStatus);
    if (
      routeStatus.node_instance_id !== restartedStatus.node_instance_id
      || routeStatus.route_policy !== previousStatus.route_policy
    ) {
      throw new Error("새 ASR 노드가 이전 경로 정책으로 복원되지 않았습니다.");
    }
    restartedStatus = routeStatus;
  }

  if (restoreCapture) {
    if (restoreDeviceId === null) {
      throw new Error("이전 마이크 장치 ID가 없어 캡처를 복원할 수 없습니다.");
    }
    if (previousStatus.route_policy === "lan" && restartedStatus.lan_health.state !== "READY") {
      const lanReadyDeadline = Date.now() + CAPTURE_TIMEOUT_MS;
      while (Date.now() < lanReadyDeadline) {
        const current = getCurrentStatus().status;
        if (
          current.node_instance_id === restartedStatus.node_instance_id
          && hasLiveTypedOutputContract(current)
          && current.route_policy === "lan"
          && current.lan_health.state === "READY"
        ) {
          restartedStatus = current;
          break;
        }
        await sleepWhileCurrent(100, isCurrent);
      }
      if (restartedStatus.lan_health.state !== "READY") {
        throw new Error("이전 LAN 전용 경로가 READY 상태로 복원되지 않아 마이크 캡처를 시작하지 않았습니다.");
      }
    }
    const startResult = await requestControl("start", restoreDeviceId, "");
    if (!startResult.accepted) {
      throw new Error(`마이크 캡처를 복원하지 못했습니다. ${startResult.message}`);
    }
    const captureDeadline = Date.now() + CAPTURE_TIMEOUT_MS;
    while (Date.now() < captureDeadline) {
      const current = getCurrentStatus().status;
      if (
        current.node_instance_id === restartedStatus.node_instance_id
        && hasLiveTypedOutputContract(current)
        && current.state === "LISTENING"
        && current.device_id === restoreDeviceId
        && current.connected
      ) {
        restartedStatus = current;
        break;
      }
      await sleepWhileCurrent(100, isCurrent);
    }
    if (
      !hasLiveTypedOutputContract(restartedStatus)
      || restartedStatus.state !== "LISTENING"
      || restartedStatus.device_id !== restoreDeviceId
      || !restartedStatus.connected
    ) {
      throw new Error("새 ASR 노드는 시작됐지만 이전 마이크 캡처가 연결된 LISTENING 상태로 복원되지 않았습니다.");
    }

    if (restoreRecording) {
      const recordingResult = await requestControl("start_recording", -1, "");
      if (!recordingResult.accepted) {
        throw new Error(`ASR 녹화를 새 세그먼트로 복원하지 못했습니다. ${recordingResult.message}`);
      }
      const recordingDeadline = Date.now() + CAPTURE_TIMEOUT_MS;
      while (Date.now() < recordingDeadline) {
        const current = getCurrentStatus().status;
        if (
          current.node_instance_id === restartedStatus.node_instance_id
          && hasLiveTypedOutputContract(current)
          && current.state === "LISTENING"
          && current.connected
          && current.recording_active
        ) {
          restartedStatus = current;
          break;
        }
        await sleepWhileCurrent(100, isCurrent);
      }
      if (!restartedStatus.recording_active) {
        throw new Error("ASR 녹화가 새 세그먼트로 복원되지 않았습니다.");
      }
    }
  }

  requireLiveTypedOutputContract(restartedStatus);

  const concurrentControlCopy = restorationSuppressed
    ? " · 재시작 대기 중 다른 ASR 제어를 감지해 이전 경로·마이크·녹음을 복원하지 않음"
    : "";
  const captureCopy = restoreCapture ? " · 마이크 캡처 복원됨" : "";
  const recordingCopy = restoreRecording ? " · 녹화는 새 세그먼트로 복원됨" : "";
  return {
    status: restartedStatus,
    message: `ASR 노드 새로 시작 완료${concurrentControlCopy}${captureCopy}${recordingCopy}`,
  };
}
