import type { TaskplannerRuntimeMode } from "../runtimeModes";
import type { MissionCameraId } from "./missionSubscriptionPlan";

export type CameraPreviewSemantic = "camera_source" | "operator_overlay";

export type CameraPreviewContract = {
  topic: string;
  semantic: CameraPreviewSemantic;
};

export type CameraPreviewContracts = Record<MissionCameraId, CameraPreviewContract>;

type ExternalPreviewTopicOverrides = Partial<Record<
  "cam1" | "cam2" | "cam3OperatorOverlay" | "cam4OperatorOverlay" | "flir",
  string
>>;

function configuredTopic(value: string | undefined, fallback: string): string {
  return value?.trim() || fallback;
}

const INTERNAL_CAMERA_PREVIEW_CONTRACTS: CameraPreviewContracts = {
  cam1: { topic: "/surgery/images/cam1/compressed", semantic: "camera_source" },
  cam2: { topic: "/surgery/images/cam2/compressed", semantic: "camera_source" },
  cam3: { topic: "/surgery/images/cam3/compressed", semantic: "camera_source" },
  cam4: { topic: "/surgery/images/cam4/compressed", semantic: "camera_source" },
  flir: { topic: "/surgery/images/flir/compressed", semantic: "camera_source" },
};

/**
 * Resolve the external operator-view data plane by meaning, not by detector
 * build or model version. CAM3/CAM4 intentionally have no raw-camera fallback:
 * if the remote final compositor is unavailable, the UI must say that the
 * operator overlay is missing instead of silently presenting different pixels.
 */
export function externalCameraPreviewContracts(
  overrides: ExternalPreviewTopicOverrides = {},
): CameraPreviewContracts {
  return {
    cam1: {
      topic: configuredTopic(
        overrides.cam1,
        "/synced/cam_1/color/image_raw/compressed",
      ),
      semantic: "camera_source",
    },
    cam2: {
      topic: configuredTopic(
        overrides.cam2,
        "/synced/cam_2/color/image_raw/compressed",
      ),
      semantic: "camera_source",
    },
    cam3: {
      topic: configuredTopic(
        overrides.cam3OperatorOverlay,
        "/perception/cam_3/overlay/compressed",
      ),
      semantic: "operator_overlay",
    },
    cam4: {
      topic: configuredTopic(
        overrides.cam4OperatorOverlay,
        "/perception/cam_4/overlay/compressed",
      ),
      semantic: "operator_overlay",
    },
    flir: {
      topic: configuredTopic(
        overrides.flir,
        "/synced/flir/color/image_raw/compressed",
      ),
      semantic: "camera_source",
    },
  };
}

export function cameraPreviewContractsForMode(
  runtimeMode: TaskplannerRuntimeMode,
  externalContracts: CameraPreviewContracts,
): CameraPreviewContracts {
  return runtimeMode === "live" || runtimeMode === "llm"
    ? externalContracts
    : INTERNAL_CAMERA_PREVIEW_CONTRACTS;
}
