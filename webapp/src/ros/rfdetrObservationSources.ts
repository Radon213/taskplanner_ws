import type { RfdetrObservationSource } from "./missionSubscriptionPlan";

/** Deployment-contract value; ToolObservation2DArray does not attest publisher IP. */
export const CONFIGURED_RFDETR_PRODUCER_HOST = "192.168.1.7";

export type RfdetrToolViewConfig = {
  cameraId: "cam3" | "cam4";
  sourceView: "cam_3" | "cam_4";
  sourceFrameId: string;
  source: Exclude<RfdetrObservationSource, "disabled">;
  configuredProducerHost: string;
  topic: string;
};

const REVIEWED_REMOTE_CONFIGS: readonly RfdetrToolViewConfig[] = [
  {
    cameraId: "cam3",
    sourceView: "cam_3",
    sourceFrameId: "cam_3_color_optical_frame",
    source: "reviewed-remote",
    configuredProducerHost: CONFIGURED_RFDETR_PRODUCER_HOST,
    topic: "/perception/cam_3/tool/observations",
  },
  {
    cameraId: "cam4",
    sourceView: "cam_4",
    sourceFrameId: "cam_4_color_optical_frame",
    source: "reviewed-remote",
    configuredProducerHost: CONFIGURED_RFDETR_PRODUCER_HOST,
    topic: "/perception/cam_4/tool/observations",
  },
];

export function rfdetrToolViewConfigs(
  source: RfdetrObservationSource,
): readonly RfdetrToolViewConfig[] {
  if (source === "reviewed-remote") return REVIEWED_REMOTE_CONFIGS;
  return [];
}
