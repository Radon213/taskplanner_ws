import type {
  RosbagUiAuditMessage,
  RosbagUiLanguage,
  RosbagUiPresentation,
  RosbagUiWorkspace,
} from "../ros/rosbagUiAuditMessages";

export type RosbagUiReplayState = {
  presentation: RosbagUiPresentation;
  bundle: string;
  startPhase: string;
};

export function initialRosbagUiReplayState({
  language,
  workspace,
}: {
  language: RosbagUiLanguage;
  workspace: RosbagUiWorkspace;
}): RosbagUiReplayState {
  return {
    presentation: {
      language,
      workspace,
      stageSurgicalBedCamera: "flir",
      stageSurgicalBedInspecting: false,
      stageIndependentCamera: "cam1",
      stageIndependentInspecting: false,
      stageCam3Inspecting: false,
      surgeryRecordVisible: false,
      surgeryRecordTab: "record",
    },
    bundle: "",
    startPhase: "",
  };
}

/**
 * Presentation-only reducer used by `?rosbagReplay=1`.
 *
 * It intentionally has no ROS dependency: every replay input has already
 * passed the strict wire normalizer and only updates browser-local state.
 */
export function reduceRosbagUiReplayState(
  current: RosbagUiReplayState,
  event: RosbagUiAuditMessage,
): RosbagUiReplayState {
  return {
    presentation: event.presentation,
    bundle: event.bundle,
    startPhase: event.startPhase,
  };
}

export function isRosbagPresentationReplayEnabled(locationSearch?: string): boolean {
  const search = locationSearch ?? (
    typeof window === "undefined" ? "" : window.location.search
  );
  return new URLSearchParams(search).get("rosbagReplay") === "1";
}
