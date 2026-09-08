import { useEffect, useRef } from "react";
import ROSLIB from "roslib";

import {
  normalizeRosbagUiAuditMessage,
  ROSBAG_UI_AUDIT_TOPIC,
  type RosbagUiAuditMessage,
} from "../ros/rosbagUiAuditMessages";

const RECONNECT_DELAY_MS = 1_000;

/**
 * A small, subscribe-only bridge that remains mounted while replay switches
 * between the Mission, Debug, and Multicam workspaces. It never advertises a
 * topic, invokes a Service, or sends a ROS Action/control request.
 */
export function useRosbagUiAuditReplay({
  enabled,
  url,
  onAudit,
}: {
  enabled: boolean;
  url: string;
  onAudit: (event: RosbagUiAuditMessage) => void;
}) {
  const onAuditRef = useRef(onAudit);
  useEffect(() => {
    onAuditRef.current = onAudit;
  }, [onAudit]);

  useEffect(() => {
    if (!enabled || !url) return undefined;

    let disposed = false;
    let activeRos: InstanceType<typeof ROSLIB.Ros> | null = null;
    let activeTopic: InstanceType<typeof ROSLIB.Topic> | null = null;
    let reconnectTimer: number | null = null;

    const clearActiveConnection = () => {
      if (activeTopic) {
        try {
          activeTopic.unsubscribe();
        } catch {
          // Closing a read-only replay bridge is best effort.
        }
        activeTopic = null;
      }
      if (activeRos) {
        try {
          activeRos.close();
        } catch {
          // A failed replay socket has already released its resources.
        }
        activeRos = null;
      }
    };

    const scheduleReconnect = () => {
      if (disposed || reconnectTimer !== null) return;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, RECONNECT_DELAY_MS);
    };

    const connect = () => {
      if (disposed || activeRos) return;
      const ros = new ROSLIB.Ros();
      const topic = new ROSLIB.Topic({
        ros,
        name: ROSBAG_UI_AUDIT_TOPIC,
        messageType: "std_msgs/msg/String",
        queue_length: 10,
      });
      activeRos = ros;
      activeTopic = topic;

      ros.on("connection", () => {
        if (disposed || ros !== activeRos) return;
        topic.subscribe((message: unknown) => {
          const event = normalizeRosbagUiAuditMessage(message);
          if (event) onAuditRef.current(event);
        });
      });
      ros.on("close", () => {
        if (ros !== activeRos) return;
        activeRos = null;
        activeTopic = null;
        scheduleReconnect();
      });
      ros.on("error", () => {
        // A close event schedules the next subscription attempt. Do not turn a
        // replay-only display issue into an operator-visible runtime error.
      });
      try {
        ros.connect(url);
      } catch {
        if (ros === activeRos) {
          activeRos = null;
          activeTopic = null;
        }
        scheduleReconnect();
      }
    };

    connect();
    return () => {
      disposed = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      clearActiveConnection();
    };
  }, [enabled, url]);
}
