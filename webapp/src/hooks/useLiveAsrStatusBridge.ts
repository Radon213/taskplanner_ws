import { useEffect, useRef, useState } from "react";
import ROSLIB from "roslib";

import {
  DEFAULT_LIVE_ASR_STATUS,
  mergeExternalAsrTranscripts,
  normalizeExternalAsrFinal,
  normalizeExternalAsrPartial,
  normalizeLiveAsrStatus,
} from "../ros/liveAsrMessages";
import type { LiveAsrFinal, LiveAsrStatus } from "../types";

const ASR_RUNTIME_STATUS_TOPIC = "/input/asr/runtime_status";
const ASR_PARTIAL_TOPIC = "/surgery/audio/partial_utterance";
const ASR_OBSERVED_FINAL_TOPIC = "/surgery/audio/observed_utterance";
const PARTIAL_STALE_AFTER_MS = 5_000;
const RECONNECT_DELAY_MS = 750;

type RosbridgeRos = {
  close: () => void;
  connect: (url: string) => void;
  on: (event: "connection" | "close" | "error", listener: () => void) => void;
};

type RosbridgeTopic = {
  subscribe: (listener: (message: unknown) => void) => void;
  unsubscribe: () => void;
};

export type LiveAsrStatusObservation = {
  status: LiveAsrStatus;
  receivedAt: number | null;
  transportConnected: boolean;
  contractError: boolean;
};

/**
 * Keeps the high-frequency ASR heartbeat out of the mission/camera bridge.
 * This hook deliberately owns its socket and is mounted by LiveAsrPanel, so a
 * 10 Hz input-level update cannot cause the whole stage to repaint.
 */
export function useLiveAsrStatusBridge({
  enabled,
  url,
}: {
  enabled: boolean;
  url: string;
}): LiveAsrStatusObservation {
  const [status, setStatus] = useState<LiveAsrStatus>(DEFAULT_LIVE_ASR_STATUS);
  const [receivedAt, setReceivedAt] = useState<number | null>(null);
  const [transportConnected, setTransportConnected] = useState(false);
  const [contractError, setContractError] = useState(false);
  const generationRef = useRef(0);
  const statusRef = useRef<LiveAsrStatus>(DEFAULT_LIVE_ASR_STATUS);
  const externalPartialRef = useRef<string | null>(null);
  const externalPartialReceivedAtRef = useRef(0);
  const externalFinalsRef = useRef<LiveAsrFinal[]>([]);

  useEffect(() => {
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    let disposed = false;
    let initialConnectTimer: number | null = null;
    let reconnectTimer: number | null = null;
    let topics: RosbridgeTopic[] = [];
    let ros: RosbridgeRos | null = null;
    const isCurrent = () => !disposed && generationRef.current === generation;
    const clearTimers = () => {
      if (initialConnectTimer !== null) {
        window.clearTimeout(initialConnectTimer);
        initialConnectTimer = null;
      }
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    };
    const scheduleReconnect = () => {
      if (!isCurrent() || reconnectTimer !== null) return;
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

    setStatus(DEFAULT_LIVE_ASR_STATUS);
    setReceivedAt(null);
    setTransportConnected(false);
    setContractError(false);
    statusRef.current = DEFAULT_LIVE_ASR_STATUS;
    externalPartialRef.current = null;
    externalPartialReceivedAtRef.current = 0;
    externalFinalsRef.current = [];

    if (!enabled || !url) {
      return () => {
        disposed = true;
        if (generationRef.current === generation) generationRef.current += 1;
      };
    }

    const bridge = new ROSLIB.Ros() as RosbridgeRos;
    ros = bridge;
    bridge.on("connection", () => {
      if (!isCurrent() || !ros) return;
      clearTimers();
      setTransportConnected(true);
      // ROSLIB replays an existing Topic subscription after reconnect. Keep one
      // object for this socket generation so repeated closes cannot multiply
      // the 10 Hz meter/partial-text callbacks.
      if (topics.length) return;
      const statusTopic = new ROSLIB.Topic({
        ros,
        name: ASR_RUNTIME_STATUS_TOPIC,
        messageType: "std_msgs/msg/String",
        queue_length: 1,
      }) as RosbridgeTopic;
      const partialTopic = new ROSLIB.Topic({
        ros,
        name: ASR_PARTIAL_TOPIC,
        messageType: "surgical_msgs/msg/SpeechUtterance",
        queue_length: 1,
      }) as RosbridgeTopic;
      const observedFinalTopic = new ROSLIB.Topic({
        ros,
        name: ASR_OBSERVED_FINAL_TOPIC,
        messageType: "surgical_msgs/msg/SpeechUtterance",
        queue_length: 20,
      }) as RosbridgeTopic;
      topics = [statusTopic, partialTopic, observedFinalTopic];
      statusTopic.subscribe((message: unknown) => {
        if (!isCurrent()) return;
        const normalized = normalizeLiveAsrStatus(message);
        if (!normalized) {
          setContractError(true);
          return;
        }
        const now = Date.now();
        if (
          externalPartialRef.current !== null
          && externalPartialReceivedAtRef.current > 0
          && now - externalPartialReceivedAtRef.current > PARTIAL_STALE_AFTER_MS
        ) {
          externalPartialRef.current = "";
          externalPartialReceivedAtRef.current = 0;
        }
        const next = mergeExternalAsrTranscripts(
          normalized,
          externalPartialRef.current,
          externalFinalsRef.current,
        );
        statusRef.current = next;
        setStatus(next);
        setReceivedAt(now);
        setContractError(false);
      });
      partialTopic.subscribe((message: unknown) => {
        if (!isCurrent()) return;
        const partial = normalizeExternalAsrPartial(message);
        if (partial === null) {
          setContractError(true);
          return;
        }
        const now = Date.now();
        externalPartialRef.current = partial;
        externalPartialReceivedAtRef.current = now;
        const next = mergeExternalAsrTranscripts(
          statusRef.current,
          partial,
          externalFinalsRef.current,
        );
        statusRef.current = next;
        setStatus(next);
        setReceivedAt(now);
        setContractError(false);
      });
      observedFinalTopic.subscribe((message: unknown) => {
        if (!isCurrent()) return;
        const final = normalizeExternalAsrFinal(message);
        if (final === null) {
          setContractError(true);
          return;
        }
        const now = Date.now();
        externalFinalsRef.current = [...externalFinalsRef.current, final].slice(-48);
        externalPartialRef.current = "";
        externalPartialReceivedAtRef.current = 0;
        const next = mergeExternalAsrTranscripts(
          statusRef.current,
          "",
          externalFinalsRef.current,
        );
        statusRef.current = next;
        setStatus(next);
        setReceivedAt(now);
        setContractError(false);
      });
    });
    bridge.on("close", () => {
      if (!isCurrent()) return;
      setTransportConnected(false);
      scheduleReconnect();
    });
    bridge.on("error", () => {
      if (!isCurrent()) return;
      setTransportConnected(false);
      scheduleReconnect();
    });
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
      clearTimers();
      for (const topic of topics) topic.unsubscribe();
      topics = [];
      ros?.close();
      ros = null;
    };
  }, [enabled, url]);

  return { status, receivedAt, transportConnected, contractError };
}
