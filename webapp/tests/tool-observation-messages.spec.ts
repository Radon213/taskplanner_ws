import { expect, test } from "playwright/test";

import { rfdetrToolViewConfigs } from "../src/ros/rfdetrObservationSources";
import {
  normalizeTypedRfdetrToolDetections,
  normalizeVlmRequestToolDetectionEvidence,
} from "../src/ros/toolObservationMessages";

const REMOTE_CAM3 = rfdetrToolViewConfigs("reviewed-remote").find(
  (config) => config.cameraId === "cam3",
);

function directCam3Observation() {
  return {
    header: {
      stamp: { sec: 1_900_000_000, nanosec: 250_000_000 },
      frame_id: "cam_3_color_optical_frame",
    },
    sequence: 42,
    // These identifiers are intentionally not pinned to the current release.
    schema_version: "pnu.surgical_tool_observation_array.future-compatible",
    observation_id: "cam3:1900000000250000000",
    view: "cam_3",
    image_width: 1_280,
    image_height: 720,
    model_version: "remote-rfdetr-next-release",
    ontology_version: "surgical-tools-next-release",
    instances: [{
      frame_local_instance_id: 7,
      class_name: "scalpel",
      class_confidence: 0.875,
      bbox_xyxy_px: [128, 72, 640, 360],
      observation_point_valid: true,
      observation_point_uv_px: [320, 180],
      mask_rle: "not retained by the browser projection",
    }],
  };
}

function validVlmContext() {
  const freshness = { status: "fresh", received_age_sec: 0.02 };
  const visualAlignment = { status: "not_compared_no_flir_reference" };
  return {
    stamp: { sec: 1_900_000_001, nanosec: 125_000_000 },
    compact_json: JSON.stringify({
      observable_perception: {
        schema: "taskplanner.rfdetr_multiview_tool_context.v1",
        source: "rfdetr_tool_observation_2d",
        ground_truth: false,
        flir_reference_stamp_sec: null,
        max_source_skew_sec: 0.35,
        mask_rle_forwarded_to_vlm: false,
        freshness: { cam_3: freshness },
        visual_alignment: { cam_3: visualAlignment },
        tool_detection_views: [{
          view: "cam_3",
          source_stamp_sec: 1_900_000_000.25,
          sequence: 42,
          model_version: "remote-rfdetr-next-release",
          ontology_version: "surgical-tools-next-release",
          truncated: false,
          detection_status: "detections",
          freshness,
          visual_alignment: visualAlignment,
          instances: [{
            tool_id: "T01",
            class_name: "scalpel",
            confidence: 0.875,
            bbox_xyxy_norm: [0.1, 0.1, 0.5, 0.5],
            center_uv_norm: [0.3, 0.3],
            observation_point_uv_norm: [0.25, 0.25],
            image_region: "middle_left",
            depth_m: 0.72,
          }],
        }],
      },
    }),
  };
}

test("normalizes a version-compatible direct CAM3 observation from the reviewed remote route", () => {
  expect(REMOTE_CAM3).toBeDefined();
  expect(REMOTE_CAM3).toMatchObject({
    source: "reviewed-remote",
    configuredProducerHost: "192.168.1.7",
    topic: "/perception/cam_3/tool/observations",
  });
  const frame = normalizeTypedRfdetrToolDetections(
    directCam3Observation(),
    REMOTE_CAM3!,
    12_345,
  );
  expect(frame).toMatchObject({
    cameraId: "cam3",
    sourceView: "cam_3",
    sourceFrameId: "cam_3_color_optical_frame",
    sourceStampSec: 1_900_000_000.25,
    sequence: 42,
    schemaVersion: "pnu.surgical_tool_observation_array.future-compatible",
    modelVersion: "remote-rfdetr-next-release",
    ontologyVersion: "surgical-tools-next-release",
    source: "reviewed-remote",
    configuredProducerHost: "192.168.1.7",
    receivedAt: 12_345,
  });
  expect(frame?.instances).toEqual([{
    instanceId: 7,
    className: "scalpel",
    confidence: 0.875,
    bboxXyxyNorm: [0.1, 0.1, 0.5, 0.5],
    observationPointUvNorm: [0.25, 0.25],
  }]);
  expect(frame?.instances[0]).not.toHaveProperty("mask_rle");
});

test("rejects direct observations whose view or source frame does not match the configured route", () => {
  expect(REMOTE_CAM3).toBeDefined();
  expect(normalizeTypedRfdetrToolDetections(
    { ...directCam3Observation(), view: "cam_4" },
    REMOTE_CAM3!,
  )).toBeNull();
  expect(normalizeTypedRfdetrToolDetections(
    {
      ...directCam3Observation(),
      header: {
        ...directCam3Observation().header,
        frame_id: "cam_4_color_optical_frame",
      },
    },
    REMOTE_CAM3!,
  )).toBeNull();
});

test("normalizes valid structured VLM tool evidence without image or mask data", () => {
  const evidence = normalizeVlmRequestToolDetectionEvidence(validVlmContext(), 67_890);
  expect(evidence).toMatchObject({
    schema: "taskplanner.rfdetr_multiview_tool_context.v1",
    source: "rfdetr_tool_observation_2d",
    contextStampSec: 1_900_000_001.125,
    receivedAt: 67_890,
    flirReferenceStampSec: null,
    maxSourceSkewSec: 0.35,
  });
  expect(evidence?.views).toHaveLength(1);
  expect(evidence?.views[0]).toMatchObject({
    view: "cam_3",
    modelVersion: "remote-rfdetr-next-release",
    ontologyVersion: "surgical-tools-next-release",
    detectionStatus: "detections",
  });
  expect(evidence?.views[0].instances[0]).toMatchObject({
    toolId: "T01",
    className: "scalpel",
    bboxXyxyNorm: [0.1, 0.1, 0.5, 0.5],
  });
  expect(evidence).not.toHaveProperty("image");
  expect(evidence).not.toHaveProperty("mask_rle");
});

test("rejects malformed and oversized VLM context payloads", () => {
  const malformed = validVlmContext();
  const parsed = JSON.parse(malformed.compact_json) as {
    observable_perception: {
      tool_detection_views: Array<{ instances: Array<{ bbox_xyxy_norm: unknown }> }>;
    };
  };
  parsed.observable_perception.tool_detection_views[0].instances[0].bbox_xyxy_norm = [0.1, 0.2, 0.5];
  expect(normalizeVlmRequestToolDetectionEvidence({
    ...malformed,
    compact_json: JSON.stringify(parsed),
  })).toBeNull();
  expect(normalizeVlmRequestToolDetectionEvidence({
    compact_json: "x".repeat(256 * 1024 + 1),
  })).toBeNull();
});
