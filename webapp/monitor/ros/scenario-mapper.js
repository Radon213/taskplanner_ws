import { PUBLIC_CONTRACT, PUBLIC_TOPIC_NAMES } from "./public-contract.js";

const SPEECH_STATES = new Set([
  "unavailable",
  "idle",
  "listening",
  "processing",
  "ready",
  "error",
]);

const IN_USE_STATES = new Set(["handed_over", "in_use"]);
const END_EFFECTOR_STATES = new Set(["unknown", "empty", "holding"]);
const IDENTITY_SCOPED_TOPICS = new Set([
  PUBLIC_TOPIC_NAMES.catalog,
  PUBLIC_TOPIC_NAMES.robotEndEffectors,
  PUBLIC_TOPIC_NAMES.toolPredictions,
  PUBLIC_TOPIC_NAMES.speech,
]);
const isRecord = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const asString = (value) => (typeof value === "string" ? value.trim() : "");

function asProbability(value) {
  if (value === "" || value === null || value === undefined || typeof value === "boolean") return null;
  const probability = Number(value);
  return Number.isFinite(probability) && probability >= 0 && probability <= 1
    ? probability
    : null;
}

function uniqueToolDefinitions(ids, catalog) {
  const definitions = [];
  const seen = new Set();
  ids.forEach((id) => {
    if (!id || seen.has(id)) return;
    seen.add(id);
    definitions.push({ id, name: catalog.get(id)?.displayName || id });
  });
  return definitions;
}

function robotStatus(state) {
  const normalized = asString(state).toLowerCase();
  if (["retracting", "changing_tool", "moving_to_standby", "moving_to_target", "executing", "running"].includes(normalized)) {
    return "moving";
  }
  if (normalized === "direct_teach") return "standby";
  if (["fault", "protective_stop", "failed", "offline"].includes(normalized)) return "fault";
  if (["standby", "idle", "completed"].includes(normalized)) return "idle";
  return "unknown";
}

function speechLabel(message) {
  const text = asString(message.text);
  if (text) return text;
  return "Listening...";
}

export class MainLayoutScenarioMapper {
  constructor() {
    this.reset();
  }

  reset() {
    this.lastRejection = null;
    this.seenGateway = false;
    this.gatewayInstanceId = "";
    this.procedureRunId = "";
    this.procedureType = "";
    this.procedureActive = false;
    this.catalogVersion = "";
    this.contractCompatible = false;
    this.phaseCatalog = new Map();
    this.instrumentCatalog = new Map();
    this.currentPhaseId = "";
  }

  reject(topic, reason, details = {}) {
    this.lastRejection = {
      topic: asString(topic),
      reason: asString(reason) || "validation_failed",
      details: isRecord(details) ? { ...details } : {},
    };
    return null;
  }

  getLastRejection() {
    return this.lastRejection ? structuredClone(this.lastRejection) : null;
  }

  clearRunScope() {
    this.currentPhaseId = "";
  }

  clearCatalogScope() {
    this.clearRunScope();
    this.phaseCatalog.clear();
    this.instrumentCatalog.clear();
    this.currentPhaseId = "";
  }

  identityMatchesGateway(message) {
    if (!this.seenGateway) return true;
    return asString(message.gateway_instance_id) === this.gatewayInstanceId
      && asString(message.procedure_run_id) === this.procedureRunId
      && asString(message.catalog_version) === this.catalogVersion
      && asString(message.schema_version) === PUBLIC_CONTRACT.schemaVersion
      && asString(message.procedure_type) === this.procedureType
      && message.procedure_active === this.procedureActive;
  }

  phasePatch(phaseId, uncertain = false) {
    const entry = this.phaseCatalog.get(phaseId);
    const phases = [...this.phaseCatalog.values()]
      .sort((left, right) => left.ordinal - right.ordinal);
    const displayIndex = phases.findIndex((phase) => phase.id === phaseId);
    const stageIsVisible = Boolean(entry && displayIndex >= 0);
    return {
      id: stageIsVisible ? entry.id : "",
      code: stageIsVisible ? entry.id : "—",
      index: stageIsVisible ? displayIndex + 1 : 0,
      total: phases.length,
      name: stageIsVisible
        ? entry.displayNameKo || entry.displayName
        : phaseId
          ? "Waiting for phase data"
          : "Waiting for procedure",
      description: stageIsVisible && entry.displayNameKo ? entry.displayName : "",
      uncertain: Boolean(uncertain),
    };
  }

  map(topic, message) {
    this.lastRejection = null;
    if (!isRecord(message)) return this.reject(topic, "message_not_object");

    // The demonstration bridge loops recorded snapshots and restarts revision
    // values without necessarily changing the gateway identity. Match the
    // handoff viewer by treating arrival order as authoritative inside the
    // validated gateway/run/catalog scope instead of rejecting a new loop as
    // an older snapshot.
    if (topic === PUBLIC_TOPIC_NAMES.gatewayInfo) {
      const gatewayResult = this.mapGatewayInfo(message);
      return gatewayResult || this.reject(topic, "validation_failed");
    }
    if (IDENTITY_SCOPED_TOPICS.has(topic) && !this.identityMatchesGateway(message)) {
      return this.reject(topic, "identity_mismatch", {
        expectedGatewayInstanceId: this.gatewayInstanceId,
        expectedProcedureRunId: this.procedureRunId,
        expectedCatalogVersion: this.catalogVersion,
        expectedProcedureActive: this.procedureActive,
        receivedGatewayInstanceId: asString(message.gateway_instance_id),
        receivedProcedureRunId: asString(message.procedure_run_id),
        receivedCatalogVersion: asString(message.catalog_version),
        receivedProcedureActive: message.procedure_active === true,
      });
    }

    let result = null;
    switch (topic) {
      case PUBLIC_TOPIC_NAMES.catalog:
        result = this.mapCatalog(message);
        break;
      case PUBLIC_TOPIC_NAMES.context:
        result = this.mapContext(message);
        break;
      case PUBLIC_TOPIC_NAMES.instruments:
        result = this.mapInstruments(message);
        break;
      case PUBLIC_TOPIC_NAMES.robots:
        result = this.mapRobots(message);
        break;
      case PUBLIC_TOPIC_NAMES.robotEndEffectors:
        result = this.mapEndEffectors(message);
        break;
      case PUBLIC_TOPIC_NAMES.toolPredictions:
        result = this.mapPredictions(message);
        break;
      case PUBLIC_TOPIC_NAMES.speech:
        result = this.mapSpeech(message);
        break;
      case PUBLIC_TOPIC_NAMES.health:
        result = this.mapHealth(message);
        break;
      default:
        return this.reject(topic, "unsupported_topic");
    }
    return result || this.reject(topic, "validation_failed");
  }

  mapGatewayInfo(message) {
    const nextGatewayInstanceId = asString(message.gateway_instance_id);
    const nextProcedureRunId = asString(message.procedure_run_id);
    const nextProcedureType = asString(message.procedure_type);
    const nextCatalogVersion = asString(message.catalog_version);
    const nextProcedureActive = message.procedure_active === true;
    const firstHeartbeat = !this.seenGateway;
    const gatewayChanged = Boolean(
      this.gatewayInstanceId
      && nextGatewayInstanceId
      && this.gatewayInstanceId !== nextGatewayInstanceId,
    );
    const runChanged = !firstHeartbeat && (
      this.procedureRunId !== nextProcedureRunId
      || this.procedureActive !== nextProcedureActive
    );
    const catalogChanged = !firstHeartbeat && Boolean(
      this.catalogVersion
      && nextCatalogVersion
      && this.catalogVersion !== nextCatalogVersion,
    );

    if (gatewayChanged) this.reset();
    else if (firstHeartbeat) {
      this.clearRunScope();
      this.clearCatalogScope();
    }
    else {
      if (runChanged) this.clearRunScope();
      if (catalogChanged) this.clearCatalogScope();
    }

    this.seenGateway = true;
    this.gatewayInstanceId = nextGatewayInstanceId;
    this.procedureRunId = nextProcedureRunId;
    this.procedureType = nextProcedureType;
    this.procedureActive = nextProcedureActive;
    this.catalogVersion = nextCatalogVersion;
    this.contractCompatible = message.schema_version === PUBLIC_CONTRACT.schemaVersion
      && message.interface_version === PUBLIC_CONTRACT.interfaceVersion;

    const resetScope = gatewayChanged
      ? "gateway"
      : catalogChanged
        ? "catalog"
        : runChanged
          ? "run"
          : firstHeartbeat
            ? "initial"
            : "";
    return {
      patch: null,
      tools: [],
      meta: {
        gatewayHeartbeat: true,
        gatewayInstanceId: this.gatewayInstanceId,
        procedureRunId: this.procedureRunId,
        procedureType: this.procedureType,
        procedureActive: this.procedureActive,
        catalogVersion: this.catalogVersion,
        revision: Number.isFinite(Number(message.revision)) ? Number(message.revision) : null,
        schemaVersion: asString(message.schema_version),
        interfaceVersion: asString(message.interface_version),
        contractCompatible: this.contractCompatible,
        resetScope,
      },
    };
  }

  mapCatalog(message) {
    if (!Array.isArray(message.phases) || !Array.isArray(message.instruments)) return null;
    if (message.phases.length > 64 || message.instruments.length > 256) return null;

    const phases = new Map();
    const phaseOrdinals = new Set();
    for (const rawPhase of message.phases) {
      if (!isRecord(rawPhase)) return null;
      const id = asString(rawPhase.phase_id);
      const ordinal = Number(rawPhase.ordinal);
      if (
        !id
        || !Number.isInteger(ordinal)
        || ordinal < 1
        || ordinal > 64
        || phases.has(id)
        || phaseOrdinals.has(ordinal)
      ) {
        return null;
      }
      phaseOrdinals.add(ordinal);
      phases.set(id, {
        id,
        ordinal,
        displayName: asString(rawPhase.display_name) || id,
        displayNameKo: asString(rawPhase.display_name_ko),
        kind: asString(rawPhase.phase_kind).toLowerCase(),
      });
    }

    const instruments = new Map();
    for (const rawInstrument of message.instruments) {
      if (!isRecord(rawInstrument)) return null;
      const id = asString(rawInstrument.instrument_id);
      if (!id || instruments.has(id)) return null;
      instruments.set(id, {
        id,
        displayName: asString(rawInstrument.display_name) || id,
      });
    }

    this.phaseCatalog = phases;
    this.instrumentCatalog = instruments;
    this.catalogVersion = asString(message.catalog_version) || this.catalogVersion;
    const procedureName = asString(message.procedure_display_name);
    const patch = {};
    if (procedureName) patch.procedure = { name: procedureName };
    patch.phase = this.phasePatch(this.currentPhaseId);
    delete patch.phase.uncertain;

    return {
      patch: Object.keys(patch).length ? patch : null,
      tools: [...instruments.values()].map(({ id, displayName }) => ({ id, name: displayName })),
      meta: { catalogVersion: asString(message.catalog_version) },
    };
  }

  mapContext(message) {
    const active = message.procedure_active === true;
    const phaseId = active ? asString(message.current_phase) : "";
    this.currentPhaseId = phaseId;
    return {
      patch: {
        phase: {
          ...this.phasePatch(phaseId, message.phase_uncertain === true),
          confidence: asProbability(message.phase_confidence),
          executionState: asString(message.execution_state),
          evidenceStatus: asString(message.evidence_status),
          safetyFlags: Array.isArray(message.safety_flags)
            ? message.safety_flags.map(asString).filter(Boolean)
            : [],
        },
      },
      tools: [],
      meta: {
        procedureActive: active,
        executionState: asString(message.execution_state),
      },
    };
  }

  mapInstruments(message) {
    if (!Array.isArray(message.instruments) || message.instruments.length > 512) return null;
    const rows = [];
    const instanceIds = new Set();
    for (const raw of message.instruments) {
      if (!isRecord(raw)) return null;
      const instrumentId = asString(raw.instrument_id);
      const instanceId = asString(raw.instance_id);
      if (!instrumentId || (instanceId && instanceIds.has(instanceId))) return null;
      const confidence = asProbability(raw.confidence);
      if (confidence === null) return null;
      if (instanceId) instanceIds.add(instanceId);
      rows.push({
        instrumentId,
        instanceId,
        holderRole: asString(raw.holder_role).toLowerCase(),
        locationType: asString(raw.location_type).toLowerCase(),
        locationId: asString(raw.location_id),
        state: asString(raw.state).toLowerCase(),
        confidence,
        evidenceStatus: asString(raw.evidence_status),
      });
    }

    const toView = (row) => ({
      toolId: row.instrumentId,
      instanceId: row.instanceId,
      state: row.state,
      confidence: row.confidence,
      evidenceStatus: row.evidenceStatus,
    });
    const inUse = rows
      .filter((row) => row.holderRole === "surgeon" && IN_USE_STATES.has(row.state))
      .map(toView);
    const mayo = rows
      .filter((row) => row.locationType === "mayo_stand")
      .map(toView);
    const ids = rows.map((row) => row.instrumentId);

    return {
      patch: {
        instrumentFlow: { inUse, mayo },
        // Retained as a read-only compatibility summary. The arrays above are authoritative.
        retrieval: {
          retrievedToolId: mayo[0]?.toolId || "none",
          location: "MAYO",
          inUseToolId: inUse[0]?.toolId || "none",
        },
      },
      tools: uniqueToolDefinitions(ids, this.instrumentCatalog),
      meta: {},
    };
  }

  mapRobots(message) {
    if (!Array.isArray(message.robots) || message.robots.length > 32) return null;
    const retractionArms = message.robots
      .filter((robot) => isRecord(robot) && asString(robot.robot_type) === "bed_retraction_arm")
      .sort((left, right) => asString(left.robot_id).localeCompare(asString(right.robot_id)));
    const armPatches = {};
    [3, 4].forEach((armNumber, index) => {
      const robot = retractionArms[index];
      armPatches[armNumber] = {
        status: robot ? robotStatus(robot.execution_state) : "unknown",
        toolName: armNumber === 3 ? "Retraction" : "Suction",
      };
    });
    return {
      patch: { arms: armPatches },
      tools: [],
      meta: {},
    };
  }

  mapEndEffectors(message) {
    if (!Array.isArray(message.end_effectors) || message.end_effectors.length > 16) return null;
    if (message.procedure_active !== true && message.end_effectors.length) return null;
    const patches = {};
    const toolIds = [];
    const byHand = new Map();
    for (const raw of message.end_effectors) {
      if (!isRecord(raw)) return null;
      const robotId = asString(raw.robot_id);
      const hand = asString(raw.end_effector_id);
      const possessionState = asString(raw.state).toLowerCase();
      const confidence = asProbability(raw.confidence);
      if (!robotId || !hand || !END_EFFECTOR_STATES.has(possessionState) || confidence === null) {
        return null;
      }
      const instrumentId = possessionState === "holding" ? asString(raw.instrument_id) : "";
      if (possessionState === "holding" && !instrumentId) return null;
      if (robotId !== "humanoid" || (hand !== "left_hand" && hand !== "right_hand")) continue;
      if (byHand.has(hand)) return null;
      byHand.set(hand, {
        possessionState,
        instrumentId,
        instanceId: possessionState === "holding" ? asString(raw.instance_id) : "",
        confidence,
        evidenceStatus: asString(raw.evidence_status),
      });
    }

    [["left_hand", 1], ["right_hand", 2]].forEach(([hand, armNumber]) => {
      const raw = byHand.get(hand);
      const possessionState = raw?.possessionState || "unknown";
      const instrumentId = raw?.instrumentId || "";
      if (instrumentId) toolIds.push(instrumentId);
      patches[armNumber] = {
        status: possessionState,
        toolId: instrumentId || "none",
        instanceId: raw?.instanceId || "",
        confidence: raw?.confidence ?? 0,
        evidenceStatus: raw?.evidenceStatus || "",
      };
    });

    return {
      patch: { arms: patches },
      tools: uniqueToolDefinitions(toolIds, this.instrumentCatalog),
      meta: { procedureActive: message.procedure_active === true },
    };
  }

  mapPredictions(message) {
    if (!Array.isArray(message.predictions) || message.predictions.length > 3) return null;
    const predictions = [];
    for (const raw of message.predictions) {
      if (!isRecord(raw)) return null;
      const rank = Number(raw.rank);
      const instrumentId = asString(raw.instrument_id);
      const confidence = asProbability(raw.confidence);
      if (!Number.isInteger(rank) || rank < 1 || rank > 3 || !instrumentId || confidence === null) {
        return null;
      }
      predictions.push({ rank, toolId: instrumentId, confidence: confidence * 100 });
    }
    predictions.sort((left, right) => left.rank - right.rank);
    if (predictions.some((prediction, index) => prediction.rank !== index + 1)) return null;

    return {
      patch: { predictions },
      tools: uniqueToolDefinitions(predictions.map((prediction) => prediction.toolId), this.instrumentCatalog),
      meta: { procedureActive: message.procedure_active === true },
    };
  }

  mapSpeech(message) {
    const candidateState = asString(message.state).toLowerCase();
    const validState = SPEECH_STATES.has(candidateState) ? candidateState : "unavailable";
    const state = validState === "listening" && (message.available !== true || message.connected !== true)
      ? "unavailable"
      : validState;
    return {
      patch: { voice: { status: state, text: speechLabel(message) } },
      tools: [],
      meta: { procedureActive: message.procedure_active === true },
    };
  }

  mapHealth(message) {
    const unavailableSources = Array.isArray(message.unavailable_sources)
      ? message.unavailable_sources.map(asString).filter(Boolean)
      : [];
    const staleSources = Array.isArray(message.stale_sources)
      ? message.stale_sources.map(asString).filter(Boolean)
      : [];
    const errorCodes = Array.isArray(message.error_codes)
      ? message.error_codes.map(asString).filter(Boolean)
      : [];
    return {
      patch: null,
      tools: [],
      meta: {
        health: {
          healthy: message.healthy === true,
          state: asString(message.state).toLowerCase(),
          unavailableSources,
          staleSources,
          errorCodes,
        },
      },
    };
  }
}
