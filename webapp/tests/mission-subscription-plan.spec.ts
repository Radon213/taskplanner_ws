import { expect, test } from "playwright/test";

import type { TaskplannerRuntimeMode } from "../src/runtimeModes";
import type { MissionObservationProfile } from "../src/runtimeFeatures";
import { missionSubscriptionPlan } from "../src/ros/missionSubscriptionPlan";
import {
  CONFIGURED_RFDETR_PRODUCER_HOST,
  rfdetrToolViewConfigs,
} from "../src/ros/rfdetrObservationSources";

const MODES: readonly TaskplannerRuntimeMode[] = [
  "live",
  "llm",
  "shadow",
  "debug",
];
const PROFILES: readonly MissionObservationProfile[] = ["live-core", "extended"];

test("every Live observation profile uses only the reviewed remote RF-DETR source", () => {
  for (const profile of PROFILES) {
    expect(missionSubscriptionPlan("live", profile).rfdetrObservationSource).toBe(
      "reviewed-remote",
    );
  }

  const configs = rfdetrToolViewConfigs("reviewed-remote");
  expect(CONFIGURED_RFDETR_PRODUCER_HOST).toBe("192.168.1.7");
  expect(configs).toEqual([
    expect.objectContaining({
      cameraId: "cam3",
      configuredProducerHost: "192.168.1.7",
      topic: "/perception/cam_3/tool/observations",
    }),
    expect.objectContaining({
      cameraId: "cam4",
      configuredProducerHost: "192.168.1.7",
      topic: "/perception/cam_4/tool/observations",
    }),
  ]);
});

test("non-Live modes cannot select an RF-DETR worker through the Mission plan", () => {
  for (const mode of MODES.filter((candidate) => candidate !== "live")) {
    for (const profile of PROFILES) {
      const source = missionSubscriptionPlan(mode, profile).rfdetrObservationSource;
      expect(source).toBe("disabled");
      expect(rfdetrToolViewConfigs(source)).toEqual([]);
    }
  }
});

test("Live core observes VLM runtime status without enabling model controls", () => {
  const plan = missionSubscriptionPlan("live", "live-core");

  expect(plan.modelStatusObservation).toBe(true);
  expect(plan.modelControls).toBe(false);
});
