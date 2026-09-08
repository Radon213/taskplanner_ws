import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";
import ts from "typescript";

// These observer owners are pure data projections. Test them without a live
// ROS graph, model load, camera socket, or browser/operator session.
const testBuildDir = await mkdtemp(join(tmpdir(), "taskplanner-observer-test-"));
const names = [
  "rosMessageBounds", "toolObservationMessages", "rfdetrObservationSources",
  "typedRfdetrObservationStore", "ttsPlaybackMessages", "liveCameraMedia",
];
for (const name of names) {
  const source = await readFile(new URL(`../src/ros/${name}.ts`, import.meta.url), "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
  }).outputText.replace(/from "\.\/([^"]+)"/g, 'from "./$1.mjs"');
  await writeFile(join(testBuildDir, `${name}.mjs`), output);
}
const load = (name) => import(pathToFileURL(join(testBuildDir, `${name}.mjs`)));
const { normalizeTypedRfdetrToolDetections } = await load("toolObservationMessages");
const { rfdetrToolViewConfigs } = await load("rfdetrObservationSources");
const { createTypedRfdetrObservationStore } = await load("typedRfdetrObservationStore");
const { normalizeTtsPlaybackStatus } = await load("ttsPlaybackMessages");
const { createLiveCameraMediaStore } = await load("liveCameraMedia");
test.after(() => rm(testBuildDir, { force: true, recursive: true }));

const cam3 = rfdetrToolViewConfigs("reviewed-remote")[0];
function observation(overrides = {}) {
  return {
    header: { stamp: { sec: 100, nanosec: 0 }, frame_id: cam3.sourceFrameId },
    sequence: 1, schema_version: "next-version", observation_id: "cam3:100",
    view: "cam_3", image_width: 1280, image_height: 720,
    model_version: "remote-model", ontology_version: "tools-next",
    instances: [{
      frame_local_instance_id: 1, class_name: "Adson", class_confidence: 0.9,
      bbox_xyxy_px: [0, 0, 640, 360], observation_point_valid: true,
      observation_point_uv_px: [320, 180],
      mask_counts: new Array(10_000).fill(1),
    }],
    ...overrides,
  };
}

test("typed observations accept lossless masks without walking or retaining them", () => {
  const input = observation();
  Object.defineProperty(input.instances[0], "mask_counts", {
    get() { throw new Error("unused mask must not be traversed"); },
  });
  const frame = normalizeTypedRfdetrToolDetections(input, cam3, 1000);
  assert.equal(frame.instances.length, 1);
  assert.deepEqual(frame.instances[0].bboxXyxyNorm, [0, 0, 0.5, 0.5]);
  assert.equal("mask_counts" in frame.instances[0], false);
});

test("compact detector projection still bounds fields it consumes", () => {
  assert.equal(normalizeTypedRfdetrToolDetections(null, cam3), null);
  assert.equal(normalizeTypedRfdetrToolDetections(observation({ instances: new Array(65).fill({}) }), cam3), null);
  assert.equal(normalizeTypedRfdetrToolDetections(observation({ model_version: "x".repeat(161) }), cam3), null);
  const invalid = observation();
  invalid.instances[0].bbox_xyxy_px = [640, 0, 0, 360];
  assert.equal(normalizeTypedRfdetrToolDetections(invalid, cam3), null);
});

test("detector updates notify only their camera and expire once", () => {
  const store = createTypedRfdetrObservationStore();
  const received3 = [];
  const received4 = [];
  const off3 = store.subscribe("cam3", (frame) => received3.push(frame));
  store.subscribe("cam4", (frame) => received4.push(frame));
  const first = normalizeTypedRfdetrToolDetections(observation(), cam3, 1000);
  store.ingest(first);
  store.ingest({ ...first, sourceStampSec: 99 });
  store.ingest({ ...first });
  assert.equal(received3.length, 1);
  assert.equal(received4.length, 0);
  assert.equal(store.get("cam3"), first);
  store.expireOlderThan(3000, 3999);
  assert.equal(store.get("cam3"), first);
  store.expireOlderThan(3000, 4001);
  store.expireOlderThan(3000, 5000);
  assert.deepEqual(received3, [first, null]);
  off3();
  store.ingest(first);
  assert.equal(received3.length, 2);
});

function playback(overrides = {}) {
  return {
    stamp: { sec: 100, nanosec: 500_000_000 }, sequence: 2,
    reply_id: "reply", turn_id: "turn", utterance_id: "speech", procedure_run_id: "run",
    state: "playing", timing: "immediate", text: "애드슨을 전달드리겠습니다.",
    voice_id: "ko", output_device: "default", synth_latency_ms: 5,
    audio_duration_sec: 1.2, playback_latency_ms: 10,
    terminal: false, success: false, error_code: "", message: "playing",
    ...overrides,
  };
}

test("TTS uses actual lifecycle state and rejects malformed metrics", () => {
  const status = normalizeTtsPlaybackStatus(playback(), 1000);
  assert.equal(status.state, "playing");
  assert.equal(status.stampSec, 100.5);
  assert.equal(status.text, "애드슨을 전달드리겠습니다.");
  for (const state of ["queued", "played", "failed", "duplicate_suppressed"]) {
    assert.equal(normalizeTtsPlaybackStatus(playback({ state })).state, state);
  }
  for (const invalid of [
    { state: "invented" }, { success: "true" }, { synth_latency_ms: null },
    { playback_latency_ms: NaN }, { sequence: 0.5 },
    { stamp: { sec: 1, nanosec: 1_000_000_000 } }, { text: "x".repeat(4097) },
  ]) assert.equal(normalizeTtsPlaybackStatus(playback(invalid)), null);
});

test("camera keeps brief outages but clears indefinitely frozen frames", () => {
  const priorWindow = globalThis.window;
  const paints = new Map();
  let serial = 0;
  globalThis.window = {
    requestAnimationFrame: (callback) => { paints.set(++serial, callback); return serial; },
    cancelAnimationFrame: (id) => paints.delete(id),
    setTimeout: () => ++serial,
    clearTimeout: () => {},
  };
  const store = createLiveCameraMediaStore();
  try {
    const received = [];
    store.subscribe("cam1", (frame) => received.push(frame));
    store.ingest("cam1", {
      header: { stamp: { sec: 100, nanosec: 0 }, frame_id: "cam1" },
      data: new Uint8Array([1, 2, 3]), format: "jpeg",
    }, "/camera/compressed");
    for (const paint of paints.values()) paint();
    const frame = store.get("cam1");
    assert.ok(frame);
    store.expireOlderThan(3000, frame.receivedAt + 2999);
    assert.equal(store.get("cam1"), frame);
    store.expireOlderThan(3000, frame.receivedAt + 3001);
    assert.equal(store.get("cam1"), null);
    assert.deepEqual(received, [frame, null]);
  } finally {
    store.dispose();
    globalThis.window = priorWindow;
  }
});
