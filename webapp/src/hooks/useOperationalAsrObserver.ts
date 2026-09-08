import { useEffect, useState } from "react";

import { normalizeLiveAsrStatus } from "../ros/liveAsrMessages";
import type { LiveAsrStatus } from "../types";
import type { DebugReadOnlyTopicSubscriber } from "./useIntegrationDebugBridge";

const OPERATIONAL_ASR_RUNTIME_STATUS_TOPIC = "/input/asr/runtime_status";

export type OperationalAsrObservation = {
  asr: LiveAsrStatus | null;
  receivedAt: number;
  contractError: boolean;
};

/**
 * Reads the active operational ASR status through Debug's existing read-only
 * ROS session.  This hook is mounted only with the STT panel, so the 10 Hz
 * observer stream cannot repaint unrelated Debug workspaces.
 */
export function useOperationalAsrObserver(
  subscribeTopic: DebugReadOnlyTopicSubscriber,
): OperationalAsrObservation {
  const [asr, setAsr] = useState<LiveAsrStatus | null>(null);
  const [receivedAt, setReceivedAt] = useState(0);
  const [contractError, setContractError] = useState(false);

  useEffect(() => {
    setAsr(null);
    setReceivedAt(0);
    setContractError(false);
    return subscribeTopic({
      name: OPERATIONAL_ASR_RUNTIME_STATUS_TOPIC,
      messageType: "std_msgs/msg/String",
      // The operational owner publishes current state only.  Keep this
      // projection to the newest update, never a replayed partial transcript.
      queueLength: 1,
      reliability: "reliable",
    }, (message) => {
      const next = normalizeLiveAsrStatus(message);
      if (!next) {
        setContractError(true);
        return;
      }
      setAsr(next);
      setReceivedAt(Date.now());
      setContractError(false);
    });
  }, [subscribeTopic]);

  return { asr, receivedAt, contractError };
}
