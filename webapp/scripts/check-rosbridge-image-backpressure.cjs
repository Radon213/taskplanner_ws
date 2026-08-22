const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(
  path.resolve(__dirname, "..", "src", "hooks", "useRosBridge.ts"),
  "utf8",
);

const violations = [];

for (const topic of [
  "/preview/cam_1/color/image_raw/compressed",
  "/preview/cam_2/color/image_raw/compressed",
  "/preview/cam_3/color/image_raw/compressed",
  "/preview/cam_4/color/image_raw/compressed",
  "/preview/flir/color/image_raw/compressed",
]) {
  if (!source.includes(topic)) {
    violations.push(`Live camera fallback must use the rate-limited preview source ${topic}`);
  }
}

if (!source.includes("const ROSBRIDGE_IMAGE_QUEUE_LENGTH = 1;")) {
  violations.push("CompressedImage subscriptions must retain only the freshest queued frame");
}
if (!source.includes('const ROSBRIDGE_IMAGE_COMPRESSION = "cbor";')) {
  violations.push("CompressedImage subscriptions must use binary CBOR transport");
}
if (!source.includes("const CAMERA_FRAME_THROTTLE_MS = 180;")) {
  violations.push("Mission camera previews must not exceed the 5 Hz preview-plane budget");
}
for (const qosContract of [
  'history: "keep_last"',
  "depth: 1",
  'reliability: "best_effort"',
  'durability: "volatile"',
  "configurePreviewImageSubscription(topic)",
]) {
  if (!source.includes(qosContract)) {
    violations.push(`Physical preview cameras must request QoS: ${qosContract}`);
  }
}

const topicBlocks = [...source.matchAll(/new ROSLIB\.Topic\(\{([\s\S]*?)\}\)/g)]
  .map((match) => match[1])
  .filter((block) => block.includes('messageType: "sensor_msgs/msg/CompressedImage"'));

if (topicBlocks.length !== 2) {
  violations.push(`Expected two CompressedImage topic factories, found ${topicBlocks.length}`);
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
