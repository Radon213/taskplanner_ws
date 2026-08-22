import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_CAMERA_THROTTLE_RATE_MS,
  PUBLIC_TOPIC_NAMES,
  SCENARIO_STATE_TOPICS,
  createMainLayoutSubscriptions,
} from "../ros/public-contract.js";
import {
  compressedImageTiming,
  normalizeCompressedImage,
} from "../ros/compressed-image.js";

test("Main Layout subscribes to nine snapshots plus one shared FLIR camera stream", () => {
  const subscriptions = createMainLayoutSubscriptions({
    enabled: true,
    topic: PUBLIC_TOPIC_NAMES.flirCamera,
    throttleRateMs: 100,
  });
  assert.equal(SCENARIO_STATE_TOPICS.length, 9);
  assert.equal(subscriptions.length, 10);
  assert.equal(
    subscriptions.some(({ name }) => name === PUBLIC_TOPIC_NAMES.health),
    true,
  );
  const cameras = subscriptions.filter(({ kind }) => kind === "camera");
  assert.deepEqual(cameras.map(({ name }) => name), [PUBLIC_TOPIC_NAMES.flirCamera]);
  cameras.forEach((camera) => {
    assert.equal(camera.messageType, "sensor_msgs/msg/CompressedImage");
    assert.equal(camera.compression, "cbor");
    assert.equal(camera.queue_length, 1);
    assert.equal(camera.throttle_rate, 100, "the requested 100ms throttle must reach rosbridge unchanged");
  });
  assert.equal(DEFAULT_CAMERA_THROTTLE_RATE_MS, 100);
  assert.equal(
    createMainLayoutSubscriptions({ throttleRateMs: 1 }).find(({ kind }) => kind === "camera")?.throttle_rate,
    100,
    "camera subscription throttles use the same 100ms public-bridge lower bound as Settings",
  );
  assert.equal(
    createMainLayoutSubscriptions({ throttleRateMs: 5_001 }).find(({ kind }) => kind === "camera")?.throttle_rate,
    5_000,
    "camera subscription throttles use the same 5000ms upper bound as Settings and playout",
  );
  assert.equal(
    createMainLayoutSubscriptions().find(({ kind }) => kind === "camera")?.throttle_rate,
    100,
    "missing camera throttle falls back to 100ms",
  );
});

test("shared FLIR camera can be disabled and non-FLIR topics cannot be injected", () => {
  assert.equal(createMainLayoutSubscriptions({ enabled: false }).length, 9);
  assert.doesNotThrow(() => createMainLayoutSubscriptions({
    enabled: true,
    topic: PUBLIC_TOPIC_NAMES.flirCamera,
  }));
  for (const topic of [PUBLIC_TOPIC_NAMES.cam4Camera, "/tf", "/surgery/images/private/compressed"]) {
    assert.throws(
      () => createMainLayoutSubscriptions({ enabled: true, topic }),
      /Unsupported public camera topic/,
    );
  }
});

test("compressed image normalization accepts rosbridge base64 and CBOR typed arrays", () => {
  assert.deepEqual(normalizeCompressedImage({ format: "jpeg", data: "AQID" }), {
    mimeType: "image/jpeg",
    dataUrl: "data:image/jpeg;base64,AQID",
    bytes: null,
  });
  const binary = normalizeCompressedImage({ format: "png", data: Uint8Array.from([1, 2, 3]) });
  assert.equal(binary.mimeType, "image/png");
  assert.deepEqual([...binary.bytes], [1, 2, 3]);
  assert.equal(normalizeCompressedImage({ format: "jpeg", data: "data:text/html;base64,PHNjcmlwdD4=" }), null);
});

test("compressed image timing reads ROS 2 header stamps without inspecting image bytes", () => {
  const message = {
    header: {
      stamp: { sec: 1_723_958_400, nanosec: 125_500_000 },
      frame_id: "flir_color_optical_frame",
    },
    format: "jpeg",
    data: "private-image-body",
  };

  assert.deepEqual(compressedImageTiming(message), {
    sourceTimestampMs: 1_723_958_400_125.5,
    seconds: 1_723_958_400,
    nanoseconds: 125_500_000,
    frameId: "flir_color_optical_frame",
  });
  assert.equal("data" in compressedImageTiming(message), false);

  assert.deepEqual(compressedImageTiming({
    header: { stamp: { secs: 20, nsecs: 250_000_000 } },
  }), {
    sourceTimestampMs: 20_250,
    seconds: 20,
    nanoseconds: 250_000_000,
    frameId: "",
  });
});

test("compressed image timing treats missing or malformed header stamps as unavailable", () => {
  const invalidMessages = [
    null,
    {},
    { header: {} },
    { header: { stamp: {} } },
    { header: { stamp: { sec: -1, nanosec: 0 } } },
    { header: { stamp: { sec: 1, nanosec: -1 } } },
    { header: { stamp: { sec: 1, nanosec: 1_000_000_000 } } },
    { header: { stamp: { sec: 1.5, nanosec: 0 } } },
  ];
  invalidMessages.forEach((message) => assert.equal(compressedImageTiming(message), null));
});
