const MODES = new Set(["latest", "live", "replay"]);

const REPLAY_DELAY_MS = 750;
const LIVE_INITIAL_DELAY_MS = 150;
const LIVE_MIN_DELAY_MS = 100;
const LIVE_MAX_DELAY_MS = 500;
const RENDER_MARGIN_MS = 17;
const JITTER_ALPHA = 1 / 16;
const INTERVAL_ALPHA = 0.15;
const DECODE_LEAD_MS = 150;
const LATE_DROP_MS = 250;
const REWIND_THRESHOLD_NS = 1_000_000_000n;
const FORWARD_JUMP_MIN_MS = 5_000;
const MAX_FUTURE_MS = 4_000;

const QUEUE_LIMITS = Object.freeze({
  latest: Object.freeze({ frames: 2, bytes: 12 * 1024 * 1024, spanMs: 0 }),
  live: Object.freeze({ frames: 8, bytes: 12 * 1024 * 1024, spanMs: 1_000 }),
  replay: Object.freeze({ frames: 32, bytes: 24 * 1024 * 1024, spanMs: 3_000 }),
});

function normalizeMode(value) {
  const mode = String(value || "").trim().toLowerCase();
  return MODES.has(mode) ? mode : "live";
}

function boundedNumber(value, fallback, minimum, maximum) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.max(minimum, Math.min(maximum, number));
}

function stampBigInt(value) {
  if (typeof value === "bigint") return value >= 0n ? value : null;
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) {
    return BigInt(value);
  }
  if (typeof value === "string" && /^\d+$/.test(value.trim())) return BigInt(value.trim());
  return null;
}

function emptyMetrics() {
  return {
    invalidStamp: 0,
    duplicateStamp: 0,
    outOfOrderStamp: 0,
    lateDropped: 0,
    backlogDropped: 0,
    overflowDropped: 0,
    decodeFailed: 0,
    rewinds: 0,
    forwardJumps: 0,
    rebuffers: 0,
    fallbackPresented: 0,
    supersededDropped: 0,
  };
}

function percentile95(samples) {
  if (!samples.length) return 0;
  const sorted = [...samples].sort((left, right) => left - right);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * 0.95) - 1)];
}

function safeDispose(disposeDecoded, decoded) {
  if (decoded === null || decoded === undefined) return;
  try {
    disposeDecoded(decoded);
  } catch {
    // Resource cleanup is best-effort; playout state remains authoritative.
  }
}

function copyBytes(bytes) {
  if (bytes instanceof Uint8Array) return new Uint8Array(bytes);
  if (bytes instanceof ArrayBuffer) return new Uint8Array(bytes.slice(0));
  if (ArrayBuffer.isView(bytes)) {
    return new Uint8Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
  }
  return null;
}

function estimatedDataUrlBytes(dataUrl) {
  if (typeof dataUrl !== "string") return 0;
  const comma = dataUrl.indexOf(",");
  if (comma < 0) return 0;
  const encodedLength = dataUrl.length - comma - 1;
  return Math.max(0, Math.floor(encodedLength * 0.75));
}

function inputPayloadSize(input) {
  const bytes = input?.bytes;
  if (bytes instanceof ArrayBuffer) return bytes.byteLength;
  if (ArrayBuffer.isView(bytes)) return bytes.byteLength;
  return estimatedDataUrlBytes(input?.dataUrl);
}

export function cameraTimingStampNs(timing) {
  if (!timing || typeof timing !== "object") return null;
  const seconds = Number(timing.seconds);
  const nanoseconds = Number(timing.nanoseconds);
  if (
    !Number.isSafeInteger(seconds)
    || seconds < 0
    || !Number.isSafeInteger(nanoseconds)
    || nanoseconds < 0
    || nanoseconds >= 1_000_000_000
  ) {
    return null;
  }
  return BigInt(seconds) * 1_000_000_000n + BigInt(nanoseconds);
}

export function createCameraPlayout({
  mode = "live",
  throttleMs = 100,
  now = () => performance.now(),
  requestFrame = (callback) => requestAnimationFrame(callback),
  cancelFrame = (id) => cancelAnimationFrame(id),
  decode = async (frame) => frame,
  present = () => {},
  disposeDecoded = (decoded) => decoded?.close?.(),
  onEvent = () => {},
} = {}) {
  const normalizedMode = normalizeMode(mode);
  const normalizedThrottleMs = boundedNumber(throttleMs, 100, 100, 5000);
  const initialDelayMs = normalizedMode === "latest"
    ? 0
    : normalizedMode === "replay"
      ? REPLAY_DELAY_MS
      : LIVE_INITIAL_DELAY_MS;
  let destroyed = false;
  let frameRequest = null;

  function initialState(previous = null, { preserveTiming = false, clearPresented = false } = {}) {
    const state = {
      mode: normalizedMode,
      generation: (previous?.generation ?? -1) + 1,
      phase: "waiting",
      sourceClock: "waiting",
      anchorStampNs: null,
      anchorMono: null,
      queue: [],
      queueBytes: 0,
      latestPending: null,
      decoding: null,
      lastStampNs: null,
      lastArrivalStampNs: null,
      lastArrivalMono: null,
      lastAcceptedAt: 0,
      lastAcceptedMono: null,
      lastPresentedStampNs: clearPresented ? null : previous?.lastPresentedStampNs ?? null,
      lastPresentedMono: clearPresented ? null : previous?.lastPresentedMono ?? null,
      presentedCount: clearPresented ? 0 : previous?.presentedCount ?? 0,
      intervalMs: null,
      jitterMs: 0,
      decodeSamplesMs: [],
      targetDelayMs: initialDelayMs,
      currentDelayMs: initialDelayMs,
      nextEligibleMono: null,
      metrics: previous ? { ...previous.metrics } : emptyMetrics(),
    };
    if (preserveTiming && previous) {
      state.intervalMs = previous.intervalMs;
    }
    if (preserveTiming && previous?.mode === "live") {
      state.jitterMs = previous.jitterMs;
      state.decodeSamplesMs = [...previous.decodeSamplesMs];
      state.targetDelayMs = previous.targetDelayMs;
      state.currentDelayMs = previous.currentDelayMs;
    }
    return state;
  }

  let state = initialState();

  function emit(type, details = {}) {
    try {
      onEvent({ type, ...details });
    } catch {
      // Diagnostics must never interrupt video playout.
    }
  }

  function discardFrame(frame) {
    if (!frame || frame.discarded) return;
    frame.discarded = true;
    if (frame.decoded !== null && frame.decoded !== undefined) {
      safeDispose(disposeDecoded, frame.decoded);
      frame.decoded = null;
    }
  }

  function cancelScheduledFrame() {
    if (frameRequest === null) return;
    try {
      cancelFrame(frameRequest);
    } catch {
      // A callback that is already executing no longer needs cancellation.
    }
    frameRequest = null;
  }

  function replaceState({ preserveTiming = false, clearPresented = false } = {}) {
    cancelScheduledFrame();
    const previous = state;
    for (const frame of previous.queue) discardFrame(frame);
    discardFrame(previous.latestPending);
    if (previous.decoding && !previous.queue.includes(previous.decoding)) {
      discardFrame(previous.decoding);
    }
    state = initialState(previous, { preserveTiming, clearPresented });
    return state;
  }

  function reset({ reason = "reset", clearPresented = false } = {}) {
    replaceState({ clearPresented });
    emit("playout.reset", { reason, generation: state.generation });
    return snapshot();
  }

  function startEpoch(stampNs, nowMono, reason) {
    const preserveTiming = reason === "underflow";
    const next = replaceState({ preserveTiming });
    if (reason === "rewind") next.metrics.rewinds += 1;
    if (reason === "forward_jump") next.metrics.forwardJumps += 1;
    if (reason === "underflow" || reason === "late_decode") next.metrics.rebuffers += 1;
    if (preserveTiming && next.mode === "live") next.currentDelayMs = next.targetDelayMs;
    next.anchorStampNs = stampNs;
    next.anchorMono = nowMono + next.currentDelayMs;
    next.phase = "buffering";
    next.sourceClock = "ok";
    emit("clock.reanchored", {
      reason,
      stampNs: stampNs.toString(),
      delayMs: next.currentDelayMs,
      generation: next.generation,
    });
    return next;
  }

  function frameDeadline(stampNs) {
    if (state.anchorStampNs === null || state.anchorMono === null) return null;
    return state.anchorMono + Number(stampNs - state.anchorStampNs) / 1_000_000;
  }

  function removeQueuedFrame(index, metric = "") {
    const [frame] = state.queue.splice(index, 1);
    if (!frame) return null;
    state.queueBytes = Math.max(0, state.queueBytes - frame.sizeBytes);
    if (state.decoding === frame) state.decoding = null;
    discardFrame(frame);
    if (metric) state.metrics[metric] += 1;
    if (metric) emit("frame.dropped", {
      reason: metric === "supersededDropped" ? "superseded" : metric,
      stampNs: frame.stampNs?.toString() ?? null,
    });
    return frame;
  }

  function boundQueue() {
    const limits = QUEUE_LIMITS[state.mode];
    while (state.queue.length) {
      const spanMs = state.queue.length > 1
        ? Number(state.queue.at(-1).stampNs - state.queue[0].stampNs) / 1_000_000
        : 0;
      if (
        state.queue.length <= limits.frames
        && state.queueBytes <= limits.bytes
        && spanMs <= limits.spanMs
      ) {
        return;
      }
      removeQueuedFrame(0, "overflowDropped");
    }
  }

  function insertFrame(frame) {
    let index = state.queue.length;
    while (index > 0 && state.queue[index - 1].stampNs > frame.stampNs) index -= 1;
    state.queue.splice(index, 0, frame);
    state.queueBytes += frame.sizeBytes;
    boundQueue();
    return state.queue.includes(frame);
  }

  function decodeP95() {
    return percentile95(state.decodeSamplesMs);
  }

  function updateLiveDelay(nowMono) {
    if (state.mode !== "live") return;
    state.targetDelayMs = boundedNumber(
      4 * state.jitterMs + decodeP95() + RENDER_MARGIN_MS,
      LIVE_INITIAL_DELAY_MS,
      LIVE_MIN_DELAY_MS,
      LIVE_MAX_DELAY_MS,
    );
    if (state.targetDelayMs <= state.currentDelayMs || state.anchorMono === null) return;
    const deltaMs = state.targetDelayMs - state.currentDelayMs;
    state.anchorMono += deltaMs;
    for (const frame of state.queue) frame.deadlineMono += deltaMs;
    state.currentDelayMs = state.targetDelayMs;
    emit("playout.delay_increased", { delayMs: state.currentDelayMs, atMono: nowMono });
  }

  function observeCameraTiming(stampNs, arrivalMono) {
    if (
      state.lastArrivalStampNs === null
      || state.lastArrivalMono === null
    ) {
      return;
    }
    const sourceDeltaMs = Number(stampNs - state.lastArrivalStampNs) / 1_000_000;
    const arrivalDeltaMs = arrivalMono - state.lastArrivalMono;
    if (sourceDeltaMs <= 0 || arrivalDeltaMs <= 0) return;
    state.intervalMs = state.intervalMs === null
      ? sourceDeltaMs
      : state.intervalMs * (1 - INTERVAL_ALPHA) + sourceDeltaMs * INTERVAL_ALPHA;
    if (state.mode !== "live") return;
    const transitVariationMs = Math.abs(arrivalDeltaMs - sourceDeltaMs);
    state.jitterMs += (transitVariationMs - state.jitterMs) * JITTER_ALPHA;
    updateLiveDelay(arrivalMono);
  }

  function forwardJump(sourceDeltaMs, arrivalDeltaMs) {
    const thresholdMs = Math.max(
      FORWARD_JUMP_MIN_MS,
      10 * (state.intervalMs || normalizedThrottleMs),
    );
    return sourceDeltaMs > thresholdMs
      && sourceDeltaMs - Math.max(0, arrivalDeltaMs) > thresholdMs;
  }

  function hasTimelyFrame(nowMono) {
    return state.queue.some((frame) => frame.deadlineMono >= nowMono - LATE_DROP_MS);
  }

  function acceptedFrame(input, stampNs, nowMono) {
    const bytes = copyBytes(input.bytes);
    const dataUrl = typeof input.dataUrl === "string" && input.dataUrl ? input.dataUrl : null;
    const sizeBytes = bytes?.byteLength || estimatedDataUrlBytes(dataUrl) || 0;
    return {
      stampNs,
      sourceTimestampMs: Number.isFinite(Number(input.sourceTimestampMs))
        ? Number(input.sourceTimestampMs)
        : null,
      mimeType: String(input.mimeType || "image/jpeg"),
      dataUrl,
      bytes,
      frameId: String(input.frameId || "").slice(0, 128),
      receivedAt: Number.isFinite(Number(input.receivedAt)) ? Number(input.receivedAt) : Date.now(),
      receivedMono: nowMono,
      sizeBytes,
      deadlineMono: null,
      decoded: null,
      decodeState: null,
      discarded: false,
    };
  }

  function recordAccepted(frame) {
    state.lastAcceptedAt = frame.receivedAt;
    state.lastAcceptedMono = frame.receivedMono;
    if (frame.stampNs !== null) {
      state.lastStampNs = frame.stampNs;
      state.lastArrivalStampNs = frame.stampNs;
      state.lastArrivalMono = frame.receivedMono;
      state.sourceClock = "ok";
    }
  }

  function recordLatestAccepted(frame) {
    if (state.lastAcceptedMono !== null && frame.receivedMono > state.lastAcceptedMono) {
      const arrivalDeltaMs = frame.receivedMono - state.lastAcceptedMono;
      state.intervalMs = state.intervalMs === null
        ? arrivalDeltaMs
        : state.intervalMs * (1 - INTERVAL_ALPHA) + arrivalDeltaMs * INTERVAL_ALPHA;
    }
    state.lastAcceptedAt = frame.receivedAt;
    state.lastAcceptedMono = frame.receivedMono;
    state.lastStampNs = frame.stampNs;
    state.lastArrivalStampNs = frame.stampNs;
    state.lastArrivalMono = frame.receivedMono;
    state.sourceClock = "arrival";
    state.phase = "playing";
  }

  function replaceLatestPending(frame) {
    if (state.latestPending) {
      const previous = state.latestPending;
      state.latestPending = null;
      discardFrame(previous);
      state.metrics.supersededDropped += 1;
      emit("frame.dropped", {
        reason: "superseded",
        stampNs: previous.stampNs?.toString() ?? null,
      });
    }
    state.latestPending = frame;
  }

  function commitLatestFrame(frame, nowMono) {
    frame.deadlineMono = state.nextEligibleMono === null
      ? nowMono
      : Math.max(nowMono, state.nextEligibleMono);
    state.queue.push(frame);
    state.queueBytes += frame.sizeBytes;
  }

  function promoteLatestPending(nowMono) {
    if (state.queue.length || !state.latestPending) return null;
    const frame = state.latestPending;
    state.latestPending = null;
    commitLatestFrame(frame, nowMono);
    return frame;
  }

  function enqueueLatestFrame(frame, nowMono) {
    const committed = state.queue[0] || null;
    if (!committed) {
      commitLatestFrame(frame, nowMono);
      return "queued";
    }
    if (!committed.decodeState && state.decoding !== committed) {
      removeQueuedFrame(0, "supersededDropped");
      commitLatestFrame(frame, nowMono);
      return "superseded";
    }
    const replaced = Boolean(state.latestPending);
    replaceLatestPending(frame);
    return replaced ? "superseded" : "queued";
  }

  function presentLatestFrame(frame, nowMono) {
    if (
      state.mode !== "latest"
      || state.queue[0] !== frame
      || frame.deadlineMono > nowMono
      || frame.decodeState !== "ready"
    ) {
      return false;
    }
    state.queue.shift();
    state.queueBytes = Math.max(0, state.queueBytes - frame.sizeBytes);
    const presented = presentFrame(frame, nowMono, "latest");
    if (presented) state.nextEligibleMono = nowMono + normalizedThrottleMs;
    promoteLatestPending(nowMono);
    return presented;
  }

  function startDecode(frame, immediate = false) {
    if (!frame || frame.discarded || frame.decodeState) return;
    const generation = state.generation;
    const decodeStartedMono = now();
    frame.decodeState = "pending";
    if (!immediate) state.decoding = frame;
    let decodeOperation;
    try {
      decodeOperation = Promise.resolve(decode(frame));
    } catch (error) {
      decodeOperation = Promise.reject(error);
    }
    decodeOperation
      .then((decoded) => {
        if (destroyed || generation !== state.generation || frame.discarded) {
          safeDispose(disposeDecoded, decoded);
          return;
        }
        const durationMs = Math.max(0, now() - decodeStartedMono);
        state.decodeSamplesMs.push(durationMs);
        if (state.decodeSamplesMs.length > 30) state.decodeSamplesMs.shift();
        frame.decoded = decoded;
        frame.decodeState = "ready";
        updateLiveDelay(now());
        if (immediate) {
          presentFrame(frame, now(), "latest-only");
        } else if (state.mode === "latest") {
          presentLatestFrame(frame, now());
        }
      })
      .catch((error) => {
        if (generation !== state.generation || frame.discarded) return;
        frame.decodeState = "failed";
        state.metrics.decodeFailed += 1;
        emit("decode.failed", {
          reason: error instanceof Error ? error.message : "decode_failed",
          stampNs: frame.stampNs?.toString() ?? null,
        });
      })
      .finally(() => {
        if (!immediate && generation === state.generation && state.decoding === frame) {
          state.decoding = null;
        }
        if (!destroyed && generation === state.generation) ensureFrame();
      });
  }

  function presentFrame(frame, nowMono, presentationMode = "timed") {
    if (!frame || frame.discarded || frame.decodeState !== "ready") return false;
    let presented = false;
    try {
      presented = present(frame, frame.decoded) !== false;
    } catch (error) {
      emit("present.failed", {
        reason: error instanceof Error ? error.message : "present_failed",
        stampNs: frame.stampNs?.toString() ?? null,
      });
    }
    if (!presented) {
      safeDispose(disposeDecoded, frame.decoded);
      frame.decoded = null;
      frame.discarded = true;
      return false;
    }
    // Successful presenters own the prepared resource until the next visible
    // frame or an explicit camera reset. Dropped/stale preparations remain the
    // controller's responsibility and are disposed above.
    frame.decoded = null;
    frame.discarded = true;
    state.lastPresentedMono = nowMono;
    state.lastPresentedStampNs = frame.stampNs;
    state.presentedCount += 1;
    if (presentationMode === "latest-only") state.metrics.fallbackPresented += 1;
    emit("frame.presented", {
      mode: presentationMode,
      stampNs: frame.stampNs?.toString() ?? null,
      receivedAt: frame.receivedAt,
    });
    return true;
  }

  function ingestLatest(input, stampNs, nowMono) {
    const payloadSize = inputPayloadSize(input);
    if (!payloadSize) {
      emit("frame.dropped", { reason: "invalid_payload", stampNs: stampNs?.toString() ?? null });
      return { accepted: false, mode: "latest", reason: "invalid_payload" };
    }
    if (payloadSize > QUEUE_LIMITS.latest.bytes) {
      state.metrics.overflowDropped += 1;
      emit("frame.dropped", { reason: "overflowDropped", stampNs: stampNs?.toString() ?? null });
      return { accepted: false, mode: "latest", reason: "queue_overflow" };
    }

    const frame = acceptedFrame(input, stampNs, nowMono);
    recordLatestAccepted(frame);
    const reason = enqueueLatestFrame(frame, nowMono);
    prepareDecode(nowMono);
    ensureFrame();
    emit("frame.queued", {
      reason,
      stampNs: stampNs?.toString() ?? null,
      deadlineMono: frame.deadlineMono ?? state.nextEligibleMono ?? nowMono,
      queuedFrames: state.queue.length + (state.latestPending ? 1 : 0),
    });
    return { accepted: true, mode: "latest", reason };
  }

  function ingestLatestOnly(input, nowMono) {
    const payloadSize = inputPayloadSize(input);
    if (!payloadSize) {
      state.metrics.invalidStamp += 1;
      emit("frame.dropped", { reason: "invalid_payload", stampNs: null });
      return { accepted: false, mode: "latest-only", reason: "invalid_payload" };
    }
    if (payloadSize > QUEUE_LIMITS[state.mode].bytes) {
      state.metrics.overflowDropped += 1;
      emit("frame.dropped", { reason: "overflowDropped", stampNs: null });
      return { accepted: false, mode: "latest-only", reason: "queue_overflow" };
    }
    const frame = acceptedFrame(input, null, nowMono);
    replaceState();
    state.metrics.invalidStamp += 1;
    state.phase = "latest-only";
    state.sourceClock = "fallback";
    recordAccepted(frame);
    emit("clock.fallback", { reason: "missing_stamp", generation: state.generation });
    startDecode(frame, true);
    return { accepted: true, mode: "latest-only", reason: "missing_stamp" };
  }

  function ingest(input = {}) {
    if (destroyed) {
      return {
        accepted: false,
        mode: normalizedMode === "latest" ? "latest" : "timed",
        reason: "destroyed",
      };
    }
    const nowMono = now();
    const parsedStampNs = stampBigInt(input.stampNs);
    if (normalizedMode === "latest") return ingestLatest(input, parsedStampNs, nowMono);
    if (parsedStampNs === null) return ingestLatestOnly(input, nowMono);

    const payloadSize = inputPayloadSize(input);
    if (!payloadSize) {
      emit("frame.dropped", { reason: "invalid_payload", stampNs: parsedStampNs.toString() });
      return { accepted: false, mode: "timed", reason: "invalid_payload" };
    }
    if (payloadSize > QUEUE_LIMITS[state.mode].bytes) {
      state.metrics.overflowDropped += 1;
      emit("frame.dropped", { reason: "overflowDropped", stampNs: parsedStampNs.toString() });
      return { accepted: false, mode: "timed", reason: "queue_overflow" };
    }
    let reason = "queued";
    let restartReason = "";
    if (state.lastStampNs !== null) {
      const stampDeltaNs = parsedStampNs - state.lastStampNs;
      if (stampDeltaNs === 0n) {
        state.metrics.duplicateStamp += 1;
        emit("frame.dropped", { reason: "duplicate_stamp", stampNs: parsedStampNs.toString() });
        return { accepted: false, mode: "timed", reason: "duplicate_stamp" };
      }
      if (stampDeltaNs < 0n) {
        if (-stampDeltaNs >= REWIND_THRESHOLD_NS) {
          restartReason = "rewind";
          reason = "rewind";
        } else {
          state.metrics.outOfOrderStamp += 1;
          emit("frame.dropped", { reason: "out_of_order_stamp", stampNs: parsedStampNs.toString() });
          return { accepted: false, mode: "timed", reason: "out_of_order_stamp" };
        }
      } else {
        const sourceDeltaMs = Number(stampDeltaNs) / 1_000_000;
        const arrivalDeltaMs = state.lastArrivalMono === null ? 0 : nowMono - state.lastArrivalMono;
        if (forwardJump(sourceDeltaMs, arrivalDeltaMs)) {
          restartReason = "forward_jump";
          reason = "forward_jump";
        }
      }
    }

    if (restartReason) {
      startEpoch(parsedStampNs, nowMono, restartReason);
    } else {
      observeCameraTiming(parsedStampNs, nowMono);
      if (state.anchorStampNs === null || state.sourceClock === "fallback") {
        startEpoch(parsedStampNs, nowMono, "initial");
      } else if (state.phase === "underflow") {
        startEpoch(parsedStampNs, nowMono, "underflow");
      }
    }

    let frame = acceptedFrame(input, parsedStampNs, nowMono);
    let deadlineMono = frameDeadline(parsedStampNs);
    if (deadlineMono === null || deadlineMono > nowMono + MAX_FUTURE_MS) {
      startEpoch(parsedStampNs, nowMono, "forward_jump");
      reason = "forward_jump";
      frame = acceptedFrame(input, parsedStampNs, nowMono);
      deadlineMono = frameDeadline(parsedStampNs);
    }
    if (deadlineMono < nowMono - LATE_DROP_MS && !hasTimelyFrame(nowMono)) {
      startEpoch(parsedStampNs, nowMono, "underflow");
      frame = acceptedFrame(input, parsedStampNs, nowMono);
      deadlineMono = frameDeadline(parsedStampNs);
    }
    frame.deadlineMono = deadlineMono;
    if (!insertFrame(frame)) {
      return { accepted: false, mode: "timed", reason: "queue_overflow" };
    }
    recordAccepted(frame);
    ensureFrame();
    emit("frame.queued", {
      reason,
      stampNs: parsedStampNs.toString(),
      deadlineMono,
      queuedFrames: state.queue.length,
    });
    return { accepted: true, mode: "timed", reason };
  }

  function prepareDecode(nowMono) {
    if (state.decoding) return;
    const frame = state.queue.find((candidate) => (
      !candidate.discarded
      && !candidate.decodeState
      && candidate.deadlineMono <= nowMono + DECODE_LEAD_MS
    ));
    if (frame) startDecode(frame);
  }

  function underflowGraceMs() {
    return boundedNumber(
      3 * (state.intervalMs || normalizedThrottleMs),
      1_000,
      1_000,
      10_000,
    );
  }

  function runLatestTick(nowMono) {
    promoteLatestPending(nowMono);
    let frame = state.queue[0] || null;
    if (!frame) return;

    // A backgrounded tab may resume long after the committed frame was due.
    // Prefer the most recent waiting frame instead of briefly flashing the
    // obsolete one, while keeping normal in-flight decodes starvation-free.
    if (
      state.latestPending
      && frame.deadlineMono <= nowMono - normalizedThrottleMs
    ) {
      removeQueuedFrame(0, "supersededDropped");
      promoteLatestPending(nowMono);
      frame = state.queue[0] || null;
      if (!frame) return;
    }

    if (frame.deadlineMono > nowMono) {
      prepareDecode(nowMono);
      ensureFrame();
      return;
    }

    if (frame.decodeState === "failed") {
      removeQueuedFrame(0);
      promoteLatestPending(nowMono);
    } else if (frame.decoded) {
      presentLatestFrame(frame, nowMono);
    } else {
      startDecode(frame);
    }

    prepareDecode(nowMono);
    if (state.queue.length || state.latestPending || state.decoding !== null) ensureFrame();
  }

  function runTick(nowMono) {
    if (destroyed) return;
    if (state.mode === "latest") {
      runLatestTick(nowMono);
      return;
    }
    updateLiveDelay(nowMono);
    if (state.anchorMono === null) return;

    if (nowMono < state.anchorMono) {
      prepareDecode(nowMono);
      ensureFrame();
      return;
    }
    if (state.phase === "buffering") state.phase = "playing";

    let dueCount = 0;
    while (dueCount < state.queue.length && state.queue[dueCount].deadlineMono <= nowMono) {
      dueCount += 1;
    }
    while (dueCount > 1) {
      removeQueuedFrame(0, "backlogDropped");
      dueCount -= 1;
    }

    const due = dueCount === 1 ? state.queue[0] : null;
    if (due?.decodeState === "failed") {
      removeQueuedFrame(0);
    } else if (due?.decoded) {
      state.queue.shift();
      state.queueBytes = Math.max(0, state.queueBytes - due.sizeBytes);
      const latenessMs = nowMono - due.deadlineMono;
      const hasTimelyFuture = state.queue.some((frame) => frame.deadlineMono >= nowMono - LATE_DROP_MS);
      if (latenessMs > LATE_DROP_MS && hasTimelyFuture) {
        state.metrics.lateDropped += 1;
        discardFrame(due);
        emit("frame.dropped", { reason: "lateDropped", stampNs: due.stampNs.toString() });
      } else {
        presentFrame(due, nowMono);
        if (latenessMs > LATE_DROP_MS && !hasTimelyFuture) {
          // A slow decoder or a slow source must not enter a discard/redecode
          // loop. Present it once, then move the source clock to the actual
          // presentation point so following frames continue at 1x.
          state.anchorStampNs = due.stampNs;
          state.anchorMono = nowMono;
          state.metrics.rebuffers += 1;
          emit("clock.reanchored", {
            reason: "late_decode",
            stampNs: due.stampNs.toString(),
            generation: state.generation,
          });
        }
      }
    } else if (due) {
      // Pending decodes are held instead of being discarded after 250 ms. A
      // completed slow decode is presented once and re-anchors above.
      startDecode(due);
    }

    prepareDecode(nowMono);
    const hasWork = state.queue.length > 0 || state.decoding !== null;
    if (
      state.phase === "playing"
      && !hasWork
      && state.lastAcceptedMono !== null
      && nowMono - state.lastAcceptedMono > underflowGraceMs()
    ) {
      state.phase = "underflow";
      emit("playout.underflow", { atMono: nowMono });
    }
    if (hasWork || state.phase === "buffering" || (
      state.phase === "playing"
      && state.lastAcceptedMono !== null
      && nowMono - state.lastAcceptedMono <= underflowGraceMs()
    )) {
      ensureFrame();
    }
  }

  function scheduledTick(timestamp) {
    frameRequest = null;
    runTick(Number.isFinite(Number(timestamp)) ? Number(timestamp) : now());
  }

  function ensureFrame() {
    if (destroyed || frameRequest !== null) return;
    frameRequest = requestFrame(scheduledTick);
  }

  function tick(timestamp = now()) {
    cancelScheduledFrame();
    runTick(Number(timestamp));
  }

  function snapshot() {
    const latestPendingBytes = state.latestPending?.sizeBytes || 0;
    return Object.freeze({
      mode: state.mode,
      phase: state.phase,
      generation: state.generation,
      sourceClock: state.sourceClock,
      anchorStampNs: state.anchorStampNs?.toString() ?? null,
      queuedFrames: state.queue.length + (state.latestPending ? 1 : 0),
      queuedBytes: state.queueBytes + latestPendingBytes,
      lastStampNs: state.lastStampNs?.toString() ?? null,
      lastAcceptedAt: state.lastAcceptedAt,
      lastPresentedStampNs: state.lastPresentedStampNs?.toString() ?? null,
      presentedCount: state.presentedCount,
      intervalMs: state.intervalMs,
      jitterMs: state.jitterMs,
      decodeP95Ms: decodeP95(),
      targetDelayMs: state.targetDelayMs,
      currentDelayMs: state.currentDelayMs,
      cadenceMs: state.mode === "latest" ? normalizedThrottleMs : null,
      nextEligibleMono: state.mode === "latest" ? state.nextEligibleMono : null,
      metrics: Object.freeze({ ...state.metrics }),
    });
  }

  function destroy() {
    if (destroyed) return;
    reset({ reason: "destroy", clearPresented: true });
    destroyed = true;
    cancelScheduledFrame();
  }

  return Object.freeze({ ingest, tick, reset, snapshot, destroy });
}
