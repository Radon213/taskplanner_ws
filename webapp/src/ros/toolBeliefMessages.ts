import type { RosTime } from "../types";
import { isBoundedRosPayload } from "./rosMessageBounds";
import {
  SET_TOOL_BELIEF_ENABLED_SERVICE,
  TOOL_BELIEF_ENABLED_MESSAGE_TYPE,
  TOOL_BELIEF_ENABLED_TOPIC,
  TOOL_BELIEF_MESSAGE_TYPE,
  TOOL_BELIEF_TOPIC,
} from "./toolBeliefContract";

export type ToolBeliefStatus =
  | "confirmed"
  | "probable"
  | "uncertain";

export type ToolLocationProbability = {
  locationId: string;
  probability: number;
};

export type TrackedToolBelief = {
  trackId: string;
  instrumentId: string;
  instanceId: string;
  displayName: string;
  existenceProbability: number;
  status: ToolBeliefStatus;
  committedLocationId: string;
  committedLocationProbability: number;
  mostLikelyLocationId: string;
  mostLikelyProbability: number;
  locations: readonly ToolLocationProbability[];
  lastPositiveAgeSec: number | null;
  evidenceSources: readonly string[];
  motionMode: string;
  statusFlags: readonly string[];
};

export const FIXED_TOOL_BELIEF_SCHEMA_V1 = "taskplanner.fixed_inventory_tool_belief.v1";
export const EXCHANGEABLE_TOOL_CAPACITY_SCHEMA_V2 = "taskplanner.exchangeable_tool_capacity_belief.v2";
export const TOOL_BELIEF_SCENARIO_CAPACITY_FLAG = "scenario_bounded_capacity";
export const TOOL_BELIEF_EXCHANGEABLE_FLAG = "logical_instance_exchangeable";
export const TOOL_BELIEF_PHYSICAL_IDENTITY_UNASSERTED_FLAG = "physical_identity_not_asserted";
export const TOOL_BELIEF_CAPACITY_ACTIVE_FLAG = "capacity_slot_active";
export const TOOL_BELIEF_CAPACITY_PROBABLE_FLAG = "capacity_slot_probable";
export const TOOL_BELIEF_CAPACITY_INACTIVE_FLAG = "capacity_slot_inactive";
export const TOOL_BELIEF_EXCHANGEABLE_ASSIGNED_FLAG = "exchangeable_slot_assigned";
export const TOOL_BELIEF_EXCHANGEABLE_REBOUND_FLAG = "exchangeable_slot_rebound";

const TOOL_BELIEF_CAPACITY_STATE_FLAGS = [
  TOOL_BELIEF_CAPACITY_ACTIVE_FLAG,
  TOOL_BELIEF_CAPACITY_PROBABLE_FLAG,
  TOOL_BELIEF_CAPACITY_INACTIVE_FLAG,
] as const;

function hasExchangeableCapacityContract(statusFlags: readonly string[]): boolean {
  return statusFlags.includes(TOOL_BELIEF_SCENARIO_CAPACITY_FLAG)
    && statusFlags.includes(TOOL_BELIEF_EXCHANGEABLE_FLAG)
    && statusFlags.includes(TOOL_BELIEF_PHYSICAL_IDENTITY_UNASSERTED_FLAG)
    && TOOL_BELIEF_CAPACITY_STATE_FLAGS.filter((flag) => statusFlags.includes(flag)).length === 1;
}

export function isExchangeableToolBelief(tool: TrackedToolBelief): boolean {
  return hasExchangeableCapacityContract(tool.statusFlags);
}

export function isInactiveCapacityToolBelief(tool: TrackedToolBelief): boolean {
  return isExchangeableToolBelief(tool)
    && tool.statusFlags.includes(TOOL_BELIEF_CAPACITY_INACTIVE_FLAG);
}

export type ToolBeliefSnapshot = {
  header: {
    stamp: RosTime | null;
    frameId: string;
  };
  schemaVersion: string;
  procedureId: string;
  procedureRunId: string;
  trackerRevision: string;
  observationOnly: boolean;
  robotMotionActive: boolean;
  robotMotionNegativeScale: number;
  ignoredOutOfInventoryCount: number;
  ignoredClassNames: readonly string[];
  tools: readonly TrackedToolBelief[];
  receivedAt: number;
};

export type ToolBeliefControlResult = {
  accepted: boolean;
  message: string;
};

export type ToolBeliefRuntime = {
  /** Authoritative latched ROS status; null until the first valid status. */
  enabled: boolean | null;
  /** Cleared whenever the authoritative status says the tracker is off. */
  snapshot: ToolBeliefSnapshot | null;
  /** Resolves only after both SetBool ACK and matching status are observed. */
  setEnabled: (enabled: boolean) => Promise<ToolBeliefControlResult>;
};

const TOOL_BELIEF_STATUSES = new Set<ToolBeliefStatus>([
  "confirmed",
  "probable",
  "uncertain",
]);
const MAX_TOOLS = 64;
const MAX_LOCATIONS_PER_TOOL = 16;
const MAX_LIST_ITEMS = 24;
const MAX_TEXT_LENGTH = 160;
const PROBABILITY_EPSILON = 1e-3;

function boundedText(value: unknown, allowEmpty = false): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  if ((!allowEmpty && !normalized) || normalized.length > MAX_TEXT_LENGTH) {
    return null;
  }
  return normalized;
}

function unitProbability(value: unknown): number | null {
  return typeof value === "number"
    && Number.isFinite(value)
    && value >= 0
    && value <= 1
    ? value
    : null;
}

function nonNegativeInteger(value: unknown): number | null {
  return typeof value === "number"
    && Number.isSafeInteger(value)
    && value >= 0
    ? value
    : null;
}

function normalizedTextList(value: unknown): readonly string[] | null {
  if (!Array.isArray(value) || value.length > MAX_LIST_ITEMS) return null;
  const result: string[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    const normalized = boundedText(item);
    if (normalized === null) return null;
    if (!seen.has(normalized)) {
      seen.add(normalized);
      result.push(normalized);
    }
  }
  return result;
}

function normalizedRosTime(value: unknown): RosTime | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const stamp = value as Record<string, unknown>;
  if (
    typeof stamp.sec !== "number"
    || !Number.isSafeInteger(stamp.sec)
    || stamp.sec < 0
    || typeof stamp.nanosec !== "number"
    || !Number.isInteger(stamp.nanosec)
    || stamp.nanosec < 0
    || stamp.nanosec >= 1_000_000_000
  ) {
    return null;
  }
  return { sec: stamp.sec, nanosec: stamp.nanosec };
}

function normalizedLocations(value: unknown): readonly ToolLocationProbability[] | null {
  if (!Array.isArray(value) || value.length === 0 || value.length > MAX_LOCATIONS_PER_TOOL) return null;
  const byLocation = new Map<string, number>();
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return null;
    const location = item as Record<string, unknown>;
    const locationId = boundedText(location.location_id);
    const probability = unitProbability(location.probability);
    if (
      locationId === null
      || probability === null
      || byLocation.has(locationId)
    ) {
      return null;
    }
    byLocation.set(locationId, probability);
  }
  const probabilityTotal = [...byLocation.values()].reduce(
    (total, probability) => total + probability,
    0,
  );
  if (
    !Number.isFinite(probabilityTotal)
    || probabilityTotal <= 0
    || Math.abs(probabilityTotal - 1) > PROBABILITY_EPSILON
  ) {
    return null;
  }
  return [...byLocation.entries()]
    .map(([locationId, probability]) => ({
      locationId,
      probability: probability / probabilityTotal,
    }))
    .sort((left, right) =>
      right.probability - left.probability
      || left.locationId.localeCompare(right.locationId),
    );
}

function matchingLocationProbability(
  locations: readonly ToolLocationProbability[],
  locationId: string,
  reportedProbability: number,
): number | null {
  const match = locations.find((location) => location.locationId === locationId);
  if (
    !match
    || Math.abs(match.probability - reportedProbability) > PROBABILITY_EPSILON
  ) {
    return null;
  }
  return match.probability;
}

function normalizedTool(
  value: unknown,
  schemaVersion: string,
): TrackedToolBelief | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const tool = value as Record<string, unknown>;
  const trackId = boundedText(tool.track_id);
  const instrumentId = boundedText(tool.instrument_id);
  const instanceId = boundedText(tool.instance_id);
  const displayName = boundedText(tool.display_name);
  const existenceProbability = unitProbability(tool.existence_probability);
  const status = boundedText(tool.status);
  const committedLocationId = boundedText(tool.committed_location_id, true);
  const committedLocationProbability = unitProbability(
    tool.committed_location_probability,
  );
  const locations = normalizedLocations(tool.locations);
  const evidenceSources = normalizedTextList(tool.evidence_sources);
  const motionMode = boundedText(tool.motion_mode, true);
  const statusFlags = normalizedTextList(tool.status_flags);
  const capacitySignal = statusFlags !== null && [
    TOOL_BELIEF_SCENARIO_CAPACITY_FLAG,
    TOOL_BELIEF_EXCHANGEABLE_FLAG,
    TOOL_BELIEF_PHYSICAL_IDENTITY_UNASSERTED_FLAG,
    TOOL_BELIEF_CAPACITY_ACTIVE_FLAG,
    TOOL_BELIEF_CAPACITY_PROBABLE_FLAG,
    TOOL_BELIEF_CAPACITY_INACTIVE_FLAG,
    TOOL_BELIEF_EXCHANGEABLE_ASSIGNED_FLAG,
    TOOL_BELIEF_EXCHANGEABLE_REBOUND_FLAG,
  ].some((flag) => statusFlags.includes(flag));
  const capacityContract = statusFlags !== null
    && hasExchangeableCapacityContract(statusFlags);
  if (
    trackId === null
    || instrumentId === null
    || instanceId === null
    || displayName === null
    || existenceProbability === null
    || (schemaVersion === FIXED_TOOL_BELIEF_SCHEMA_V1 && capacitySignal)
    || (capacitySignal && !capacityContract)
    || (!capacityContract && existenceProbability !== 1)
    || status === null
    || !TOOL_BELIEF_STATUSES.has(status as ToolBeliefStatus)
    || committedLocationId === null
    || committedLocationProbability === null
    || locations === null
    || evidenceSources === null
    || motionMode === null
    || statusFlags === null
  ) {
    return null;
  }

  const mostLikelyLocationId = boundedText(tool.most_likely_location_id);
  const mostLikelyProbability = unitProbability(tool.most_likely_probability);
  if (mostLikelyLocationId === null || mostLikelyProbability === null) return null;
  const normalizedMostLikelyProbability = matchingLocationProbability(
    locations,
    mostLikelyLocationId,
    mostLikelyProbability,
  );
  if (
    normalizedMostLikelyProbability === null
    || locations.some(
      (location) => location.probability > normalizedMostLikelyProbability + PROBABILITY_EPSILON,
    )
  ) {
    return null;
  }
  let normalizedCommittedLocationProbability = 0;
  if (committedLocationId) {
    const matchingCommittedProbability = matchingLocationProbability(
      locations,
      committedLocationId,
      committedLocationProbability,
    );
    if (matchingCommittedProbability === null) return null;
    normalizedCommittedLocationProbability = matchingCommittedProbability;
  } else if (committedLocationProbability > PROBABILITY_EPSILON) {
    return null;
  }
  const rawLastPositiveAgeSec = tool.last_positive_age_sec;
  const lastPositiveAgeSec = typeof rawLastPositiveAgeSec === "number"
    && Number.isFinite(rawLastPositiveAgeSec)
    && rawLastPositiveAgeSec >= 0
    ? rawLastPositiveAgeSec
    : null;

  return {
    trackId,
    instrumentId,
    instanceId,
    displayName,
    existenceProbability,
    status: status as ToolBeliefStatus,
    committedLocationId,
    committedLocationProbability: normalizedCommittedLocationProbability,
    mostLikelyLocationId,
    mostLikelyProbability: normalizedMostLikelyProbability,
    locations,
    lastPositiveAgeSec,
    evidenceSources,
    motionMode,
    statusFlags,
  };
}

/**
 * Convert rosbridge snake_case into a bounded, read-only UI projection.
 * The schema/revision strings are intentionally not pinned to one version;
 * additive tracker revisions can continue to render through this stable view.
 */
export function normalizeToolBeliefSnapshot(
  value: unknown,
  receivedAt = Date.now(),
): ToolBeliefSnapshot | null {
  if (!isBoundedRosPayload(value)) return null;
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const message = value as Record<string, unknown>;
  if (!Array.isArray(message.tools) || message.tools.length > MAX_TOOLS) return null;

  const schemaVersion = boundedText(message.schema_version);
  const procedureId = boundedText(message.procedure_id);
  const procedureRunId = boundedText(message.procedure_run_id, true);
  const trackerRevision = boundedText(message.tracker_revision);
  const ignoredClassNames = normalizedTextList(
    message.ignored_class_names ?? [],
  );
  const ignoredOutOfInventoryCount = nonNegativeInteger(
    message.ignored_out_of_inventory_count ?? 0,
  );
  const robotMotionNegativeScale = unitProbability(
    message.robot_motion_negative_scale ?? 1,
  );
  if (
    schemaVersion === null
    || procedureId === null
    || procedureRunId === null
    || trackerRevision === null
    || message.observation_only !== true
    || typeof message.robot_motion_active !== "boolean"
    || ignoredClassNames === null
    || ignoredOutOfInventoryCount === null
    || robotMotionNegativeScale === null
  ) {
    return null;
  }

  const tools: TrackedToolBelief[] = [];
  const trackIds = new Set<string>();
  for (const item of message.tools) {
    const tool = normalizedTool(item, schemaVersion);
    if (!tool || trackIds.has(tool.trackId)) return null;
    trackIds.add(tool.trackId);
    tools.push(tool);
  }

  const rawHeader = message.header;
  const header = rawHeader && typeof rawHeader === "object" && !Array.isArray(rawHeader)
    ? rawHeader as Record<string, unknown>
    : {};
  const frameId = boundedText(header.frame_id, true);
  if (frameId === null) return null;

  return {
    header: {
      stamp: normalizedRosTime(header.stamp),
      frameId,
    },
    schemaVersion,
    procedureId,
    procedureRunId,
    trackerRevision,
    observationOnly: true,
    robotMotionActive: message.robot_motion_active,
    robotMotionNegativeScale,
    ignoredOutOfInventoryCount,
    ignoredClassNames,
    tools,
    receivedAt,
  };
}

export function shouldAcceptToolBeliefSnapshot(
  current: ToolBeliefSnapshot | null,
  next: ToolBeliefSnapshot,
): boolean {
  if (
    !current
    || !next.procedureRunId
    || next.procedureRunId !== current.procedureRunId
    || !next.header.stamp
    || !current.header.stamp
  ) {
    return true;
  }
  return next.header.stamp.sec > current.header.stamp.sec
    || (
      next.header.stamp.sec === current.header.stamp.sec
      && next.header.stamp.nanosec > current.header.stamp.nanosec
    );
}

/** Keep validation and monotonic replacement inside this deferred contract chunk. */
export function reduceToolBeliefSnapshot(
  current: ToolBeliefSnapshot | null,
  value: unknown,
): ToolBeliefSnapshot | null {
  const next = normalizeToolBeliefSnapshot(value);
  return next && shouldAcceptToolBeliefSnapshot(current, next) ? next : current;
}

type ToolBeliefStateSetter = (runtime: ToolBeliefRuntime | null) => void;
type ToolBeliefTopic = {
  subscribe: (callback: (value: unknown) => void) => void;
  unsubscribe: () => void;
};
type ToolBeliefTopicConstructor = new (options: {
  ros: unknown;
  name: string;
  messageType: string;
}) => ToolBeliefTopic;
type ToolBeliefRosConnection = {
  idCounter?: number;
  on: (event: string, callback: (response: unknown) => void) => void;
  off?: (event: string, callback: (response: unknown) => void) => void;
  removeListener?: (event: string, callback: (response: unknown) => void) => void;
  callOnConnection: (request: Record<string, unknown>) => void;
};
const CONTROL_ACK_TIMEOUT_MS = 4_000;

function serviceResponse(value: unknown): ToolBeliefControlResult {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { accepted: false, message: "Malformed SetBool response." };
  }
  const response = value as Record<string, unknown>;
  const message = typeof response.message === "string"
    ? response.message.trim().slice(0, MAX_TEXT_LENGTH)
    : "";
  return response.success === true
    ? { accepted: true, message }
    : { accepted: false, message: message || "Tracker rejected the request." };
}

/** Deferred Live status/data subscription plus one narrow SetBool control. */
export default function createToolBeliefRuntimeContract(
  Topic: ToolBeliefTopicConstructor,
  ros: unknown,
  isCurrent: () => boolean,
  setRuntime: ToolBeliefStateSetter,
): ToolBeliefTopic | null {
  if (!isCurrent()) return null;
  const beliefTopic = new Topic({
    ros,
    name: TOOL_BELIEF_TOPIC,
    messageType: TOOL_BELIEF_MESSAGE_TYPE,
  });
  const enabledTopic = new Topic({
    ros,
    name: TOOL_BELIEF_ENABLED_TOPIC,
    messageType: TOOL_BELIEF_ENABLED_MESSAGE_TYPE,
  });
  const connection = ros as ToolBeliefRosConnection;
  let enabled: boolean | null = null;
  let snapshot: ToolBeliefSnapshot | null = null;
  const pendingServiceCancels = new Set<() => void>();
  const statusWaiters = new Set<{
    desired: boolean;
    resolve: () => void;
  }>();
  const publish = () => {
    if (!isCurrent()) return;
    setRuntime({ enabled, snapshot, setEnabled });
  };
  const waitForStatus = (desired: boolean) => {
    if (enabled === desired) return Promise.resolve();
    return new Promise<void>((resolve, reject) => {
      const waiter = { desired, resolve };
      statusWaiters.add(waiter);
      const timeout = globalThis.setTimeout(() => {
        statusWaiters.delete(waiter);
        reject(new Error("Tracker status acknowledgement timed out."));
      }, CONTROL_ACK_TIMEOUT_MS);
      waiter.resolve = () => {
        globalThis.clearTimeout(timeout);
        resolve();
      };
    });
  };
  async function setEnabled(desired: boolean): Promise<ToolBeliefControlResult> {
    if (!isCurrent()) {
      return { accepted: false, message: "ROS bridge generation changed." };
    }
    if (enabled === desired) {
      return { accepted: true, message: "Tracker already has the requested state." };
    }
    const response = await new Promise<ToolBeliefControlResult>((resolve) => {
      const id = `tool_belief_control:${Number(connection.idCounter ?? 0) + 1}`;
      connection.idCounter = Number(connection.idCounter ?? 0) + 1;
      let settled = false;
      const cleanup = () => {
        globalThis.clearTimeout(timeout);
        pendingServiceCancels.delete(cancel);
        if (connection.off) connection.off(id, onResponse);
        else connection.removeListener?.(id, onResponse);
      };
      const finish = (result: ToolBeliefControlResult) => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(result);
      };
      const onResponse = (value: unknown) => {
        if (!value || typeof value !== "object" || Array.isArray(value)) {
          finish({ accepted: false, message: "Malformed rosbridge response." });
          return;
        }
        const responseEnvelope = value as Record<string, unknown>;
        finish(responseEnvelope.result === false
          ? { accepted: false, message: "SetBool transport failed." }
          : serviceResponse(responseEnvelope.values));
      };
      const cancel = () => finish({ accepted: false, message: "ROS bridge changed." });
      const timeout = globalThis.setTimeout(
        () => finish({ accepted: false, message: "SetBool request timed out." }),
        CONTROL_ACK_TIMEOUT_MS,
      );
      pendingServiceCancels.add(cancel);
      connection.on(id, onResponse);
      connection.callOnConnection({
        op: "call_service",
        id,
        service: SET_TOOL_BELIEF_ENABLED_SERVICE,
        args: { data: desired },
        timeout: CONTROL_ACK_TIMEOUT_MS / 1000,
      });
    });
    if (!response.accepted) return response;
    try {
      await waitForStatus(desired);
      return response;
    } catch (error) {
      return {
        accepted: false,
        message: error instanceof Error ? error.message : String(error),
      };
    }
  }

  beliefTopic.subscribe((value) => {
    if (!isCurrent()) return;
    if (enabled === false) return;
    snapshot = reduceToolBeliefSnapshot(snapshot, value);
    publish();
  });
  enabledTopic.subscribe((value) => {
    if (!isCurrent() || !value || typeof value !== "object" || Array.isArray(value)) return;
    const data = (value as Record<string, unknown>).data;
    if (typeof data !== "boolean") return;
    enabled = data;
    if (!enabled) snapshot = null;
    for (const waiter of statusWaiters) {
      if (waiter.desired !== enabled) continue;
      statusWaiters.delete(waiter);
      waiter.resolve();
    }
    publish();
  });
  publish();
  return {
    subscribe: () => {},
    unsubscribe: () => {
      beliefTopic.unsubscribe();
      enabledTopic.unsubscribe();
      for (const cancel of pendingServiceCancels) cancel();
      statusWaiters.clear();
    },
  };
}
