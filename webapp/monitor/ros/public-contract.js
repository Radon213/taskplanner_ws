export const PUBLIC_TOPIC_NAMES = Object.freeze({
  gatewayInfo: "/surgery/gateway_info",
  catalog: "/surgery/catalog",
  context: "/surgery/context",
  instruments: "/surgery/instruments",
  robots: "/surgery/robots",
  robotEndEffectors: "/surgery/robot_end_effectors",
  toolPredictions: "/surgery/tool_predictions",
  speech: "/surgery/speech",
  clinicalObservations: "/surgery/clinical_observations",
  health: "/surgery/health",
  events: "/surgery/events",
  flirCamera: "/surgery/images/flir/compressed",
  cam4Camera: "/surgery/images/cam4/compressed",
});

export const PUBLIC_CONTRACT = Object.freeze({
  schemaVersion: "1.1.0",
  interfaceVersion: "0.3.0",
  snapshotStaleAfterMs: 3000,
  cameraStaleAfterMs: 3000,
});

export const DEFAULT_CAMERA_THROTTLE_RATE_MS = 100;

export const SCENARIO_STATE_TOPICS = Object.freeze([
  Object.freeze({
    name: PUBLIC_TOPIC_NAMES.gatewayInfo,
    messageType: "surgical_interop_msgs/msg/GatewayInfo",
  }),
  Object.freeze({
    name: PUBLIC_TOPIC_NAMES.catalog,
    messageType: "surgical_interop_msgs/msg/ProcedureCatalog",
  }),
  Object.freeze({
    name: PUBLIC_TOPIC_NAMES.context,
    messageType: "surgical_interop_msgs/msg/SurgeryContext",
  }),
  Object.freeze({
    name: PUBLIC_TOPIC_NAMES.instruments,
    messageType: "surgical_interop_msgs/msg/InstrumentStateArray",
  }),
  Object.freeze({
    name: PUBLIC_TOPIC_NAMES.robots,
    messageType: "surgical_interop_msgs/msg/RobotStateArray",
  }),
  Object.freeze({
    name: PUBLIC_TOPIC_NAMES.robotEndEffectors,
    messageType: "surgical_interop_msgs/msg/RobotEndEffectorStateArray",
  }),
  Object.freeze({
    name: PUBLIC_TOPIC_NAMES.toolPredictions,
    messageType: "surgical_interop_msgs/msg/ToolPredictionArray",
  }),
  Object.freeze({
    name: PUBLIC_TOPIC_NAMES.speech,
    messageType: "surgical_interop_msgs/msg/SpeechRecognitionState",
  }),
  Object.freeze({
    name: PUBLIC_TOPIC_NAMES.health,
    messageType: "surgical_interop_msgs/msg/SurgeryHealth",
  }),
]);

export function createMainLayoutSubscriptions(cameraStreams = {}) {
  const subscriptions = SCENARIO_STATE_TOPICS.map((definition) => ({
    ...definition,
    compression: "none",
    queue_length: 1,
  }));

  if (cameraStreams.enabled === false) return subscriptions;

  if (
    cameraStreams.topic !== undefined
    && String(cameraStreams.topic) !== PUBLIC_TOPIC_NAMES.flirCamera
  ) {
    throw new Error(`Unsupported public camera topic: ${cameraStreams.topic}`);
  }

  const throttleRate = Math.min(
    5000,
    Math.max(
      100,
      Number(cameraStreams.throttleRateMs) || DEFAULT_CAMERA_THROTTLE_RATE_MS,
    ),
  );
  subscriptions.push({
    name: PUBLIC_TOPIC_NAMES.flirCamera,
    messageType: "sensor_msgs/msg/CompressedImage",
    compression: "cbor",
    queue_length: 1,
    throttle_rate: throttleRate,
    kind: "camera",
    view: "shared",
  });
  return subscriptions;
}
