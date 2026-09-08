const fs = require("node:fs");
const path = require("node:path");

const bridgeSource = fs.readFileSync(
  path.resolve(__dirname, "..", "src", "hooks", "useRosBridge.ts"),
  "utf8",
);
const previewContractSource = fs.readFileSync(
  path.resolve(__dirname, "..", "src", "ros", "cameraPreviewContracts.ts"),
  "utf8",
);
const source = `${bridgeSource}\n${previewContractSource}`;

const violations = [];

for (const topic of [
  "/synced/cam_1/color/image_raw/compressed",
  "/synced/cam_2/color/image_raw/compressed",
  "/perception/cam_3/overlay/compressed",
  "/perception/cam_4/overlay/compressed",
  "/synced/flir/color/image_raw/compressed",
]) {
  if (!source.includes(topic)) {
    violations.push(`Live operator preview contract is missing ${topic}`);
  }
}

for (const semanticVariable of [
  "VITE_EXTERNAL_CAM3_OPERATOR_OVERLAY_TOPIC",
  "VITE_EXTERNAL_CAM4_OPERATOR_OVERLAY_TOPIC",
]) {
  if (!bridgeSource.includes(semanticVariable)) {
    violations.push(`Live operator overlay must be configurable through ${semanticVariable}`);
  }
}
const operatorOverlaySemantics = previewContractSource.match(
  /semantic: "operator_overlay"/g,
) ?? [];
if (operatorOverlaySemantics.length < 2) {
  violations.push("CAM3/CAM4 operator overlays must retain explicit display semantics");
}
for (const forbiddenRawPreview of [
  "/synced/cam_3/color/image_raw/compressed",
  "/synced/cam_4/color/image_raw/compressed",
]) {
  if (previewContractSource.includes(forbiddenRawPreview)) {
    violations.push(`Operator preview must not silently fall back to ${forbiddenRawPreview}`);
  }
}

if (!source.includes("const ROSBRIDGE_IMAGE_QUEUE_LENGTH = 1;")) {
  violations.push("CompressedImage subscriptions must retain only the freshest queued frame");
}
if (!source.includes('const ROSBRIDGE_IMAGE_COMPRESSION = "cbor";')) {
  violations.push("CompressedImage subscriptions must use binary CBOR transport");
}
if (!source.includes("const CAMERA_FRAME_THROTTLE_MS = 0;")) {
  violations.push("Mission camera rendering must retain native synced-frame delivery");
}
for (const qosContract of [
  'history: "keep_last"',
  "depth: 1",
  'reliability: "best_effort"',
  'durability: "volatile"',
  "configurePreviewImageSubscription(topic)",
]) {
  if (!source.includes(qosContract)) {
    violations.push(`Physical synchronized cameras must request QoS: ${qosContract}`);
  }
}

const topicBlocks = [...bridgeSource.matchAll(/new ROSLIB\.Topic\(\{([\s\S]*?)\}\)/g)]
  .map((match) => match[1])
  .filter((block) => block.includes('messageType: "sensor_msgs/msg/CompressedImage"'));

if (topicBlocks.length === 0) {
  violations.push("Expected at least one bounded CompressedImage topic factory");
}

for (const block of topicBlocks) {
  if (!block.includes("queue_length: ROSBRIDGE_IMAGE_QUEUE_LENGTH")) {
    violations.push("Every CompressedImage topic factory must set queue_length=1");
  }
  if (!block.includes("compression: ROSBRIDGE_IMAGE_COMPRESSION")) {
    violations.push("Every CompressedImage topic factory must use CBOR");
  }
}

if (!source.includes("data?: string | number[] | Uint8Array;")) {
  violations.push("The frame decoder must accept CBOR Uint8Array image payloads");
}

if (violations.length) {
  console.error("ROSBridge image backpressure guard failed:");
  for (const violation of violations) console.error(`- ${violation}`);
  process.exit(1);
}

console.log("ROSBridge image backpressure guard passed.");
