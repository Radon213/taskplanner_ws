import { useEffect, useRef, useState } from "react";
import ROSLIB from "roslib";

import {
  createLiveCameraMediaStore,
  type LiveCameraMediaStore,
  type LiveStageCameraId,
  type RosCompressedImagePayload,
} from "../ros/liveCameraMedia";
import { externalCameraPreviewContracts } from "../ros/cameraPreviewContracts";

/**
 * The media bridge is intentionally separate from the command/state bridge.
 * It is local-only and holds one latest frame per topic when a browser misses
 * a paint deadline, rather than allowing a FIFO WebSocket backlog to turn
 * into visible video latency.
 */
// Live defaults to the loopback owner on this workstation. A LAN deployment
// may explicitly provide a reviewed media proxy URL without touching code.
export const LIVE_CAMERA_MEDIA_BRIDGE_URL =
  import.meta.env.VITE_LIVE_MEDIA_BRIDGE_URL?.trim() || "ws://127.0.0.1:9095";

const RECONNECT_DELAY_MS = 750;
const CAMERA_FRAME_STALE_AFTER_MS = 3_000;
const CAMERA_STALE_SWEEP_MS = 500;

const externalPreviewContracts = externalCameraPreviewContracts({
  cam1: import.meta.env.VITE_EXTERNAL_CAM1_TOPIC,
  cam2: import.meta.env.VITE_EXTERNAL_CAM2_TOPIC,
  cam3OperatorOverlay: import.meta.env.VITE_EXTERNAL_CAM3_OPERATOR_OVERLAY_TOPIC,
  cam4OperatorOverlay: import.meta.env.VITE_EXTERNAL_CAM4_OPERATOR_OVERLAY_TOPIC,
  flir: import.meta.env.VITE_EXTERNAL_FLIR_TOPIC,
});

/**
 * One source of truth for the five native-rate Live previews.  This mirrors
 * the existing operator-preview contracts, including their explicit env
 * overrides, rather than introducing a second camera-topic configuration.
 */
export const LIVE_CAMERA_MEDIA_TOPICS: Readonly<Record<LiveStageCameraId, string>> = {
  cam1: externalPreviewContracts.cam1.topic,
  cam2: externalPreviewContracts.cam2.topic,
  cam3: externalPreviewContracts.cam3.topic,
  cam4: externalPreviewContracts.cam4.topic,
  flir: externalPreviewContracts.flir.topic,
};

type RosbridgeRos = {
  close: () => void;
  connect: (url: string) => void;
  isConnected?: boolean;
  on: (event: "connection" | "close" | "error", listener: () => void) => void;
};

type RosbridgeTopic = {
  subscribe: (listener: (message: unknown) => void) => void;
  unsubscribe: () => void;
};

export type LiveCameraMediaBridge = {
  /**
   * Image components subscribe to this local store directly.  A JPEG arrival
   * never updates root React state or the command/state bridge.
   */
  store: LiveCameraMediaStore;
  /** Only the dedicated media socket's state; never mirrors control status. */
  transportConnected: boolean;
};

export type UseLiveCameraMediaBridgeOptions = {
  /** Mount only for the Live workspace; false releases the media socket. */
  enabled: boolean;
  /** Kept injectable for focused local tests and a future LAN media proxy. */
  url?: string;
};

function unsubscribeTopics(ros: RosbridgeRos, topics: RosbridgeTopic[]): void {
  // ROSLIB queues an unsubscribe until its next connection when the socket is
  // already closed.  Do not leave that queued command behind during unmount.
  if (!ros.isConnected) return;
  for (const topic of topics) {
    try {
      topic.unsubscribe();
    } catch {
      // A concurrent socket close has already discarded the subscription.
    }
  }
}

/**
 * Native-JPEG Live camera transport.
 *
 * The hook has no control authority and is deliberately unreferenced until
 * the stage components adopt the local `LiveCameraMediaStore` lane.  It keeps
 * the last rendered image through a transient socket reconnect, avoiding the
 * familiar "connecting / image / connecting" flash while media recovers.
 */
export function useLiveCameraMediaBridge({
  enabled,
  url = LIVE_CAMERA_MEDIA_BRIDGE_URL,
}: UseLiveCameraMediaBridgeOptions): LiveCameraMediaBridge {
  const storeRef = useRef<LiveCameraMediaStore | null>(null);
  if (!storeRef.current) storeRef.current = createLiveCameraMediaStore();
  const store = storeRef.current;

  const [transportConnected, setTransportConnected] = useState(false);
  const generationRef = useRef(0);

  useEffect(() => {
    store.activate();
    return () => {
      store.dispose();
    };
  }, [store]);

  useEffect(() => {
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    let disposed = false;
    let initialConnectTimer: number | null = null;
    let reconnectTimer: number | null = null;
    let staleSweepTimer: number | null = null;
    let ros: RosbridgeRos | null = null;
    let topics: RosbridgeTopic[] = [];

    const isCurrent = () => !disposed && generationRef.current === generation;
    const clearReconnectTimer = () => {
      if (reconnectTimer === null) return;
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    };
    const clearInitialConnectTimer = () => {
      if (initialConnectTimer === null) return;
      window.clearTimeout(initialConnectTimer);
      initialConnectTimer = null;
    };
    const scheduleReconnect = () => {
      if (!isCurrent() || reconnectTimer !== null || !ros) return;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        if (!isCurrent() || !ros) return;
        try {
          ros.connect(url);
        } catch {
          scheduleReconnect();
        }
      }, RECONNECT_DELAY_MS);
    };

    // The caller owns mode selection.  Only an intentional Live-mode exit
    // clears the displayed frames; an ordinary reconnect preserves them.
    if (!enabled || !url) {
      setTransportConnected(false);
      store.clear();
      return () => {
        disposed = true;
        if (generationRef.current === generation) generationRef.current += 1;
      };
    }

    const bridge = new ROSLIB.Ros() as RosbridgeRos;
    ros = bridge;
    staleSweepTimer = window.setInterval(() => {
      if (isCurrent()) store.expireOlderThan(CAMERA_FRAME_STALE_AFTER_MS);
    }, CAMERA_STALE_SWEEP_MS);

    bridge.on("connection", () => {
      if (!isCurrent() || !ros) return;
      clearReconnectTimer();
      setTransportConnected(true);

      // ROSLIB.Topic automatically replays this subscription after a close.
      // Keep the topic objects alive across a reconnect so that one socket
      // recovery cannot create duplicate JPEG deliveries or subscriptions.
      if (topics.length > 0) return;
      topics = (Object.entries(LIVE_CAMERA_MEDIA_TOPICS) as Array<
        [LiveStageCameraId, string]
      >).map(([cameraId, topicName]) => {
        const topic = new ROSLIB.Topic({
          ros,
          name: topicName,
          messageType: "sensor_msgs/msg/CompressedImage",
          compression: "cbor",
          // Native source rate: this is deliberately not a 5 Hz throttle.
          throttle_rate: 0,
          // A slow browser gets the latest pending JPEG, never a FIFO backlog.
          queue_length: 1,
        }) as RosbridgeTopic;
        topic.subscribe((message: unknown) => {
          if (!isCurrent()) return;
          store.ingest(
            cameraId,
            message as RosCompressedImagePayload,
            topicName,
          );
        });
        return topic;
      });
    });

    bridge.on("close", () => {
      if (!isCurrent()) return;
      // Preserve current image frames while ROSLIB reconnects its existing
      // topic objects.  This status belongs only to this media lane.
      setTransportConnected(false);
      scheduleReconnect();
    });

    bridge.on("error", () => {
      if (!isCurrent()) return;
      // A WebSocket error can precede the close event.  Do not clear frames or
      // flip the control bridge state; the close handler marks media offline.
      scheduleReconnect();
    });

    // React StrictMode deliberately runs one disposable effect setup/cleanup
    // before the real mount. Deferring the first connect lets that cleanup
    // cancel its socket instead of producing a one-shot media reconnect.
    initialConnectTimer = window.setTimeout(() => {
      initialConnectTimer = null;
      if (!isCurrent() || !ros) return;
      try {
        ros.connect(url);
      } catch {
        scheduleReconnect();
      }
    }, 0);

    return () => {
      disposed = true;
      if (generationRef.current === generation) generationRef.current += 1;
      clearInitialConnectTimer();
      clearReconnectTimer();
      if (staleSweepTimer !== null) window.clearInterval(staleSweepTimer);
      unsubscribeTopics(bridge, topics);
      topics = [];
      bridge.close();
      ros = null;
    };
  }, [enabled, store, url]);

  return { store, transportConnected };
}
