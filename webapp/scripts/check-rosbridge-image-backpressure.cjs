const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(
  path.resolve(__dirname, "..", "src", "hooks", "useRosBridge.ts"),
  "utf8",
);

const violations = [];

if (!source.includes("const ROSBRIDGE_IMAGE_QUEUE_LENGTH = 1;")) {
  violations.push("CompressedImage subscriptions must retain only the freshest queued frame");
}
if (!source.includes('const ROSBRIDGE_IMAGE_COMPRESSION = "cbor";')) {
  violations.push("CompressedImage subscriptions must use binary CBOR transport");
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
