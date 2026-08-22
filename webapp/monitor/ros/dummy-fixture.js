import { PUBLIC_CONTRACT, PUBLIC_TOPIC_NAMES } from "./public-contract.js";
import { MainLayoutScenarioMapper } from "./scenario-mapper.js";
import { validateDummyFixture } from "../runtime-settings.js";

const REPLAY_TOPICS = Object.freeze([
  PUBLIC_TOPIC_NAMES.health,
  PUBLIC_TOPIC_NAMES.context,
  PUBLIC_TOPIC_NAMES.instruments,
  PUBLIC_TOPIC_NAMES.robots,
  PUBLIC_TOPIC_NAMES.robotEndEffectors,
  PUBLIC_TOPIC_NAMES.toolPredictions,
  PUBLIC_TOPIC_NAMES.speech,
]);
const IDENTITY_TOPICS = new Set([
  PUBLIC_TOPIC_NAMES.catalog,
  PUBLIC_TOPIC_NAMES.robotEndEffectors,
  PUBLIC_TOPIC_NAMES.toolPredictions,
  PUBLIC_TOPIC_NAMES.speech,
]);
const RESERVED_IDS = new Set(["__proto__", "prototype", "constructor"]);

function mergePatch(target, source) {
  Object.entries(source || {}).forEach(([key, value]) => {
    if (RESERVED_IDS.has(key)) return;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      target[key] = mergePatch({ ...(target[key] || {}) }, value);
    } else {
      target[key] = value;
    }
  });
  return target;
}

function fixtureIdentity(fixture) {
  const baseline = fixture.baseline;
  const gateway = fixture[PUBLIC_TOPIC_NAMES.gatewayInfo] || {};
  const context = fixture[PUBLIC_TOPIC_NAMES.context];
  if (typeof context.procedure_active !== "boolean") {
    throw new Error("/surgery/context.procedure_active는 boolean이어야 합니다.");
  }
  const identity = {
    gateway_instance_id: String(gateway.gateway_instance_id || "dummy-fixture-gateway"),
    procedure_run_id: String(gateway.procedure_run_id || "dummy-fixture-run"),
    procedure_type: String(baseline.procedure_type),
    procedure_active: context.procedure_active,
    catalog_version: String(baseline.catalog_version),
    schema_version: String(baseline.schema_version),
    interface_version: String(baseline.interface_version),
  };
  if (!identity.gateway_instance_id || !identity.procedure_run_id) {
    throw new Error("더미 gateway/run identity가 필요합니다.");
  }
  const stringFields = [
    "gateway_instance_id",
    "procedure_run_id",
    "procedure_type",
    "catalog_version",
    "schema_version",
    "interface_version",
  ];
  stringFields.forEach((field) => {
    const raw = gateway[field];
    if (raw !== undefined && String(raw) !== identity[field]) {
      throw new Error(`/surgery/gateway_info.${field}가 baseline과 일치하지 않습니다.`);
    }
  });
  if (
    gateway.procedure_active !== undefined
    && gateway.procedure_active !== identity.procedure_active
  ) {
    throw new Error("/surgery/gateway_info.procedure_active가 context와 일치하지 않습니다.");
  }
  return identity;
}

function scopedMessage(topic, raw, identity) {
  if (!IDENTITY_TOPICS.has(topic)) return structuredClone(raw);
  const message = structuredClone(raw);
  for (const field of [
    "gateway_instance_id",
    "procedure_run_id",
    "procedure_type",
    "catalog_version",
    "schema_version",
  ]) {
    if (message[field] !== undefined && String(message[field]) !== identity[field]) {
      throw new Error(`${topic}.${field}가 fixture baseline과 일치하지 않습니다.`);
    }
    message[field] = identity[field];
  }
  if (
    message.procedure_active !== undefined
    && message.procedure_active !== identity.procedure_active
  ) {
    throw new Error(`${topic}.procedure_active가 context와 일치하지 않습니다.`);
  }
  message.procedure_active = identity.procedure_active;
  return message;
}

function compatibleFallbackCatalog(fixture, fallbackFixture) {
  if (fixture[PUBLIC_TOPIC_NAMES.catalog]) return null;
  const fallbackCatalog = fallbackFixture?.[PUBLIC_TOPIC_NAMES.catalog];
  if (!fallbackCatalog) return null;
  const baseline = fixture.baseline;
  const fallbackBaseline = fallbackFixture.baseline;
  if (
    baseline.procedure_type !== fallbackBaseline.procedure_type
    || baseline.catalog_version !== fallbackBaseline.catalog_version
  ) {
    return null;
  }
  const catalog = structuredClone(fallbackCatalog);
  for (const field of [
    "gateway_instance_id",
    "procedure_run_id",
    "procedure_type",
    "procedure_active",
    "catalog_version",
    "schema_version",
    "interface_version",
  ]) delete catalog[field];
  return catalog;
}

function collectResult(result, topic, patch, tools) {
  if (!result) throw new Error(`${topic} snapshot이 공개 계약과 맞지 않습니다.`);
  mergePatch(patch, result.patch);
  result.tools.forEach((tool) => {
    const id = String(tool.id || "");
    if (!id || id.length > 96 || RESERVED_IDS.has(id)) {
      throw new Error(`${topic}에 안전하지 않은 도구 ID가 있습니다.`);
    }
    tools.set(id, { id, name: String(tool.name || id).slice(0, 160) });
  });
}

export function replayDummyFixture(candidate, { fallbackFixture = null } = {}) {
  const fixture = validateDummyFixture(candidate);
  const fallback = fallbackFixture ? validateDummyFixture(fallbackFixture) : null;
  const identity = fixtureIdentity(fixture);
  const mapper = new MainLayoutScenarioMapper();
  const patch = {};
  const tools = new Map();
  const replayedTopics = [];

  const gatewayMessage = {
    ...structuredClone(fixture[PUBLIC_TOPIC_NAMES.gatewayInfo] || {}),
    revision: Number(fixture[PUBLIC_TOPIC_NAMES.gatewayInfo]?.revision) || 0,
    ...identity,
  };
  const gatewayResult = mapper.map(PUBLIC_TOPIC_NAMES.gatewayInfo, gatewayMessage);
  if (!gatewayResult?.meta?.contractCompatible) {
    throw new Error(
      `Gateway 계약 버전은 schema ${PUBLIC_CONTRACT.schemaVersion}, interface ${PUBLIC_CONTRACT.interfaceVersion}이어야 합니다.`,
    );
  }
  replayedTopics.push(PUBLIC_TOPIC_NAMES.gatewayInfo);

  const rawCatalog = fixture[PUBLIC_TOPIC_NAMES.catalog]
    || compatibleFallbackCatalog(fixture, fallback);
  if (!rawCatalog) {
    throw new Error("동일 procedure_type/catalog_version의 /surgery/catalog snapshot이 필요합니다.");
  }
  const catalogResult = mapper.map(
    PUBLIC_TOPIC_NAMES.catalog,
    scopedMessage(PUBLIC_TOPIC_NAMES.catalog, rawCatalog, identity),
  );
  collectResult(catalogResult, PUBLIC_TOPIC_NAMES.catalog, patch, tools);
  replayedTopics.push(PUBLIC_TOPIC_NAMES.catalog);

  for (const topic of REPLAY_TOPICS) {
    const raw = fixture[topic];
    if (!raw) continue;
    const message = scopedMessage(topic, raw, identity);
    const result = mapper.map(topic, message);
    collectResult(result, topic, patch, tools);
    replayedTopics.push(topic);
  }

  return Object.freeze({
    patch: structuredClone(patch),
    tools: [...tools.values()].map((tool) => ({ ...tool })),
    meta: Object.freeze({
      replayedTopics: Object.freeze([...replayedTopics]),
      catalogSource: fixture[PUBLIC_TOPIC_NAMES.catalog] ? "fixture" : "bundled",
      procedureType: identity.procedure_type,
    }),
  });
}
