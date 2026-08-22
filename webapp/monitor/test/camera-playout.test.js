import test from "node:test";
import assert from "node:assert/strict";

import { createCameraPlayout } from "../ros/camera-playout.js";

const NS_PER_MS = 1_000_000n;

function timedFrame(stampMs, {
  frameId = `frame-${stampMs}`,
  receivedAt = stampMs,
  size = 4,
} = {}) {
  return {
    stampNs: BigInt(stampMs) * NS_PER_MS,
    sourceTimestampMs: stampMs,
    mimeType: "image/jpeg",
    dataUrl: null,
    bytes: new Uint8Array(size).fill(Number(stampMs) & 0xff),
    frameId,
    receivedAt,
  };
}

function missingStampFrame(frameId, receivedAt, size = 4) {
  return {
    stampNs: null,
    sourceTimestampMs: null,
    mimeType: "image/jpeg",
    dataUrl: null,
    bytes: new Uint8Array(size).fill(receivedAt & 0xff),
    frameId,
    receivedAt,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
}

function createHarness({
  mode = "replay",
  throttleMs = 500,
  decode,
} = {}) {
  let mono = 0;
  let frameRequestId = 0;
  const requestedFrames = new Map();
  const cancelledFrames = [];
  const decodedDisposed = [];
  const presented = [];
  const events = [];
  const decodeCalls = [];

  const decodeImpl = decode || (async (frame) => ({
    frameId: frame.frameId,
    width: 2,
    height: 2,
  }));

  const playout = createCameraPlayout({
    mode,
    throttleMs,
    now: () => mono,
    requestFrame(callback) {
      const id = ++frameRequestId;
      requestedFrames.set(id, callback);
      return id;
    },
    cancelFrame(id) {
      requestedFrames.delete(id);
      cancelledFrames.push(id);
    },
    decode(frame) {
      decodeCalls.push(frame);
      return decodeImpl(frame);
    },
    present(frame, decoded) {
      presented.push({ frame, decoded, at: mono });
    },
    disposeDecoded(decoded) {
      decodedDisposed.push(decoded);
    },
    onEvent(event) {
      events.push(event);
    },
  });

  return {
    playout,
    presented,
    events,
    decodeCalls,
    decodedDisposed,
    requestedFrames,
    cancelledFrames,
    now: () => mono,
    setNow(value) {
      mono = value;
    },
    async tick(value = mono) {
      mono = value;
      playout.tick(mono);
      await flushPromises();
    },
  };
}

test("replay waits 750 ms while live starts after its 150 ms initial delay", async () => {
  const replay = createHarness({ mode: "replay" });
  replay.setNow(1_000);
  assert.deepEqual(replay.playout.ingest(timedFrame(0, { receivedAt: 10_000 })), {
    accepted: true,
    mode: "timed",
    reason: "queued",
  });
  await replay.tick(1_600); // decode lead
  await replay.tick(1_749);
  assert.equal(replay.presented.length, 0);
  await replay.tick(1_750);
  assert.equal(replay.presented.length, 1);
  assert.equal(replay.presented[0].frame.stampNs, 0n);

  const live = createHarness({ mode: "live" });
  live.setNow(2_000);
  live.playout.ingest(timedFrame(0, { receivedAt: 20_000 }));
  await live.tick(2_000); // the 150 ms decode lead includes the first frame
  await live.tick(2_149);
  assert.equal(live.presented.length, 0);
  await live.tick(2_150);
  assert.equal(live.presented.length, 1);
  assert.equal(live.playout.snapshot().mode, "live");
});

test("a fast burst is source-paced and only the latest due frame is presented", async () => {
  const harness = createHarness({ mode: "replay" });
  for (const stampMs of [0, 500, 1_000, 1_500]) {
    assert.equal(harness.playout.ingest(timedFrame(stampMs)).accepted, true);
  }

  assert.equal(harness.presented.length, 0, "arrival must not present a timed frame");
  await harness.tick(2_250);
  await harness.tick(2_250);

  assert.deepEqual(harness.presented.map(({ frame }) => frame.stampNs), [1_500n * NS_PER_MS]);
  const snapshot = harness.playout.snapshot();
  assert.equal(snapshot.metrics.backlogDropped, 3);
  assert.equal(snapshot.presentedCount, 1);
  assert.equal(snapshot.lastPresentedStampNs, (1_500n * NS_PER_MS).toString());
});

test("a slow arrival holds the last frame and resumes after one replay buffer", async () => {
  const harness = createHarness({ mode: "replay" });
  harness.playout.ingest(timedFrame(0, { receivedAt: 1_000 }));
  await harness.tick(600);
  await harness.tick(750);
  assert.equal(harness.presented.length, 1);

  harness.setNow(2_000);
  const result = harness.playout.ingest(timedFrame(500, { receivedAt: 2_000 }));
  assert.equal(result.accepted, true);
  assert.equal(harness.presented.length, 1, "the previous canvas must remain while buffering");

  await harness.tick(2_600);
  await harness.tick(2_749);
  assert.equal(harness.presented.length, 1);
  await harness.tick(2_750);
  assert.equal(harness.presented.length, 2);
  assert.equal(harness.presented[1].frame.stampNs, 500n * NS_PER_MS);
  assert.equal(harness.playout.snapshot().metrics.rebuffers, 1);
});

test("duplicate and small reverse stamps are rejected without advancing accepted freshness", () => {
  const harness = createHarness();
  harness.setNow(10);
  harness.playout.ingest(timedFrame(5_000, { receivedAt: 100 }));
  assert.equal(harness.playout.snapshot().lastAcceptedAt, 100);

  harness.setNow(20);
  assert.deepEqual(harness.playout.ingest(timedFrame(5_000, { receivedAt: 200 })), {
    accepted: false,
    mode: "timed",
    reason: "duplicate_stamp",
  });
  assert.equal(harness.playout.snapshot().lastAcceptedAt, 100);

  harness.setNow(30);
  assert.deepEqual(harness.playout.ingest({
    ...timedFrame(4_001, { receivedAt: 300 }),
    stampNs: 4_000_000_001n,
    sourceTimestampMs: 4_000.000001,
  }), {
    accepted: false,
    mode: "timed",
    reason: "out_of_order_stamp",
  });
  assert.equal(harness.playout.snapshot().lastAcceptedAt, 100);
  assert.equal(harness.playout.snapshot().queuedFrames, 1);
});

test("invalid payloads do not advance accepted freshness or the source clock", () => {
  const harness = createHarness();
  harness.playout.ingest(timedFrame(1_000, { receivedAt: 100 }));
  const accepted = harness.playout.snapshot();

  harness.setNow(50);
  assert.deepEqual(harness.playout.ingest({
    ...timedFrame(1_500, { receivedAt: 200 }),
    bytes: new Uint8Array(),
  }), {
    accepted: false,
    mode: "timed",
    reason: "invalid_payload",
  });
  assert.deepEqual(harness.playout.ingest({
    ...missingStampFrame("missing-invalid", 300),
    bytes: new Uint8Array(),
  }), {
    accepted: false,
    mode: "latest-only",
    reason: "invalid_payload",
  });

  const snapshot = harness.playout.snapshot();
  assert.equal(snapshot.lastAcceptedAt, accepted.lastAcceptedAt);
  assert.equal(snapshot.lastStampNs, accepted.lastStampNs);
  assert.equal(snapshot.generation, accepted.generation);
  assert.equal(snapshot.queuedFrames, accepted.queuedFrames);
});

test("a frame larger than the bounded live queue is rejected without advancing freshness", () => {
  const harness = createHarness({ mode: "live" });
  harness.playout.ingest(timedFrame(1_000, { receivedAt: 100 }));
  const accepted = harness.playout.snapshot();
  const oversizedBytes = new Uint8Array(12 * 1024 * 1024 + 1);

  const result = harness.playout.ingest({
    ...timedFrame(1_500, { receivedAt: 200 }),
    bytes: oversizedBytes,
  });
  assert.deepEqual(result, {
    accepted: false,
    mode: "timed",
    reason: "queue_overflow",
  });
  const snapshot = harness.playout.snapshot();
  assert.equal(snapshot.lastAcceptedAt, accepted.lastAcceptedAt);
  assert.equal(snapshot.lastStampNs, accepted.lastStampNs);
  assert.equal(snapshot.metrics.overflowDropped, accepted.metrics.overflowDropped + 1);

  const rewindResult = harness.playout.ingest({
    ...timedFrame(0, { receivedAt: 300 }),
    bytes: oversizedBytes,
  });
  assert.equal(rewindResult.reason, "queue_overflow");
  const afterRewind = harness.playout.snapshot();
  assert.equal(afterRewind.generation, snapshot.generation);
  assert.equal(afterRewind.anchorStampNs, snapshot.anchorStampNs);
  assert.equal(afterRewind.queuedFrames, snapshot.queuedFrames);
});

test("replay learns source cadence and preserves it when a slow source re-buffers", async () => {
  const harness = createHarness({ mode: "replay", throttleMs: 500 });
  harness.playout.ingest(timedFrame(0, { receivedAt: 0 }));
  await harness.tick(600);
  await harness.tick(750);
  await harness.tick(1_501);
  assert.equal(harness.playout.snapshot().phase, "underflow");

  harness.setNow(5_000);
  harness.playout.ingest(timedFrame(5_000, { receivedAt: 5_000 }));
  assert.equal(harness.playout.snapshot().intervalMs, 5_000);
  assert.equal(harness.playout.snapshot().metrics.rebuffers, 1);
});

test("dropping a due frame releases the decode slot so the latest frame is not blocked", async () => {
  const pending = new Map();
  const harness = createHarness({
    mode: "replay",
    decode(frame) {
      const operation = deferred();
      pending.set(frame.frameId, operation);
      return operation.promise;
    },
  });
  for (const stampMs of [0, 500, 1_000]) {
    harness.playout.ingest(timedFrame(stampMs));
  }

  await harness.tick(600);
  assert.deepEqual(harness.decodeCalls.map(({ frameId }) => frameId), ["frame-0"]);
  await harness.tick(1_750);
  assert.deepEqual(
    harness.decodeCalls.map(({ frameId }) => frameId),
    ["frame-0", "frame-1000"],
    "the obsolete in-flight decode must not retain the only decode slot",
  );

  pending.get("frame-1000").resolve({ frameId: "decoded-latest" });
  await flushPromises();
  await harness.tick(1_750);
  assert.deepEqual(harness.presented.map(({ frame }) => frame.frameId), ["frame-1000"]);
  pending.get("frame-0").resolve({ frameId: "decoded-obsolete" });
  await flushPromises();
  assert.deepEqual(harness.decodedDisposed, [{ frameId: "decoded-obsolete" }]);
});

test("an exact one-second rewind starts a fresh timed epoch", () => {
  const harness = createHarness();
  harness.playout.ingest(timedFrame(5_000));
  const generation = harness.playout.snapshot().generation;

  const result = harness.playout.ingest(timedFrame(4_000));
  assert.deepEqual(result, {
    accepted: true,
    mode: "timed",
    reason: "rewind",
  });
  const snapshot = harness.playout.snapshot();
  assert.equal(snapshot.generation, generation + 1);
  assert.equal(snapshot.anchorStampNs, (4_000n * NS_PER_MS).toString());
  assert.equal(snapshot.queuedFrames, 1);
  assert.equal(snapshot.metrics.rewinds, 1);
});

test("a long source interval is not a jump when wall time advances with it", () => {
  const harness = createHarness({ mode: "replay", throttleMs: 500 });
  harness.playout.ingest(timedFrame(0));
  const generation = harness.playout.snapshot().generation;

  harness.setNow(6_000);
  const result = harness.playout.ingest(timedFrame(6_000, { receivedAt: 6_000 }));
  assert.deepEqual(result, {
    accepted: true,
    mode: "timed",
    reason: "queued",
  });
  assert.equal(harness.playout.snapshot().generation, generation);
  assert.equal(harness.playout.snapshot().metrics.forwardJumps, 0);
});

test("a source-only forward jump skips the obsolete epoch and re-anchors", () => {
  const harness = createHarness({ mode: "replay", throttleMs: 500 });
  harness.playout.ingest(timedFrame(0));
  const generation = harness.playout.snapshot().generation;

  const result = harness.playout.ingest(timedFrame(6_000, { receivedAt: 1 }));
  assert.deepEqual(result, {
    accepted: true,
    mode: "timed",
    reason: "forward_jump",
  });
  const snapshot = harness.playout.snapshot();
  assert.equal(snapshot.generation, generation + 1);
  assert.equal(snapshot.anchorStampNs, (6_000n * NS_PER_MS).toString());
  assert.equal(snapshot.queuedFrames, 1);
  assert.equal(snapshot.metrics.forwardJumps, 1);
});

test("missing stamps use latest-only presentation and the next valid stamp starts a new epoch", async () => {
  const pending = new Map();
  const harness = createHarness({
    mode: "replay",
    decode(frame) {
      const operation = deferred();
      pending.set(frame.frameId, operation);
      return operation.promise;
    },
  });

  harness.playout.ingest(timedFrame(0, { frameId: "timed" }));
  const timedGeneration = harness.playout.snapshot().generation;
  assert.deepEqual(harness.playout.ingest(missingStampFrame("missing-old", 10)), {
    accepted: true,
    mode: "latest-only",
    reason: "missing_stamp",
  });
  await harness.tick();
  assert.deepEqual(harness.playout.ingest(missingStampFrame("missing-new", 20)), {
    accepted: true,
    mode: "latest-only",
    reason: "missing_stamp",
  });
  await harness.tick();
  assert.equal(harness.playout.snapshot().generation > timedGeneration, true);
  assert.equal(harness.playout.snapshot().queuedFrames, 0);

  pending.get("missing-old").resolve({ frameId: "decoded-old" });
  await flushPromises();
  assert.equal(harness.presented.length, 0, "an older latest-only decode must be discarded");
  pending.get("missing-new").resolve({ frameId: "decoded-new" });
  await flushPromises();
  assert.deepEqual(harness.presented.map(({ frame }) => frame.frameId), ["missing-new"]);

  harness.setNow(100);
  assert.deepEqual(harness.playout.ingest(timedFrame(1_000, {
    frameId: "timed-after-fallback",
    receivedAt: 30,
  })), {
    accepted: true,
    mode: "timed",
    reason: "queued",
  });
  assert.equal(harness.playout.snapshot().anchorStampNs, (1_000n * NS_PER_MS).toString());
});

test("a slow decode is presented once without a late rebuffer/decode livelock", async () => {
  const decodeOperation = deferred();
  const harness = createHarness({
    mode: "replay",
    decode: () => decodeOperation.promise,
  });
  harness.playout.ingest(timedFrame(0, { frameId: "slow-decode" }));

  await harness.tick(600);
  assert.equal(harness.decodeCalls.length, 1);
  harness.setNow(1_001); // 401 ms decode: 251 ms past the presentation deadline.
  decodeOperation.resolve({ frameId: "decoded-slow", width: 2, height: 2 });
  await flushPromises();
  await harness.tick(1_001);
  await harness.tick(1_751);

  assert.equal(harness.presented.length, 1);
  assert.equal(harness.presented[0].frame.frameId, "slow-decode");
  assert.equal(harness.decodeCalls.length, 1, "the decoded frame must not be decoded repeatedly");
});

test("reset invalidates and disposes a decode that resolves from the previous generation", async () => {
  const decodeOperation = deferred();
  const harness = createHarness({
    mode: "replay",
    decode: () => decodeOperation.promise,
  });
  harness.playout.ingest(timedFrame(0, { frameId: "before-reset" }));
  await harness.tick(600);
  assert.equal(harness.decodeCalls.length, 1);
  const generation = harness.playout.snapshot().generation;

  harness.playout.reset({ reason: "reconnect", clearPresented: true });
  assert.equal(harness.playout.snapshot().generation, generation + 1);
  assert.equal(harness.playout.snapshot().queuedFrames, 0);

  const decoded = { frameId: "late-decoded", width: 2, height: 2 };
  decodeOperation.resolve(decoded);
  await flushPromises();
  await harness.tick(2_000);

  assert.equal(harness.presented.length, 0);
  assert.deepEqual(harness.decodedDisposed, [decoded]);
});

test("latest defaults to 100ms and clamps throttle to the shared 100-5000ms bounds", () => {
  const omitted = createCameraPlayout({ mode: "latest" });
  assert.equal(
    omitted.snapshot().cadenceMs,
    100,
    "an omitted throttle must use the production 100ms latest-frame cadence",
  );
  omitted.destroy();

  const configured = createHarness({ mode: "latest", throttleMs: 100 });
  assert.equal(configured.playout.snapshot().cadenceMs, 100);

  const belowMinimum = createHarness({ mode: "latest", throttleMs: 1 });
  assert.equal(belowMinimum.playout.snapshot().cadenceMs, 100);

  const aboveMaximum = createHarness({ mode: "latest", throttleMs: 5_001 });
  assert.equal(aboveMaximum.playout.snapshot().cadenceMs, 5_000);
});

test("latest presents the first frame immediately and the newest burst frame at the throttle boundary", async () => {
  const harness = createHarness({ mode: "latest", throttleMs: 500 });
  harness.setNow(1_000);
  assert.deepEqual(harness.playout.ingest(timedFrame(0, {
    frameId: "first",
    receivedAt: 10_000,
  })), {
    accepted: true,
    mode: "latest",
    reason: "queued",
  });
  await flushPromises();

  assert.equal(harness.playout.snapshot().mode, "latest");
  assert.deepEqual(harness.presented.map(({ frame, at }) => [frame.frameId, at]), [
    ["first", 1_000],
  ]);

  for (const [at, stampMs, frameId] of [
    [1_100, 100, "burst-old"],
    [1_250, 250, "burst-middle"],
    [1_499, 499, "burst-newest"],
  ]) {
    harness.setNow(at);
    assert.equal(harness.playout.ingest(timedFrame(stampMs, { frameId, receivedAt: at })).accepted, true);
    await flushPromises();
  }

  assert.deepEqual(
    harness.presented.map(({ frame }) => frame.frameId),
    ["first"],
    "a burst inside the throttle window must retain the already presented frame",
  );
  await harness.tick(1_499);
  assert.deepEqual(harness.presented.map(({ frame }) => frame.frameId), ["first"]);
  await harness.tick(1_500);
  assert.deepEqual(harness.presented.map(({ frame, at }) => [frame.frameId, at]), [
    ["first", 1_000],
    ["burst-newest", 1_500],
  ]);
  assert.equal(harness.presented.some(({ frame }) => frame.frameId === "burst-old"), false);
  assert.equal(harness.presented.some(({ frame }) => frame.frameId === "burst-middle"), false);
});

test("latest presents a slow arrival immediately without adding a playback delay", async () => {
  const harness = createHarness({ mode: "latest", throttleMs: 500 });
  harness.playout.ingest(timedFrame(0, { frameId: "first", receivedAt: 0 }));
  await flushPromises();
  assert.deepEqual(harness.presented.map(({ at }) => at), [0]);

  harness.setNow(750);
  harness.playout.ingest(timedFrame(750, { frameId: "slow", receivedAt: 750 }));
  assert.deepEqual(
    harness.presented.map(({ frame }) => frame.frameId),
    ["first"],
    "the last frame remains visible while the new frame decodes",
  );
  await flushPromises();

  assert.deepEqual(harness.presented.map(({ frame, at }) => [frame.frameId, at]), [
    ["first", 0],
    ["slow", 750],
  ]);
});

test("latest coalesces arrivals just inside consecutive throttle windows without halving cadence", async () => {
  const harness = createHarness({ mode: "latest", throttleMs: 500 });
  harness.playout.ingest(timedFrame(0, { frameId: "at-0", receivedAt: 0 }));
  await flushPromises();

  harness.setNow(499);
  harness.playout.ingest(timedFrame(499, { frameId: "at-499", receivedAt: 499 }));
  await flushPromises();
  assert.deepEqual(harness.presented.map(({ frame, at }) => [frame.frameId, at]), [["at-0", 0]]);
  await harness.tick(500);
  assert.deepEqual(harness.presented.map(({ frame, at }) => [frame.frameId, at]), [
    ["at-0", 0],
    ["at-499", 500],
  ]);

  harness.setNow(998);
  harness.playout.ingest(timedFrame(998, { frameId: "at-998", receivedAt: 998 }));
  await flushPromises();
  await harness.tick(999);
  assert.deepEqual(harness.presented.map(({ frame }) => frame.frameId), ["at-0", "at-499"]);
  await harness.tick(1_000);
  assert.deepEqual(harness.presented.map(({ frame, at }) => [frame.frameId, at]), [
    ["at-0", 0],
    ["at-499", 500],
    ["at-998", 1_000],
  ]);
});

test("latest treats duplicate and reverse stamps as metadata and preserves arrival order", async () => {
  const harness = createHarness({ mode: "latest", throttleMs: 500 });
  harness.playout.ingest(timedFrame(1_000, { frameId: "visible", receivedAt: 0 }));
  await flushPromises();
  const baseline = harness.playout.snapshot();

  harness.setNow(100);
  const duplicate = harness.playout.ingest(timedFrame(1_000, {
    frameId: "duplicate-but-newer-arrival",
    receivedAt: 100,
  }));
  assert.equal(duplicate.accepted, true);
  assert.equal(duplicate.mode, "latest");

  harness.setNow(200);
  const reverse = harness.playout.ingest(timedFrame(500, {
    frameId: "reverse-but-newest-arrival",
    receivedAt: 200,
  }));
  assert.equal(reverse.accepted, true);
  assert.equal(reverse.mode, "latest");
  assert.equal(harness.playout.snapshot().generation, baseline.generation);

  await harness.tick(500);
  assert.deepEqual(harness.presented.map(({ frame }) => frame.frameId), [
    "visible",
    "reverse-but-newest-arrival",
  ]);
  const after = harness.playout.snapshot();
  assert.equal(after.metrics.duplicateStamp, baseline.metrics.duplicateStamp);
  assert.equal(after.metrics.outOfOrderStamp, baseline.metrics.outOfOrderStamp);
  assert.equal(after.metrics.rewinds, baseline.metrics.rewinds);
});

test("latest continuous bursts do not starve the active decode and supersede waiting candidates", async () => {
  const pending = new Map();
  const harness = createHarness({
    mode: "latest",
    throttleMs: 500,
    decode(frame) {
      const operation = deferred();
      pending.set(frame.frameId, operation);
      return operation.promise;
    },
  });

  harness.playout.ingest(timedFrame(0, { frameId: "initial", receivedAt: 0 }));
  await flushPromises();
  assert.deepEqual(harness.decodeCalls.map(({ frameId }) => frameId), ["initial"]);

  for (const [at, frameId] of [[10, "waiting-a"], [20, "waiting-b"], [30, "waiting-c"]]) {
    harness.setNow(at);
    harness.playout.ingest(timedFrame(at, { frameId, receivedAt: at }));
    await flushPromises();
  }
  assert.deepEqual(
    harness.decodeCalls.map(({ frameId }) => frameId),
    ["initial"],
    "new arrivals must not repeatedly cancel and starve the initial decode",
  );

  harness.setNow(100);
  pending.get("initial").resolve({ frameId: "decoded-initial" });
  await flushPromises();
  assert.deepEqual(harness.presented.map(({ frame }) => frame.frameId), ["initial"]);

  await harness.tick(599);
  assert.deepEqual(
    harness.presented.map(({ frame }) => frame.frameId),
    ["initial"],
    "pre-decoding is allowed, but the next frame cannot present before the throttle gate",
  );
  await harness.tick(600);
  assert.deepEqual(harness.decodeCalls.map(({ frameId }) => frameId), ["initial", "waiting-c"]);

  for (const [at, frameId] of [[610, "next-a"], [620, "next-b"], [630, "next-newest"]]) {
    harness.setNow(at);
    harness.playout.ingest(timedFrame(at, { frameId, receivedAt: at }));
    await flushPromises();
  }
  assert.deepEqual(
    harness.decodeCalls.map(({ frameId }) => frameId),
    ["initial", "waiting-c"],
    "continuous arrivals must leave the decode selected for the current gate runnable",
  );

  harness.setNow(650);
  pending.get("waiting-c").resolve({ frameId: "decoded-waiting-c" });
  await flushPromises();
  assert.deepEqual(harness.presented.map(({ frame, at }) => [frame.frameId, at]), [
    ["initial", 100],
    ["waiting-c", 650],
  ]);

  await harness.tick(1_149);
  assert.deepEqual(harness.presented.map(({ frame }) => frame.frameId), ["initial", "waiting-c"]);
  await harness.tick(1_150);
  assert.equal(
    harness.decodeCalls.some(({ frameId }) => frameId === "next-newest"),
    true,
    "the newest waiting candidate must begin decoding no later than its gate",
  );
  pending.get("next-newest").resolve({ frameId: "decoded-next-newest" });
  await flushPromises();
  assert.deepEqual(harness.presented.map(({ frame }) => frame.frameId), [
    "initial",
    "waiting-c",
    "next-newest",
  ]);
});

test("latest reset invalidates a pending decode and starts a fresh generation", async () => {
  const pending = new Map();
  const harness = createHarness({
    mode: "latest",
    throttleMs: 500,
    decode(frame) {
      const operation = deferred();
      pending.set(frame.frameId, operation);
      return operation.promise;
    },
  });

  harness.playout.ingest(timedFrame(0, { frameId: "before-reset", receivedAt: 0 }));
  await flushPromises();
  const generation = harness.playout.snapshot().generation;
  harness.playout.reset({ reason: "reconnect", clearPresented: false });
  assert.equal(harness.playout.snapshot().generation, generation + 1);
  assert.equal(harness.playout.snapshot().queuedFrames, 0);

  const staleDecoded = { frameId: "decoded-before-reset" };
  pending.get("before-reset").resolve(staleDecoded);
  await flushPromises();
  assert.deepEqual(harness.presented, []);
  assert.deepEqual(harness.decodedDisposed, [staleDecoded]);

  harness.setNow(100);
  harness.playout.ingest(timedFrame(100, { frameId: "after-reset", receivedAt: 100 }));
  await flushPromises();
  assert.deepEqual(harness.decodeCalls.map(({ frameId }) => frameId), ["before-reset", "after-reset"]);
  pending.get("after-reset").resolve({ frameId: "decoded-after-reset" });
  await flushPromises();
  assert.deepEqual(harness.presented.map(({ frame, at }) => [frame.frameId, at]), [
    ["after-reset", 100],
  ]);
});

test("latest applies the same throttle gate to missing stamps and invalid input keeps the last frame", async () => {
  const harness = createHarness({ mode: "latest", throttleMs: 500 });
  harness.playout.ingest(timedFrame(0, { frameId: "visible", receivedAt: 10 }));
  await flushPromises();
  const visible = harness.playout.snapshot();
  assert.equal(visible.lastPresentedStampNs, "0");

  harness.setNow(100);
  const missingResult = harness.playout.ingest(missingStampFrame("missing", 100));
  assert.equal(missingResult.accepted, true);
  assert.equal(missingResult.mode, "latest");
  await flushPromises();
  assert.deepEqual(harness.presented.map(({ frame }) => frame.frameId), ["visible"]);
  assert.equal(harness.playout.snapshot().lastPresentedStampNs, "0");
  assert.equal(harness.playout.snapshot().metrics.invalidStamp, visible.metrics.invalidStamp);

  const acceptedMissing = harness.playout.snapshot();
  harness.setNow(200);
  assert.deepEqual(harness.playout.ingest({
    ...timedFrame(200, { frameId: "invalid", receivedAt: 200 }),
    bytes: new Uint8Array(),
  }), {
    accepted: false,
    mode: "latest",
    reason: "invalid_payload",
  });
  const afterInvalid = harness.playout.snapshot();
  assert.equal(afterInvalid.lastAcceptedAt, acceptedMissing.lastAcceptedAt);
  assert.equal(afterInvalid.lastPresentedStampNs, "0");
  assert.equal(afterInvalid.generation, acceptedMissing.generation);
  assert.deepEqual(harness.presented.map(({ frame }) => frame.frameId), ["visible"]);

  await harness.tick(499);
  assert.deepEqual(harness.presented.map(({ frame }) => frame.frameId), ["visible"]);
  await harness.tick(500);
  assert.deepEqual(harness.presented.map(({ frame, at }) => [frame.frameId, at]), [
    ["visible", 0],
    ["missing", 500],
  ]);
});
