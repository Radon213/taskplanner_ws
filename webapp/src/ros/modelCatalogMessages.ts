import type {
  ModelCatalogEntry,
  ModelProviderStatus,
  ModelRuntimeCommand,
  ModelSelection,
} from "../types";
import {
  isBoundedRosPayload,
  MAX_ROS_PAYLOAD_COLLECTION_ITEMS,
  MAX_ROS_PAYLOAD_STRING_CHARS,
} from "./rosMessageBounds";

const MAX_PROVIDER_ID_CHARS = 512;
const MAX_PROVIDER_NAME_CHARS = 512;
const MAX_MODEL_ID_CHARS = 512;
const MAX_MODEL_DISPLAY_NAME_CHARS = 1_024;
const MAX_ENDPOINT_CHARS = 2_048;
const MAX_STATUS_CHARS = 128;
const MAX_CAPABILITY_CHARS = 128;
const MAX_DETAIL_CHARS = 4_096;
const MAX_MESSAGE_CHARS = 4_096;
const MAX_PARAMETER_NAME_CHARS = 256;
const MAX_UINT32 = 0xffff_ffff;

export const ROS_PARAMETER_BOOL = 1 as const;
export const ROS_PARAMETER_STRING = 4 as const;

const MODEL_RUNTIME_COMMANDS = new Set<ModelRuntimeCommand>([
  "load",
  "unload",
  "sleep",
  "wake",
]);

export type ModelCatalogProjection = {
  providers: ModelProviderStatus[];
  models: ModelCatalogEntry[];
  selection: ModelSelection | null;
  status: string;
};

export type RosStringParameter = {
  name: string;
  value: {
    type: typeof ROS_PARAMETER_STRING;
    string_value: string;
  };
};

export type RosBoolParameter = {
  name: string;
  value: {
    type: typeof ROS_PARAMETER_BOOL;
    bool_value: boolean;
  };
};

export type RosParameter = RosStringParameter | RosBoolParameter;

function record(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function boundedText(
  value: unknown,
  maxChars: number,
  { allowEmpty = true }: { allowEmpty?: boolean } = {},
): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  if ((!allowEmpty && !normalized) || normalized.length > maxChars) return null;
  return normalized;
}

function displayText(value: unknown, fallback: string, maxChars: number): string {
  if (typeof value !== "string" || value.length === 0) return fallback;
  return value.slice(0, maxChars);
}

function finiteNonNegativeNumber(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  return value;
}

function uint32(value: unknown): number | null {
  if (
    typeof value !== "number"
    || !Number.isInteger(value)
    || value < 0
    || value > MAX_UINT32
  ) {
    return null;
  }
  return value;
}

/** Normalize one typed provider row without granting it runtime authority. */
export function normalizeModelProviderStatus(
  value: unknown,
): ModelProviderStatus | null {
  const row = record(value);
  if (!row) return null;
  const providerId = boundedText(
    row.provider_id,
    MAX_PROVIDER_ID_CHARS,
    { allowEmpty: false },
  );
  const providerName = row.provider_name === undefined || row.provider_name === ""
    ? providerId
    : boundedText(row.provider_name, MAX_PROVIDER_NAME_CHARS, { allowEmpty: false });
  const endpoint = boundedText(row.endpoint ?? "", MAX_ENDPOINT_CHARS);
  const status = boundedText(row.status ?? "", MAX_STATUS_CHARS);
  const detail = boundedText(row.detail ?? "", MAX_DETAIL_CHARS);
  const latencySec = finiteNonNegativeNumber(row.latency_sec ?? 0);
  const modelCount = uint32(row.model_count ?? 0);
  if (
    !providerId
    || !providerName
    || endpoint === null
    || typeof row.reachable !== "boolean"
    || status === null
    || detail === null
    || latencySec === null
    || modelCount === null
  ) {
    return null;
  }
  return {
    provider_id: providerId,
    provider_name: providerName,
    endpoint,
    reachable: row.reachable,
    status,
    detail,
    latency_sec: latencySec,
    model_count: modelCount,
  };
}

/** Normalize one typed model row while ignoring unknown future fields. */
export function normalizeModelCatalogEntry(
  value: unknown,
): ModelCatalogEntry | null {
  const row = record(value);
  if (!row) return null;
  const providerId = boundedText(
    row.provider_id,
    MAX_PROVIDER_ID_CHARS,
    { allowEmpty: false },
  );
  const modelId = boundedText(
    row.model_id,
    MAX_MODEL_ID_CHARS,
    { allowEmpty: false },
  );
  const providerName = row.provider_name === undefined || row.provider_name === ""
    ? providerId
    : boundedText(row.provider_name, MAX_PROVIDER_NAME_CHARS, { allowEmpty: false });
  const displayName = row.display_name === undefined || row.display_name === ""
    ? modelId
    : boundedText(row.display_name, MAX_MODEL_DISPLAY_NAME_CHARS, { allowEmpty: false });
  const capability = boundedText(row.capability ?? "unknown", MAX_CAPABILITY_CHARS);
  const loadState = boundedText(row.load_state ?? "unknown", MAX_STATUS_CHARS);
  const detail = boundedText(row.detail ?? "", MAX_DETAIL_CHARS);
  if (
    !providerId
    || !modelId
    || !providerName
    || !displayName
    || capability === null
    || loadState === null
    || detail === null
    || (row.selectable !== undefined && typeof row.selectable !== "boolean")
    || (row.runtime_managed !== undefined && typeof row.runtime_managed !== "boolean")
  ) {
    return null;
  }
  const availableActions = Array.isArray(row.available_actions)
    ? row.available_actions
      .slice(0, MAX_ROS_PAYLOAD_COLLECTION_ITEMS)
      .filter(
        (command): command is ModelRuntimeCommand =>
          typeof command === "string"
          && MODEL_RUNTIME_COMMANDS.has(command as ModelRuntimeCommand),
      )
    : [];
  return {
    provider_id: providerId,
    provider_name: providerName,
    model_id: modelId,
    display_name: displayName,
    capability,
    load_state: loadState,
    selectable: row.selectable === undefined || row.selectable === true,
    detail,
    runtime_managed: row.runtime_managed === true,
    available_actions: availableActions,
  };
}

function responseRecord(response: unknown, contract: string): Record<string, unknown> {
  const payload = record(response);
  if (!payload || !isBoundedRosPayload(payload)) {
    throw new Error(`${contract} response exceeded the UI payload bound or was malformed.`);
  }
  if (typeof payload.success !== "boolean") {
    throw new Error(`${contract} response success field was invalid.`);
  }
  if (!payload.success) {
    throw new Error(displayText(payload.message, `${contract} unavailable.`, MAX_MESSAGE_CHARS));
  }
  return payload;
}

/** Parse the modern multi-provider ListModelCatalog response. */
export function parseModelCatalogResponse(response: unknown): ModelCatalogProjection {
  const payload = responseRecord(response, "Model catalog");
  if (!Array.isArray(payload.providers) || !Array.isArray(payload.models)) {
    throw new Error("Model catalog response collections were invalid.");
  }
  const providers = payload.providers
    .slice(0, MAX_ROS_PAYLOAD_COLLECTION_ITEMS)
    .map(normalizeModelProviderStatus)
    .filter((row): row is ModelProviderStatus => row !== null);
  const models = payload.models
    .slice(0, MAX_ROS_PAYLOAD_COLLECTION_ITEMS)
    .map(normalizeModelCatalogEntry)
    .filter((row): row is ModelCatalogEntry => row !== null);
  const activeProviderId = boundedText(payload.active_provider_id ?? "", MAX_PROVIDER_ID_CHARS);
  const activeModelId = boundedText(payload.active_model_id ?? "", MAX_MODEL_ID_CHARS);
  if (activeProviderId === null || activeModelId === null) {
    throw new Error("Model catalog active selection was invalid.");
  }
  return {
    providers,
    models,
    selection: activeProviderId && activeModelId
      ? { provider_id: activeProviderId, model_id: activeModelId }
      : null,
    status: displayText(
      payload.message,
      models.length ? "connected" : "empty",
      MAX_MESSAGE_CHARS,
    ),
  };
}

/** Parse the legacy single-provider ListModels response into the same projection. */
export function parseLegacyModelCatalogResponse(
  response: unknown,
  providerName = "OpenAI compatible",
): ModelCatalogProjection {
  const payload = responseRecord(response, "Legacy model catalog");
  if (!Array.isArray(payload.model_ids)) {
    throw new Error("Legacy model catalog response model_ids field was invalid.");
  }
  const normalizedProviderName = boundedText(
    providerName,
    MAX_PROVIDER_NAME_CHARS,
    { allowEmpty: false },
  );
  if (!normalizedProviderName) {
    throw new Error("Legacy model catalog provider name was invalid.");
  }
  const modelIds = payload.model_ids
    .slice(0, MAX_ROS_PAYLOAD_COLLECTION_ITEMS)
    .map((modelId) => boundedText(modelId, MAX_MODEL_ID_CHARS, { allowEmpty: false }))
    .filter((modelId): modelId is string => modelId !== null);
  const provider: ModelProviderStatus = {
    provider_id: "legacy",
    provider_name: normalizedProviderName,
    endpoint: "",
    reachable: true,
    status: "online",
    detail: "Legacy single-provider catalog",
    latency_sec: 0,
    model_count: modelIds.length,
  };
  const models: ModelCatalogEntry[] = modelIds.map((modelId) => ({
    provider_id: provider.provider_id,
    provider_name: provider.provider_name,
    model_id: modelId,
    display_name: modelId,
    capability: "unknown",
    load_state: "unknown",
    selectable: true,
    detail: "",
    runtime_managed: false,
    available_actions: [],
  }));
  return {
    providers: [provider],
    models,
    selection: modelIds[0]
      ? { provider_id: provider.provider_id, model_id: modelIds[0] }
      : null,
    status: displayText(
      payload.message,
      modelIds.length ? "connected" : "empty",
      MAX_MESSAGE_CHARS,
    ),
  };
}

function parameterName(name: string): string {
  const normalized = boundedText(name, MAX_PARAMETER_NAME_CHARS, { allowEmpty: false });
  if (!normalized) throw new Error("ROS parameter name was invalid.");
  return normalized;
}

export function stringParameter(name: string, value: string): RosStringParameter {
  if (typeof value !== "string" || value.length > MAX_ROS_PAYLOAD_STRING_CHARS) {
    throw new Error("ROS string parameter value was invalid.");
  }
  return {
    name: parameterName(name),
    value: {
      type: ROS_PARAMETER_STRING,
      string_value: value,
    },
  };
}

export function boolParameter(name: string, value: boolean): RosBoolParameter {
  if (typeof value !== "boolean") {
    throw new Error("ROS boolean parameter value was invalid.");
  }
  return {
    name: parameterName(name),
    value: {
      type: ROS_PARAMETER_BOOL,
      bool_value: value,
    },
  };
}

export function setParametersRequest(parameters: readonly RosParameter[]) {
  if (
    parameters.length === 0
    || parameters.length > MAX_ROS_PAYLOAD_COLLECTION_ITEMS
    || !isBoundedRosPayload(parameters)
  ) {
    throw new Error("ROS parameter request was empty, oversized, or malformed.");
  }
  return { parameters: [...parameters] };
}

/**
 * Validate one SetParameters response. Transport freshness, cancellation, and
 * node ownership remain the caller's responsibility.
 */
export function assertSetParametersAccepted(
  response: unknown,
  expectedCount?: number,
): void {
  const payload = record(response);
  if (!payload || !isBoundedRosPayload(payload) || !Array.isArray(payload.results)) {
    throw new Error("SetParameters response was malformed.");
  }
  if (
    expectedCount !== undefined
    && (
      !Number.isInteger(expectedCount)
      || expectedCount < 0
      || payload.results.length !== expectedCount
    )
  ) {
    throw new Error("SetParameters response result count did not match the request.");
  }
  for (const result of payload.results) {
    const row = record(result);
    if (!row || typeof row.successful !== "boolean") {
      throw new Error("SetParameters response contained an invalid result.");
    }
    if (!row.successful) {
      throw new Error(displayText(row.reason, "parameter update rejected", MAX_DETAIL_CHARS));
    }
  }
}
