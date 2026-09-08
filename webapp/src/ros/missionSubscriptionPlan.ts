import type { TaskplannerRuntimeMode } from "../runtimeModes";
import type { MissionObservationProfile } from "../runtimeFeatures";

export type MissionCameraId = "cam1" | "cam2" | "cam3" | "cam4" | "flir";
export type RfdetrObservationSource = "reviewed-remote" | "disabled";

export type MissionSubscriptionPlan = {
  cameraPreviews: readonly MissionCameraId[];
  rfdetrObservationSource: RfdetrObservationSource;
  vlmModelVisual: boolean;
  vlmRequestContext: boolean;
  inputSourceStatuses: boolean;
  vlmReducerDecisions: boolean;
  surgeonLlmDecision: boolean;
  shadowReplay: boolean;
  cam4Semantics: boolean;
  /** Read-only provider/model/load-state observation; never grants controls. */
  modelStatusObservation: boolean;
  modelControls: boolean;
};

const LIVE_CAMERA_IDS = ["cam1", "cam2", "cam3", "cam4", "flir"] as const;

/**
 * Declare the browser data plane separately from rendering and normalization.
 * A Production Live page receives configured operator previews, authoritative
 * planner state, Live admission topics, and public typed RF-DETR topics.
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
      cameraPreviews: LIVE_CAMERA_IDS,
      rfdetrObservationSource:
        runtimeMode === "live" ? "reviewed-remote" : "disabled",
      vlmModelVisual: false,
      vlmRequestContext: false,
      // Speech input mode is selected at runtime by the adapter. Keep the
      // tiny source-status topic in the core profile so the Live ASR card and
      // public gateway can identify external vs microphone ingress even before
      // the first transcript arrives.
      inputSourceStatuses: true,
      vlmReducerDecisions: false,
      surgeonLlmDecision: false,
      shadowReplay: false,
      cam4Semantics: false,
      modelStatusObservation: true,
      modelControls: false,
    };
  }

  return {
    cameraPreviews: LIVE_CAMERA_IDS,
    rfdetrObservationSource:
      runtimeMode === "live" ? "reviewed-remote" : "disabled",
    vlmModelVisual: true,
    vlmRequestContext: true,
    inputSourceStatuses: true,
    vlmReducerDecisions: true,
    surgeonLlmDecision: runtimeMode === "llm",
    shadowReplay: runtimeMode === "shadow",
    cam4Semantics: runtimeMode !== "llm",
    modelStatusObservation: true,
    modelControls: true,
  };
}
