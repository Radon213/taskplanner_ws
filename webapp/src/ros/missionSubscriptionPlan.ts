import type { TaskplannerRuntimeMode } from "../runtimeModes";
import type { MissionObservationProfile } from "../runtimeFeatures";

export type MissionCameraId = "cam1" | "cam2" | "cam3" | "cam4" | "flir";
export type RfdetrObservationSource = "reviewed-remote" | "disabled";

export type MissionSubscriptionPlan = {
  rawCameras: readonly MissionCameraId[];
  rfdetrObservationSource: RfdetrObservationSource;
  vlmModelVisual: boolean;
  vlmRequestContext: boolean;
  inputSourceStatuses: boolean;
  vlmReducerDecisions: boolean;
  surgeonLlmDecision: boolean;
  shadowReplay: boolean;
  cam4Semantics: boolean;
  modelControls: boolean;
};

const LIVE_CAMERA_IDS = ["cam1", "cam2", "cam3", "cam4", "flir"] as const;

/**
 * Declare the browser data plane separately from rendering and normalization.
 * A Production Live page receives raw operator cameras, authoritative planner
 * state, Live admission topics, and the configured public typed RF-DETR topics.
 * Model-visual and semantic observer streams are deliberately outside that
 * profile and are created only for an explicit extended launch. Typed RF-DETR
 * observations never fall back to a browser-local or Taskplanner-local worker:
 * every Live profile uses the reviewed remote publisher contract.
 */
export function missionSubscriptionPlan(
  runtimeMode: TaskplannerRuntimeMode,
  profile: MissionObservationProfile,
): MissionSubscriptionPlan {
  if (profile === "live-core") {
    return {
      rawCameras: LIVE_CAMERA_IDS,
      rfdetrObservationSource:
        runtimeMode === "live" ? "reviewed-remote" : "disabled",
      vlmModelVisual: false,
      vlmRequestContext: false,
      inputSourceStatuses: false,
      vlmReducerDecisions: false,
      surgeonLlmDecision: false,
      shadowReplay: false,
      cam4Semantics: false,
      modelControls: false,
    };
  }

  return {
    rawCameras: LIVE_CAMERA_IDS,
    rfdetrObservationSource:
      runtimeMode === "live" ? "reviewed-remote" : "disabled",
    vlmModelVisual: true,
    vlmRequestContext: true,
    inputSourceStatuses: true,
    vlmReducerDecisions: true,
    surgeonLlmDecision: runtimeMode === "llm",
    shadowReplay: runtimeMode === "shadow",
    cam4Semantics: runtimeMode !== "llm",
    modelControls: true,
  };
}
