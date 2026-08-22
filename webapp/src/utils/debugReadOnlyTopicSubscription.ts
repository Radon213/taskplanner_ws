import ROSLIB from "roslib";

import type { DebugReadOnlyTopicSpec } from "../hooks/useIntegrationDebugBridge";

interface ReadOnlyRosConnection {
  isConnected?: boolean;
}

interface TopicHandle {
  callForSubscribeAndAdvertise?: (request: Record<string, unknown>) => void;
  subscribe: (callback: (message: unknown) => void) => void;
  unsubscribe: (callback: (message: unknown) => void) => void;
}

/**
 * Kept in the perception panel's deferred chunk so image subscription code is
 * never paid for by operators who do not open that Debug tab.
 */
export function subscribeDebugReadOnlyTopic(
  ros: ReadOnlyRosConnection,
  spec: DebugReadOnlyTopicSpec,
  onMessage: (message: unknown) => void,
): () => void {
  const topic = new ROSLIB.Topic({
    ros: ros as never,
    name: spec.name,
    messageType: spec.messageType,
    ...(spec.compression ? { compression: spec.compression } : {}),
    ...(spec.throttleRate === undefined ? {} : { throttle_rate: spec.throttleRate }),
    ...(spec.queueLength === undefined ? {} : { queue_length: spec.queueLength }),
  }) as unknown as TopicHandle;
  if (topic.callForSubscribeAndAdvertise) {
    const send = topic.callForSubscribeAndAdvertise.bind(topic);
    topic.callForSubscribeAndAdvertise = (request) => {
      if (request.op !== "subscribe") {
        send(request);
        return;
      }
      const normalizedRequest = { ...request };
      if (!spec.compression) delete normalizedRequest.compression;
      send(spec.reliability ? {
        ...normalizedRequest,
        qos: {
          history: "keep_last",
          depth: Math.max(1, spec.queueLength ?? 1),
          reliability: spec.reliability,
          durability: spec.durability ?? "volatile",
        },
      } : normalizedRequest);
    };
  }
  topic.subscribe(onMessage);
  return () => {
    try {
      topic.unsubscribe(onMessage);
    } catch {
      // The owning bridge can already be closed during reconnect cleanup.
    }
  };
}
