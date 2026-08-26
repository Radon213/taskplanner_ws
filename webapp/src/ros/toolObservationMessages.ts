import type { RosTime } from "../types";
import type { RfdetrToolViewConfig } from "./rfdetrObservationSources";
import { isBoundedRosPayload } from "./rosMessageBounds";

/**
 * Read-only projection of RF-DETR facts committed to a concrete VLM request.
 * Images, rendered overlays, and masks are intentionally excluded.
 */
export type VlmToolDetectionViewId = "cam_3" | "cam_4";

export type VlmToolDetectionFreshness = {
  status: string;
  receivedAgeSec: number | null;
};

/** Visual-frame comparison is diagnostic only; it never admits tool facts. */
export type VlmToolDetectionVisualAlignment = {
  status: string;
  detectorStampSec: number | null;
  offsetSec: number | null;
};

export type VlmToolDetectionInstance = {
  toolId: string;
  className: string;
  confidence: number;
  bboxXyxyNorm: readonly [number, number, number, number];
  centerUvNorm: readonly [number, number];
  observationPointUvNorm: readonly [number, number] | null;
  imageRegion: string;
  depthM: number | null;
};

export type VlmToolDetectionView = {
  view: VlmToolDetectionViewId;
  sourceStampSec: number;
  sequence: number;
  modelVersion: string;
  ontologyVersion: string;
  truncated: boolean;
  /** A fresh empty array is explicit; missing/misaligned is not zero. */
  detectionStatus: "detections" | "no_detections";
  freshness: VlmToolDetectionFreshness;
  visualAlignment: VlmToolDetectionVisualAlignment;
  instances: readonly VlmToolDetectionInstance[];
};

export type VlmRequestToolDetectionEvidence = {
  schema: "taskplanner.rfdetr_multiview_tool_context.v1";
  source: "rfdetr_tool_observation_2d";
  contextStampSec: number | null;
  receivedAt: number;
  flirReferenceStampSec: number | null;
  maxSourceSkewSec: number | null;
  freshness: Readonly<Partial<Record<VlmToolDetectionViewId, VlmToolDetectionFreshness>>>;
  visualAlignment: Readonly<Partial<Record<VlmToolDetectionViewId, VlmToolDetectionVisualAlignment>>>;
  views: readonly VlmToolDetectionView[];
};

/**
 * Compact browser-local projection of a typed ToolObservation2DArray.
 * Upstream masks and detector rasters are never retained.
 */
export type TypedRfdetrToolViewId = "cam3" | "cam4";

export type TypedRfdetrToolDetectionInstance = {
  instanceId: number;
  className: string;
  confidence: number;
  bboxXyxyNorm: readonly [number, number, number, number];
  observationPointUvNorm: readonly [number, number] | null;
};

export type TypedRfdetrToolDetectionFrame = {
  cameraId: TypedRfdetrToolViewId;
  sourceView: "cam_3" | "cam_4";
  sourceFrameId: string;
  sourceStampSec: number;
  sequence: number;
  schemaVersion: string;
  observationId: string;
  imageWidth: number;
  imageHeight: number;
  modelVersion: string;
  ontologyVersion: string;
  source: RfdetrToolViewConfig["source"];
  configuredProducerHost: string;
  topic: string;
  receivedAt: number;
  instances: readonly TypedRfdetrToolDetectionInstance[];
};

export type TypedRfdetrToolDetections = Readonly<
  Partial<Record<TypedRfdetrToolViewId, TypedRfdetrToolDetectionFrame>>
>;

const VLM_REQUEST_TOOL_CONTEXT_SCHEMA = "taskplanner.rfdetr_multiview_tool_context.v1";
const VLM_REQUEST_TOOL_CONTEXT_SOURCE = "rfdetr_tool_observation_2d";
const VLM_REQUEST_TOOL_CONTEXT_VIEWS = ["cam_3", "cam_4"] as const;
const VLM_REQUEST_TOOL_FRESHNESS_STATUSES = new Set([
  "fresh",
  "missing",
  "stale",
]);
const VLM_REQUEST_TOOL_VISUAL_ALIGNMENT_STATUSES = new Set([
  "aligned",
  "not_compared_no_flir_reference",
  "misaligned",
  "not_compared_missing_source_timestamp",
  "not_compared_no_fresh_detector",
  "not_compared_stale_detector",
]);
const VLM_REQUEST_TOOL_DETECTION_STATUSES = new Set([
  "detections",
  "no_detections",
]);
const MAX_VLM_REQUEST_CONTEXT_JSON_CHARS = 256 * 1024;
const MAX_VLM_REQUEST_TOOL_VIEWS = 2;
const MAX_VLM_REQUEST_TOOL_INSTANCES_PER_VIEW = 12;
const MAX_VLM_REQUEST_TOOL_TEXT_CHARS = 160;
const MAX_VLM_REQUEST_TOOL_MODEL_VERSION_CHARS = 80;
const MAX_VLM_REQUEST_TOOL_OFFSET_SEC = 60;
const MAX_VLM_REQUEST_TOOL_RECEIVED_AGE_SEC = 3_600;

// Camera previews and typed detector observations have independent cadence.
// A renderer must enforce this source-time bound before painting a detection.
export const TYPED_RFDETR_STAGE_MAX_FRAME_SKEW_SEC = 0.35;
const MAX_TYPED_RFDETR_TOOL_IMAGE_DIMENSION = 16_384;
const MAX_TYPED_RFDETR_TOOL_INSTANCES = 64;
const MAX_TYPED_RFDETR_TOOL_TEXT_CHARS = 160;
const MAX_TYPED_RFDETR_TOOL_MODEL_VERSION_CHARS = 160;

type RosTypedRfdetrToolObservationArray = {
  header?: {
    stamp?: RosTime;
    frame_id?: string;
  };
  sequence?: number;
  schema_version?: string;
  observation_id?: string;
  view?: string;
  image_width?: number;
  image_height?: number;
  model_version?: string;
  ontology_version?: string;
  instances?: unknown[];
};

function isFiniteNonNegativeNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function isFiniteUnitInterval(value: unknown): value is number {
  return isFiniteNonNegativeNumber(value) && value <= 1;
}

function isNonNegativeInteger(value: unknown): value is number {
  return isFiniteNonNegativeNumber(value) && Number.isInteger(value);
}

function isRosTime(value: unknown): value is RosTime {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const stamp = value as Partial<RosTime>;
  return typeof stamp.sec === "number"
    && Number.isSafeInteger(stamp.sec)
    && stamp.sec >= 0
    && isNonNegativeInteger(stamp.nanosec)
    && stamp.nanosec < 1_000_000_000;
}

function boundedVlmRequestToolText(
  value: unknown,
  maxLength = MAX_VLM_REQUEST_TOOL_TEXT_CHARS,
  allowEmpty = false,
): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  if ((!allowEmpty && !normalized) || normalized.length > maxLength) return null;
  return normalized;
}

function isVlmToolDetectionViewId(value: string): value is VlmToolDetectionViewId {
  return VLM_REQUEST_TOOL_CONTEXT_VIEWS.some((candidate) => candidate === value);
}

function optionalVlmRequestFiniteNumber(
  value: unknown,
  minimum: number,
  maximum: number,
): number | null | undefined {
  if (value === undefined || value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
    return undefined;
  }
  return value;
}

function normalizeVlmToolDetectionFreshness(
  value: unknown,
): VlmToolDetectionFreshness | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as Record<string, unknown>;
  const status = boundedVlmRequestToolText(candidate.status, 96);
  if (!status || !VLM_REQUEST_TOOL_FRESHNESS_STATUSES.has(status)) return null;
  const receivedAgeSec = optionalVlmRequestFiniteNumber(
    candidate.received_age_sec,
    0,
    MAX_VLM_REQUEST_TOOL_RECEIVED_AGE_SEC,
  );
  if (receivedAgeSec === undefined) return null;
  return { status, receivedAgeSec };
}

function normalizeVlmToolDetectionVisualAlignment(
  value: unknown,
): VlmToolDetectionVisualAlignment | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as Record<string, unknown>;
  const status = boundedVlmRequestToolText(candidate.status, 96);
  if (!status || !VLM_REQUEST_TOOL_VISUAL_ALIGNMENT_STATUSES.has(status)) return null;
  const detectorStampSec = optionalVlmRequestFiniteNumber(
    candidate.detector_stamp_sec,
    0,
    Number.MAX_SAFE_INTEGER,
  );
  const offsetSec = optionalVlmRequestFiniteNumber(
    candidate.offset_sec,
    -MAX_VLM_REQUEST_TOOL_OFFSET_SEC,
    MAX_VLM_REQUEST_TOOL_OFFSET_SEC,
  );
  if (detectorStampSec === undefined || offsetSec === undefined) return null;
  return { status, detectorStampSec, offsetSec };
}

function normalizeVlmToolDetectionVector(
  value: unknown,
  size: 2 | 4,
): readonly number[] | null {
  if (!Array.isArray(value) || value.length !== size) return null;
  if (!value.every((coordinate) => isFiniteUnitInterval(coordinate))) return null;
  if (size === 4 && (value[0] > value[2] || value[1] > value[3])) return null;
  return value as readonly number[];
}

function normalizeVlmToolDetectionInstance(
  value: unknown,
): VlmToolDetectionInstance | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as Record<string, unknown>;
  const rawToolId = candidate.tool_id;
  const toolId = rawToolId === undefined || rawToolId === null
    ? ""
    : boundedVlmRequestToolText(rawToolId, MAX_VLM_REQUEST_TOOL_TEXT_CHARS, true);
  const className = boundedVlmRequestToolText(candidate.class_name);
  const confidence = candidate.confidence;
  const bbox = normalizeVlmToolDetectionVector(candidate.bbox_xyxy_norm, 4);
  const center = normalizeVlmToolDetectionVector(candidate.center_uv_norm, 2);
  const rawObservationPoint = candidate.observation_point_uv_norm;
  const observationPoint = rawObservationPoint === undefined || rawObservationPoint === null
    ? null
    : normalizeVlmToolDetectionVector(rawObservationPoint, 2);
  const rawImageRegion = candidate.image_region;
  const imageRegion = rawImageRegion === undefined || rawImageRegion === null
    ? ""
    : boundedVlmRequestToolText(rawImageRegion, MAX_VLM_REQUEST_TOOL_TEXT_CHARS, true);
  const depthM = optionalVlmRequestFiniteNumber(candidate.depth_m, 0, 100);
  if (
    toolId === null
    || !className
    || !isFiniteUnitInterval(confidence)
    || !bbox
    || !center
    || (rawObservationPoint !== undefined && rawObservationPoint !== null && !observationPoint)
    || imageRegion === null
    || depthM === undefined
  ) {
    return null;
  }
  return {
    toolId,
    className,
    confidence,
    bboxXyxyNorm: bbox as readonly [number, number, number, number],
    centerUvNorm: center as readonly [number, number],
    observationPointUvNorm: observationPoint as readonly [number, number] | null,
    imageRegion,
    depthM,
  };
}

/**
 * Validate the exact structured-observation projection committed to a VLM
 * request. Unsupported or malformed contracts clear observer evidence instead
 * of making a stale tool location look current.
 */
export function normalizeVlmRequestToolDetectionEvidence(
  message: unknown,
  receivedAt = Date.now(),
): VlmRequestToolDetectionEvidence | null {
  if (!message || typeof message !== "object" || Array.isArray(message)) return null;
  const envelope = message as Record<string, unknown>;
  const compactJson = envelope.compact_json;
  if (
    typeof compactJson !== "string"
    || !compactJson
    || compactJson.length > MAX_VLM_REQUEST_CONTEXT_JSON_CHARS
  ) {
    return null;
  }
  try {
    const parsed = JSON.parse(compactJson) as Record<string, unknown>;
    if (!isBoundedRosPayload(parsed)) return null;
    const perception = parsed.observable_perception;
    if (!perception || typeof perception !== "object" || Array.isArray(perception)) return null;
    const payload = perception as Record<string, unknown>;
    if (
      payload.schema !== VLM_REQUEST_TOOL_CONTEXT_SCHEMA
      || payload.source !== VLM_REQUEST_TOOL_CONTEXT_SOURCE
      || payload.ground_truth !== false
      || payload.mask_rle_forwarded_to_vlm !== false
    ) {
      return null;
    }
    const flirReferenceStampSec = optionalVlmRequestFiniteNumber(
      payload.flir_reference_stamp_sec,
      0,
      Number.MAX_SAFE_INTEGER,
    );
    const maxSourceSkewSec = optionalVlmRequestFiniteNumber(
      payload.max_source_skew_sec,
      0,
      MAX_VLM_REQUEST_TOOL_OFFSET_SEC,
    );
    if (flirReferenceStampSec === undefined || maxSourceSkewSec === undefined) return null;
    const rawFreshness = payload.freshness;
    const rawVisualAlignment = payload.visual_alignment;
    if (
      !rawFreshness
      || typeof rawFreshness !== "object"
      || Array.isArray(rawFreshness)
      || !rawVisualAlignment
      || typeof rawVisualAlignment !== "object"
      || Array.isArray(rawVisualAlignment)
    ) {
      return null;
    }
    const freshnessPayload = rawFreshness as Record<string, unknown>;
    const visualAlignmentPayload = rawVisualAlignment as Record<string, unknown>;
    const freshness: Partial<Record<VlmToolDetectionViewId, VlmToolDetectionFreshness>> = {};
    const visualAlignment: Partial<
      Record<VlmToolDetectionViewId, VlmToolDetectionVisualAlignment>
    > = {};
    for (const view of VLM_REQUEST_TOOL_CONTEXT_VIEWS) {
      const rawViewFreshness = freshnessPayload[view];
      const rawViewVisualAlignment = visualAlignmentPayload[view];
      if ((rawViewFreshness === undefined) !== (rawViewVisualAlignment === undefined)) return null;
      if (rawViewFreshness === undefined) continue;
      const normalizedFreshness = normalizeVlmToolDetectionFreshness(rawViewFreshness);
      const normalizedVisualAlignment = normalizeVlmToolDetectionVisualAlignment(
        rawViewVisualAlignment,
      );
      if (!normalizedFreshness || !normalizedVisualAlignment) return null;
      freshness[view] = normalizedFreshness;
      visualAlignment[view] = normalizedVisualAlignment;
    }
    if (!Object.keys(freshness).length || !Object.keys(visualAlignment).length) return null;
    const rawViews = payload.tool_detection_views;
    if (!Array.isArray(rawViews) || rawViews.length > MAX_VLM_REQUEST_TOOL_VIEWS) return null;
    const seenViews = new Set<VlmToolDetectionViewId>();
    const views: VlmToolDetectionView[] = [];
    for (const rawView of rawViews) {
      if (!rawView || typeof rawView !== "object" || Array.isArray(rawView)) return null;
      const candidate = rawView as Record<string, unknown>;
      const viewName = boundedVlmRequestToolText(candidate.view, 16);
      if (!viewName || !isVlmToolDetectionViewId(viewName) || seenViews.has(viewName)) return null;
      const sourceStampSec = optionalVlmRequestFiniteNumber(
        candidate.source_stamp_sec,
        0,
        Number.MAX_SAFE_INTEGER,
      );
      const sequence = candidate.sequence;
      const modelVersion = boundedVlmRequestToolText(
        candidate.model_version,
        MAX_VLM_REQUEST_TOOL_MODEL_VERSION_CHARS,
        true,
      );
      const ontologyVersion = boundedVlmRequestToolText(
        candidate.ontology_version,
        MAX_VLM_REQUEST_TOOL_MODEL_VERSION_CHARS,
        true,
      );
      const rowFreshness = normalizeVlmToolDetectionFreshness(candidate.freshness);
      const rowVisualAlignment = normalizeVlmToolDetectionVisualAlignment(
        candidate.visual_alignment,
      );
      const rawInstances = candidate.instances;
      const detectionStatus = boundedVlmRequestToolText(candidate.detection_status, 32);
      if (
        sourceStampSec === null
        || sourceStampSec === undefined
        || !isNonNegativeInteger(sequence)
        || modelVersion === null
        || ontologyVersion === null
        || typeof candidate.truncated !== "boolean"
        || !rowFreshness
        || rowFreshness.status !== "fresh"
        || freshness[viewName]?.status !== "fresh"
        || !rowVisualAlignment
        || !visualAlignment[viewName]
        || rowVisualAlignment.status !== visualAlignment[viewName]?.status
        || !detectionStatus
        || !VLM_REQUEST_TOOL_DETECTION_STATUSES.has(detectionStatus)
        || !Array.isArray(rawInstances)
        || rawInstances.length > MAX_VLM_REQUEST_TOOL_INSTANCES_PER_VIEW
      ) {
        return null;
      }
      const instances = rawInstances.map(normalizeVlmToolDetectionInstance);
      if (instances.some((instance) => instance === null)) return null;
      if (
        (detectionStatus === "detections" && instances.length === 0)
        || (detectionStatus === "no_detections" && instances.length !== 0)
      ) {
        return null;
      }
      seenViews.add(viewName);
      views.push({
        view: viewName,
        sourceStampSec,
        sequence,
        modelVersion,
        ontologyVersion,
        truncated: candidate.truncated,
        detectionStatus: detectionStatus as VlmToolDetectionView["detectionStatus"],
        freshness: rowFreshness,
        visualAlignment: rowVisualAlignment,
        instances: instances as VlmToolDetectionInstance[],
      });
    }
    const contextStamp = isRosTime(envelope.stamp) ? envelope.stamp : null;
    return {
      schema: VLM_REQUEST_TOOL_CONTEXT_SCHEMA,
      source: VLM_REQUEST_TOOL_CONTEXT_SOURCE,
      contextStampSec: contextStamp
        ? contextStamp.sec + contextStamp.nanosec / 1_000_000_000
        : null,
      receivedAt,
      flirReferenceStampSec,
      maxSourceSkewSec,
      freshness,
      visualAlignment,
      views,
    };
  } catch {
    return null;
  }
}

function normalizeTypedRfdetrToolPixelVector(
  value: unknown,
  size: 2 | 4,
  imageWidth: number,
  imageHeight: number,
): readonly number[] | null {
  if (!Array.isArray(value) || value.length !== size) return null;
  if (!value.every((coordinate) => typeof coordinate === "number" && Number.isFinite(coordinate))) {
    return null;
  }
  const maximums = size === 2
    ? [imageWidth, imageHeight]
    : [imageWidth, imageHeight, imageWidth, imageHeight];
  if (value.some((coordinate, index) => coordinate < 0 || coordinate > maximums[index])) {
    return null;
  }
  if (size === 4 && (value[0] >= value[2] || value[1] >= value[3])) return null;
  return value as readonly number[];
}

function normalizeTypedRfdetrToolDetectionInstance(
  value: unknown,
  imageWidth: number,
  imageHeight: number,
): TypedRfdetrToolDetectionInstance | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as Record<string, unknown>;
  const instanceId = candidate.frame_local_instance_id;
  const className = boundedVlmRequestToolText(
    candidate.class_name,
    MAX_TYPED_RFDETR_TOOL_TEXT_CHARS,
  );
  const confidence = candidate.class_confidence;
  const bbox = normalizeTypedRfdetrToolPixelVector(
    candidate.bbox_xyxy_px,
    4,
    imageWidth,
    imageHeight,
  );
  if (
    !isNonNegativeInteger(instanceId)
    || !className
    || !isFiniteUnitInterval(confidence)
    || !bbox
  ) {
    return null;
  }
  const rawPoint = candidate.observation_point_uv_px;
  const point = candidate.observation_point_valid === true
    ? normalizeTypedRfdetrToolPixelVector(rawPoint, 2, imageWidth, imageHeight)
    : null;
  if (candidate.observation_point_valid === true && !point) return null;
  return {
    instanceId,
    className,
    confidence,
    bboxXyxyNorm: [
      bbox[0] / imageWidth,
      bbox[1] / imageHeight,
      bbox[2] / imageWidth,
      bbox[3] / imageHeight,
    ],
    observationPointUvNorm: point
      ? [point[0] / imageWidth, point[1] / imageHeight]
      : null,
  };
}

/**
 * Validate and compact a direct ToolObservation2DArray for the browser stage.
 * A non-empty schema/model/ontology version is retained rather than pinned to
 * one release, so a compatible upstream version change does not break display.
 */
export function normalizeTypedRfdetrToolDetections(
  message: unknown,
  expected: RfdetrToolViewConfig,
  receivedAt = Date.now(),
): TypedRfdetrToolDetectionFrame | null {
  if (!isBoundedRosPayload(message)) return null;
  const candidate = message as RosTypedRfdetrToolObservationArray;
  const header = candidate.header;
  const sourceStamp = header?.stamp;
  if (!header || !isRosTime(sourceStamp)) return null;
  const sourceStampSec = sourceStamp.sec + sourceStamp.nanosec / 1_000_000_000;
  const sourceFrameId = boundedVlmRequestToolText(
    header.frame_id,
    MAX_TYPED_RFDETR_TOOL_TEXT_CHARS,
  );
  const schemaVersion = boundedVlmRequestToolText(
    candidate.schema_version,
    MAX_TYPED_RFDETR_TOOL_TEXT_CHARS,
  );
  const observationId = boundedVlmRequestToolText(
    candidate.observation_id,
    MAX_TYPED_RFDETR_TOOL_TEXT_CHARS,
  );
  const modelVersion = boundedVlmRequestToolText(
    candidate.model_version,
    MAX_TYPED_RFDETR_TOOL_MODEL_VERSION_CHARS,
  );
  const ontologyVersion = boundedVlmRequestToolText(
    candidate.ontology_version,
    MAX_TYPED_RFDETR_TOOL_MODEL_VERSION_CHARS,
  );
  const sourceView = boundedVlmRequestToolText(candidate.view, 16);
  const imageWidth = candidate.image_width;
  const imageHeight = candidate.image_height;
  const rawInstances = candidate.instances;
  if (
    sourceStampSec <= 0
    || sourceFrameId !== expected.sourceFrameId
    || !schemaVersion
    || !observationId
    || sourceView !== expected.sourceView
    || !isNonNegativeInteger(candidate.sequence)
    || !isNonNegativeInteger(imageWidth)
    || !isNonNegativeInteger(imageHeight)
    || imageWidth <= 0
    || imageHeight <= 0
    || imageWidth > MAX_TYPED_RFDETR_TOOL_IMAGE_DIMENSION
    || imageHeight > MAX_TYPED_RFDETR_TOOL_IMAGE_DIMENSION
    || !modelVersion
    || !ontologyVersion
    || !Array.isArray(rawInstances)
    || rawInstances.length > MAX_TYPED_RFDETR_TOOL_INSTANCES
  ) {
    return null;
  }
  const instances = rawInstances.map((instance) =>
    normalizeTypedRfdetrToolDetectionInstance(instance, imageWidth, imageHeight),
  );
  if (instances.some((instance) => instance === null)) return null;
  return {
    cameraId: expected.cameraId,
    sourceView: expected.sourceView,
    sourceFrameId,
    sourceStampSec,
    sequence: candidate.sequence,
    schemaVersion,
    observationId,
    imageWidth,
    imageHeight,
    modelVersion,
    ontologyVersion,
    source: expected.source,
    configuredProducerHost: expected.configuredProducerHost,
    topic: expected.topic,
    receivedAt,
    instances: instances as TypedRfdetrToolDetectionInstance[],
  };
}
