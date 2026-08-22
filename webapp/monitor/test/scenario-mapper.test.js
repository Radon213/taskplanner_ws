import test from "node:test";
import assert from "node:assert/strict";

import { PUBLIC_TOPIC_NAMES } from "../ros/public-contract.js";
import { MainLayoutScenarioMapper } from "../ros/scenario-mapper.js";

function applyScenarioResult(uiState, result) {
  if (!result?.patch) return uiState;
  Object.assign(uiState, structuredClone(result.patch));
  return uiState;
}

function catalogMessage(revision = 1) {
  return {
    revision,
    catalog_version: "catalog-1",
    procedure_display_name: "Open Thyroidectomy Demonstration",
    phases: [
      { ordinal: 1, phase_id: "P01", display_name: "Preparation", phase_kind: "normal" },
      { ordinal: 2, phase_id: "P02", display_name: "Incision", phase_kind: "normal" },
      { ordinal: 3, phase_id: "P03", display_name: "Central-field dissection before fixed retraction", phase_kind: "normal" },
    ],
    instruments: [
      { instrument_id: "T02", display_name: "Adson forceps" },
      { instrument_id: "T07", display_name: "Bipolar cautery" },
      { instrument_id: "T08", display_name: "Mosquito forceps" },
    ],
  };
}

function gatewayMessage({ revision, procedureActive, procedureRunId }) {
  return {
    revision,
    schema_version: "1.1.0",
    interface_version: "0.3.0",
    catalog_version: "catalog-1",
    gateway_instance_id: "gateway-replay",
    procedure_run_id: procedureRunId,
    procedure_type: "thyroidectomy_demo",
    procedure_active: procedureActive,
  };
}

function replayIdentity({ procedureActive = true, procedureRunId = "run-replay" } = {}) {
  return {
    schema_version: "1.1.0",
    catalog_version: "catalog-1",
    gateway_instance_id: "gateway-replay",
    procedure_run_id: procedureRunId,
    procedure_type: "thyroidectomy_demo",
    procedure_active: procedureActive,
  };
}

test("valid topic snapshots are applied in arrival order when replay revisions decrease", () => {
  const cases = [
    [PUBLIC_TOPIC_NAMES.catalog, (revision) => catalogMessage(revision)],
    [PUBLIC_TOPIC_NAMES.context, (revision) => ({
      revision,
      procedure_active: true,
      current_phase: "P01",
    })],
    [PUBLIC_TOPIC_NAMES.instruments, (revision) => ({ revision, instruments: [] })],
    [PUBLIC_TOPIC_NAMES.robots, (revision) => ({ revision, robots: [] })],
    [PUBLIC_TOPIC_NAMES.robotEndEffectors, (revision) => ({
      revision,
      procedure_active: true,
      end_effectors: [],
    })],
    [PUBLIC_TOPIC_NAMES.toolPredictions, (revision) => ({
      revision,
      procedure_active: true,
      predictions: [],
    })],
    [PUBLIC_TOPIC_NAMES.speech, (revision) => ({
      revision,
      procedure_active: true,
      available: true,
      connected: true,
      state: "listening",
      text: "",
    })],
    [PUBLIC_TOPIC_NAMES.health, (revision) => ({
      revision,
      healthy: true,
      state: "ok",
      unavailable_sources: [],
      stale_sources: [],
      error_codes: [],
    })],
  ];

  cases.forEach(([topic, messageForRevision]) => {
    const mapper = new MainLayoutScenarioMapper();
    assert.notEqual(mapper.map(topic, messageForRevision(100)), null, `${topic} high revision`);
    assert.notEqual(mapper.map(topic, messageForRevision(1)), null, `${topic} replay revision`);
  });
});

test("catalog and context join phase IDs to authoritative display metadata", () => {
  const mapper = new MainLayoutScenarioMapper();
  const catalog = mapper.map(PUBLIC_TOPIC_NAMES.catalog, catalogMessage());
  assert.equal(catalog.patch.procedure.name, "Open Thyroidectomy Demonstration");
  assert.deepEqual(catalog.tools[0], { id: "T02", name: "Adson forceps" });

  const context = mapper.map(PUBLIC_TOPIC_NAMES.context, {
    revision: 2,
    procedure_active: true,
    current_phase: "P03",
    phase_uncertain: false,
    execution_state: "running",
  });
  assert.deepEqual({
    code: context.patch.phase.code,
    index: context.patch.phase.index,
    total: context.patch.phase.total,
    name: context.patch.phase.name,
    description: context.patch.phase.description,
  }, {
    code: "P03",
    index: 3,
    total: 3,
    name: "Central-field dissection before fixed retraction",
    description: "",
  });
});

test("bilingual phase labels use Korean titles, English descriptions, and a safe fallback", () => {
  const mapper = new MainLayoutScenarioMapper();
  mapper.map(PUBLIC_TOPIC_NAMES.catalog, {
    revision: 1,
    catalog_version: "catalog-bilingual",
    procedure_display_name: "Open Thyroidectomy Demonstration",
    phases: [
      {
        ordinal: 1,
        phase_id: "P01",
        display_name: "Patient positioning and operative-field preparation",
        display_name_ko: "환자 체위 및 수술부위 준비",
        phase_kind: "normal",
      },
      {
        ordinal: 2,
        phase_id: "P02",
        display_name: "Skin incision and flap elevation",
        display_name_ko: "   ",
        phase_kind: "normal",
      },
    ],
    instruments: [],
  });

  const bilingual = mapper.map(PUBLIC_TOPIC_NAMES.context, {
    revision: 1,
    procedure_active: true,
    current_phase: "P01",
  });
  assert.equal(bilingual.patch.phase.name, "환자 체위 및 수술부위 준비");
  assert.equal(
    bilingual.patch.phase.description,
    "Patient positioning and operative-field preparation",
  );

  const fallback = mapper.map(PUBLIC_TOPIC_NAMES.context, {
    revision: 2,
    procedure_active: true,
    current_phase: "P02",
  });
  assert.equal(fallback.patch.phase.name, "Skin incision and flap elevation");
  assert.equal(fallback.patch.phase.description, "");
});

test("a ten-phase catalog controls count, ordinal order, and display names", () => {
  const mapper = new MainLayoutScenarioMapper();
  const phases = Array.from({ length: 10 }, (_, index) => ({
    ordinal: ((index + 4) % 10) + 1,
    phase_id: `P${String(index + 1).padStart(2, "0")}`,
    display_name: `Catalog display ${index + 1}`,
    phase_kind: index === 3 ? "interrupt" : "normal",
  })).reverse();
  mapper.map(PUBLIC_TOPIC_NAMES.catalog, {
    revision: 1,
    catalog_version: "catalog-ten",
    procedure_display_name: "Ten-phase procedure",
    phases,
    instruments: [],
  });

  [...phases].sort((left, right) => left.ordinal - right.ordinal).forEach((expected, index) => {
    const result = mapper.map(PUBLIC_TOPIC_NAMES.context, {
      revision: index + 1,
      procedure_active: true,
      current_phase: expected.phase_id,
      phase_confidence: 0.8,
      phase_uncertain: false,
      execution_state: "running",
    });
    assert.deepEqual({
      code: result.patch.phase.code,
      index: result.patch.phase.index,
      total: result.patch.phase.total,
      name: result.patch.phase.name,
    }, {
      code: expected.phase_id,
      index: index + 1,
      total: 10,
      name: expected.display_name,
    });
  });
});

test("context before catalog and unknown phase IDs stay neutral until the catalog resolves them", () => {
  const mapper = new MainLayoutScenarioMapper();
  const beforeCatalog = mapper.map(PUBLIC_TOPIC_NAMES.context, {
    revision: 1,
    procedure_active: true,
    current_phase: "P02",
  });
  assert.equal(beforeCatalog.patch.phase.code, "—");
  assert.equal(beforeCatalog.patch.phase.index, 0);
  assert.equal(beforeCatalog.patch.phase.total, 0);
  assert.match(beforeCatalog.patch.phase.name, /waiting/i);

  const catalog = mapper.map(PUBLIC_TOPIC_NAMES.catalog, {
    revision: 1,
    catalog_version: "catalog-late",
    procedure_display_name: "Server Procedure Name",
    phases: [
      { ordinal: 2, phase_id: "P02", display_name: "Catalog-resolved phase", phase_kind: "normal" },
      { ordinal: 1, phase_id: "P01", display_name: "First phase", phase_kind: "normal" },
    ],
    instruments: [],
  });
  assert.equal(catalog.patch.phase.code, "P02");
  assert.equal(catalog.patch.phase.index, 2);
  assert.equal(catalog.patch.phase.total, 2);
  assert.equal(catalog.patch.phase.name, "Catalog-resolved phase");

  const unknown = mapper.map(PUBLIC_TOPIC_NAMES.context, {
    revision: 2,
    procedure_active: true,
    current_phase: "P99",
  });
  assert.equal(unknown.patch.phase.code, "—");
  assert.equal(unknown.patch.phase.index, 0);
  assert.equal(unknown.patch.phase.total, 2);
  assert.match(unknown.patch.phase.name, /(?:waiting|unknown)/i);

  const empty = mapper.map(PUBLIC_TOPIC_NAMES.context, {
    revision: 3,
    procedure_active: true,
    current_phase: "",
  });
  assert.equal(empty.patch.phase.code, "—");
  assert.equal(empty.patch.phase.index, 0);
  assert.equal(empty.patch.phase.total, 2);
  assert.match(empty.patch.phase.name, /waiting/i);
});

test("a catalog refresh immediately remaps the current phase name and ordinal", () => {
  const mapper = new MainLayoutScenarioMapper();
  mapper.map(PUBLIC_TOPIC_NAMES.catalog, {
    revision: 1,
    catalog_version: "catalog-v1",
    procedure_display_name: "Procedure v1",
    phases: [
      { ordinal: 1, phase_id: "P01", display_name: "First", phase_kind: "normal" },
      { ordinal: 2, phase_id: "P02", display_name: "Original current phase", phase_kind: "normal" },
      { ordinal: 3, phase_id: "P03", display_name: "Third", phase_kind: "normal" },
    ],
    instruments: [],
  });
  const context = mapper.map(PUBLIC_TOPIC_NAMES.context, {
    revision: 1,
    procedure_active: true,
    current_phase: "P02",
  });
  assert.equal(context.patch.phase.index, 2);
  assert.equal(context.patch.phase.name, "Original current phase");

  const refreshed = mapper.map(PUBLIC_TOPIC_NAMES.catalog, {
    revision: 2,
    catalog_version: "catalog-v2",
    procedure_display_name: "Procedure v2",
    phases: [
      { ordinal: 3, phase_id: "P01", display_name: "First moved", phase_kind: "normal" },
      { ordinal: 1, phase_id: "P02", display_name: "Updated current phase", phase_kind: "normal" },
      { ordinal: 2, phase_id: "P03", display_name: "Third moved", phase_kind: "normal" },
    ],
    instruments: [],
  });
  assert.equal(refreshed.patch.phase.code, "P02");
  assert.equal(refreshed.patch.phase.index, 1);
  assert.equal(refreshed.patch.phase.total, 3);
  assert.equal(refreshed.patch.phase.name, "Updated current phase");
});

test("duplicate or invalid catalog ordinals are rejected without corrupting the last valid catalog", () => {
  const mapper = new MainLayoutScenarioMapper();
  mapper.map(PUBLIC_TOPIC_NAMES.catalog, catalogMessage(1));
  const current = mapper.map(PUBLIC_TOPIC_NAMES.context, {
    revision: 1,
    procedure_active: true,
    current_phase: "P02",
  });
  assert.equal(current.patch.phase.index, 2);
  assert.equal(current.patch.phase.name, "Incision");

  for (const [revision, badOrdinal] of [[2, 1], [3, 0], [4, 1.5]]) {
    const phases = [
      { ordinal: 1, phase_id: "P01", display_name: "Bad first", phase_kind: "normal" },
      { ordinal: badOrdinal, phase_id: "P02", display_name: "Bad second", phase_kind: "normal" },
    ];
    const rejected = mapper.map(PUBLIC_TOPIC_NAMES.catalog, {
      revision,
      catalog_version: `bad-catalog-${revision}`,
      procedure_display_name: "Bad catalog",
      phases,
      instruments: [],
    });
    assert.equal(rejected, null);

    const preserved = mapper.map(PUBLIC_TOPIC_NAMES.context, {
      revision: revision + 1,
      procedure_active: true,
      current_phase: "P02",
    });
    assert.equal(preserved.patch.phase.code, "P02");
    assert.equal(preserved.patch.phase.index, 2);
    assert.equal(preserved.patch.phase.total, 3);
    assert.equal(preserved.patch.phase.name, "Incision");
  }
});

test("an invalid high catalog revision does not block a later valid replay snapshot", () => {
  const mapper = new MainLayoutScenarioMapper();
  assert.notEqual(mapper.map(PUBLIC_TOPIC_NAMES.catalog, catalogMessage(1)), null);
  mapper.map(PUBLIC_TOPIC_NAMES.context, {
    revision: 1,
    procedure_active: true,
    current_phase: "P02",
  });

  const rejected = mapper.map(PUBLIC_TOPIC_NAMES.catalog, {
    revision: 100,
    catalog_version: "invalid-high-revision",
    procedure_display_name: "Must not be committed",
    phases: [
      { ordinal: 1, phase_id: "P01", display_name: "First", phase_kind: "normal" },
      { ordinal: 1, phase_id: "P02", display_name: "Duplicate ordinal", phase_kind: "normal" },
    ],
    instruments: [],
  });
  assert.equal(rejected, null);

  const recoveredCatalog = catalogMessage(2);
  recoveredCatalog.catalog_version = "catalog-recovered";
  recoveredCatalog.procedure_display_name = "Recovered procedure";
  recoveredCatalog.phases[1].display_name = "Recovered current phase";
  const recovered = mapper.map(PUBLIC_TOPIC_NAMES.catalog, recoveredCatalog);

  assert.notEqual(recovered, null);
  assert.equal(recovered.patch.procedure.name, "Recovered procedure");
  assert.equal(recovered.patch.phase.code, "P02");
  assert.equal(recovered.patch.phase.index, 2);
  assert.equal(recovered.patch.phase.name, "Recovered current phase");
});

test("instrument snapshots preserve every surgeon-use and Mayo row in input order", () => {
  const mapper = new MainLayoutScenarioMapper();
  mapper.map(PUBLIC_TOPIC_NAMES.catalog, catalogMessage());
  const result = mapper.map(PUBLIC_TOPIC_NAMES.instruments, {
    revision: 3,
    instruments: [
      {
        instrument_id: "T02",
        instance_id: "T02#1",
        holder_role: "surgeon",
        location_type: "surgeon",
        state: "in_use",
        confidence: 0.51,
        evidence_status: "DT_ACCEPTED",
      },
      {
        instrument_id: "T07",
        instance_id: "T07#1",
        holder_role: "surgeon",
        location_type: "surgeon",
        state: "handed_over",
        confidence: 0.99,
        evidence_status: "EVENT_ACCEPTED",
      },
      {
        instrument_id: "T02",
        instance_id: "T02#assistant",
        holder_role: "assistant",
        location_type: "surgeon",
        state: "in_use",
        confidence: 1,
        evidence_status: "DT_ACCEPTED",
      },
      {
        instrument_id: "T07",
        instance_id: "T07#mayo",
        holder_role: "none",
        location_type: "mayo_stand",
        state: "parked_for_reuse",
        confidence: 0.55,
        evidence_status: "DT_ACCEPTED",
      },
      {
        instrument_id: "T08",
        instance_id: "T08#mayo",
        holder_role: "none",
        location_type: "mayo_stand",
        state: "awaiting_retrieval",
        confidence: 0.98,
        evidence_status: "EVENT_ACCEPTED",
      },
    ],
  });
  assert.deepEqual(result.patch.instrumentFlow, {
    inUse: [
      {
        toolId: "T02",
        instanceId: "T02#1",
        state: "in_use",
        confidence: 0.51,
        evidenceStatus: "DT_ACCEPTED",
      },
      {
        toolId: "T07",
        instanceId: "T07#1",
        state: "handed_over",
        confidence: 0.99,
        evidenceStatus: "EVENT_ACCEPTED",
      },
    ],
    mayo: [
      {
        toolId: "T07",
        instanceId: "T07#mayo",
        state: "parked_for_reuse",
        confidence: 0.55,
        evidenceStatus: "DT_ACCEPTED",
      },
      {
        toolId: "T08",
        instanceId: "T08#mayo",
        state: "awaiting_retrieval",
        confidence: 0.98,
        evidenceStatus: "EVENT_ACCEPTED",
      },
    ],
  });
});

test("malformed instrument snapshots fail atomically without blocking a lower replay revision", () => {
  const mapper = new MainLayoutScenarioMapper();
  mapper.map(PUBLIC_TOPIC_NAMES.catalog, catalogMessage());
  const uiState = {};
  applyScenarioResult(uiState, mapper.map(PUBLIC_TOPIC_NAMES.instruments, {
    revision: 1,
    instruments: [{
      instrument_id: "T02",
      instance_id: "T02#valid",
      holder_role: "surgeon",
      location_type: "surgeon",
      state: "in_use",
      confidence: 0.9,
      evidence_status: "DT_ACCEPTED",
    }],
  }));
  const lastKnown = structuredClone(uiState);

  const invalidSnapshots = [
    {
      revision: 100,
      instruments: ["not-an-instrument-row"],
    },
    {
      revision: 101,
      instruments: [{
        instrument_id: "   ",
        instance_id: "empty-tool-id",
        holder_role: "surgeon",
        location_type: "surgeon",
        state: "in_use",
        confidence: 0.8,
      }],
    },
    {
      revision: 102,
      instruments: [
        {
          instrument_id: "T02",
          instance_id: "duplicate-instance",
          holder_role: "surgeon",
          location_type: "surgeon",
          state: "in_use",
          confidence: 0.8,
        },
        {
          instrument_id: "T07",
          instance_id: "duplicate-instance",
          holder_role: "none",
          location_type: "mayo_stand",
          state: "parked_for_reuse",
          confidence: 0.7,
        },
      ],
    },
  ];

  invalidSnapshots.forEach((snapshot) => {
    const rejected = mapper.map(PUBLIC_TOPIC_NAMES.instruments, snapshot);
    assert.equal(rejected, null);
    applyScenarioResult(uiState, rejected);
    assert.deepEqual(uiState, lastKnown);
  });

  const recovered = mapper.map(PUBLIC_TOPIC_NAMES.instruments, {
    revision: 2,
    instruments: [
      {
        instrument_id: "T07",
        instance_id: "",
        holder_role: "surgeon",
        location_type: "surgeon",
        state: "handed_over",
        confidence: 0.75,
        evidence_status: "EVENT_ACCEPTED",
      },
      {
        instrument_id: "T08",
        instance_id: "",
        holder_role: "none",
        location_type: "mayo_stand",
        state: "awaiting_retrieval",
        confidence: 0.65,
        evidence_status: "DT_ACCEPTED",
      },
    ],
  });
  assert.notEqual(recovered, null);
  assert.deepEqual(recovered.patch.instrumentFlow, {
    inUse: [{
      toolId: "T07",
      instanceId: "",
      state: "handed_over",
      confidence: 0.75,
      evidenceStatus: "EVENT_ACCEPTED",
    }],
    mayo: [{
      toolId: "T08",
      instanceId: "",
      state: "awaiting_retrieval",
      confidence: 0.65,
      evidenceStatus: "DT_ACCEPTED",
    }],
  });
});

test("end effectors map hand possession, absence, and catalog labels to ARM 1 and ARM 2", () => {
  const mapper = new MainLayoutScenarioMapper();
  mapper.map(PUBLIC_TOPIC_NAMES.catalog, catalogMessage());
  const result = mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, {
    revision: 4,
    procedure_active: true,
    end_effectors: [
      {
        robot_id: "humanoid",
        end_effector_id: "left_hand",
        state: "holding",
        instrument_id: "T02",
        instance_id: "T02#held",
        confidence: 0.93,
        evidence_status: "DT_ACCEPTED",
      },
      {
        robot_id: "humanoid",
        end_effector_id: "right_hand",
        state: "empty",
        instrument_id: "T07",
        instance_id: "T07#must-be-ignored",
        confidence: 0.88,
        evidence_status: "EVENT_ACCEPTED",
      },
    ],
  });
  assert.deepEqual(result.patch.arms[1], {
    status: "holding",
    toolId: "T02",
    instanceId: "T02#held",
    confidence: 0.93,
    evidenceStatus: "DT_ACCEPTED",
  });
  assert.deepEqual(result.patch.arms[2], {
    status: "empty",
    toolId: "none",
    instanceId: "",
    confidence: 0.88,
    evidenceStatus: "EVENT_ACCEPTED",
  });
  assert.deepEqual(result.tools, [{ id: "T02", name: "Adson forceps" }]);

  const unknownAndMissing = mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, {
    revision: 5,
    procedure_active: true,
    end_effectors: [
      {
        robot_id: "humanoid",
        end_effector_id: "left_hand",
        state: "unknown",
        instrument_id: "T07",
        instance_id: "T07#must-be-ignored",
        confidence: 0.4,
        evidence_status: "SOURCE_STALE",
      },
      {
        robot_id: "other-robot",
        end_effector_id: "right_hand",
        state: "holding",
        instrument_id: "T08",
        instance_id: "T08#other-robot",
        confidence: 1,
        evidence_status: "DT_ACCEPTED",
      },
    ],
  });
  assert.deepEqual(unknownAndMissing.patch.arms[1], {
    status: "unknown",
    toolId: "none",
    instanceId: "",
    confidence: 0.4,
    evidenceStatus: "SOURCE_STALE",
  });
  assert.deepEqual(unknownAndMissing.patch.arms[2], {
    status: "unknown",
    toolId: "none",
    instanceId: "",
    confidence: 0,
    evidenceStatus: "",
  });
  assert.deepEqual(unknownAndMissing.tools, []);
});

test("invalid end-effector snapshots preserve LKV and do not block a lower replay revision", () => {
  const mapper = new MainLayoutScenarioMapper();
  mapper.map(PUBLIC_TOPIC_NAMES.catalog, catalogMessage());
  const uiState = {};
  applyScenarioResult(uiState, mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, {
    revision: 1,
    procedure_active: true,
    end_effectors: [
      {
        robot_id: "humanoid",
        end_effector_id: "left_hand",
        state: "holding",
        instrument_id: "T02",
        instance_id: "T02#valid",
        confidence: 0.9,
        evidence_status: "DT_ACCEPTED",
      },
      {
        robot_id: "humanoid",
        end_effector_id: "right_hand",
        state: "empty",
        instrument_id: "",
        instance_id: "",
        confidence: 1,
        evidence_status: "DT_ACCEPTED",
      },
    ],
  }));
  const lastKnown = structuredClone(uiState);

  const validLeft = {
    robot_id: "humanoid",
    end_effector_id: "left_hand",
    state: "empty",
    instrument_id: "",
    instance_id: "",
    confidence: 1,
    evidence_status: "DT_ACCEPTED",
  };
  const invalidSnapshots = [
    {
      revision: 100,
      procedure_active: true,
      end_effectors: [{ ...validLeft, state: "moving" }],
    },
    {
      revision: 101,
      procedure_active: true,
      end_effectors: [validLeft, { ...validLeft, confidence: 0.5 }],
    },
    {
      revision: 102,
      procedure_active: true,
      end_effectors: [{ ...validLeft, state: "holding", instrument_id: "   " }],
    },
    {
      revision: 103,
      procedure_active: true,
      end_effectors: [null],
    },
    {
      revision: 104,
      procedure_active: false,
      end_effectors: [validLeft],
    },
  ];

  invalidSnapshots.forEach((snapshot) => {
    const rejected = mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, snapshot);
    assert.equal(rejected, null);
    applyScenarioResult(uiState, rejected);
    assert.deepEqual(uiState, lastKnown);
  });

  const recovered = mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, {
    revision: 2,
    procedure_active: true,
    end_effectors: [{
      ...validLeft,
      confidence: 0.8,
      evidence_status: "EVENT_ACCEPTED",
    }],
  });
  assert.notEqual(recovered, null);
  assert.deepEqual(recovered.patch.arms[1], {
    status: "empty",
    toolId: "none",
    instanceId: "",
    confidence: 0.8,
    evidenceStatus: "EVENT_ACCEPTED",
  });
  assert.deepEqual(recovered.patch.arms[2], {
    status: "unknown",
    toolId: "none",
    instanceId: "",
    confidence: 0,
    evidenceStatus: "",
  });
});

test("an explicit empty end-effector snapshot authoritatively clears both hand arms", () => {
  const mapper = new MainLayoutScenarioMapper();
  mapper.map(PUBLIC_TOPIC_NAMES.catalog, catalogMessage());
  const uiState = {};
  applyScenarioResult(uiState, mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, {
    revision: 1,
    procedure_active: true,
    end_effectors: [
      {
        robot_id: "humanoid",
        end_effector_id: "left_hand",
        state: "holding",
        instrument_id: "T02",
        instance_id: "T02#1",
        confidence: 1,
        evidence_status: "DT_ACCEPTED",
      },
      {
        robot_id: "humanoid",
        end_effector_id: "right_hand",
        state: "holding",
        instrument_id: "T07",
        instance_id: "T07#1",
        confidence: 1,
        evidence_status: "DT_ACCEPTED",
      },
    ],
  }));

  const cleared = mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, {
    revision: 2,
    procedure_active: false,
    end_effectors: [],
  });
  assert.notEqual(cleared, null);
  assert.equal(cleared.meta.procedureActive, false);
  assert.deepEqual(cleared.patch.arms, {
    1: {
      status: "unknown",
      toolId: "none",
      instanceId: "",
      confidence: 0,
      evidenceStatus: "",
    },
    2: {
      status: "unknown",
      toolId: "none",
      instanceId: "",
      confidence: 0,
      evidenceStatus: "",
    },
  });

  applyScenarioResult(uiState, cleared);
  assert.deepEqual(uiState.arms, cleared.patch.arms);
  assert.deepEqual(cleared.tools, []);
});

test("a catalog version refresh replaces tool labels used by later snapshots", () => {
  const mapper = new MainLayoutScenarioMapper();
  const initialCatalog = mapper.map(PUBLIC_TOPIC_NAMES.catalog, catalogMessage(1));
  assert.deepEqual(
    initialCatalog.tools.find(({ id }) => id === "T02"),
    { id: "T02", name: "Adson forceps" },
  );

  const initialHolding = mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, {
    revision: 1,
    procedure_active: true,
    end_effectors: [{
      robot_id: "humanoid",
      end_effector_id: "left_hand",
      state: "holding",
      instrument_id: "T02",
      instance_id: "T02#1",
      confidence: 1,
      evidence_status: "DT_ACCEPTED",
    }],
  });
  assert.deepEqual(initialHolding.tools, [{ id: "T02", name: "Adson forceps" }]);

  const nextCatalogMessage = catalogMessage(2);
  nextCatalogMessage.catalog_version = "catalog-2";
  nextCatalogMessage.instruments[0].display_name = "Adson precision forceps";
  const refreshedCatalog = mapper.map(PUBLIC_TOPIC_NAMES.catalog, nextCatalogMessage);
  assert.equal(refreshedCatalog.meta.catalogVersion, "catalog-2");
  assert.deepEqual(
    refreshedCatalog.tools.find(({ id }) => id === "T02"),
    { id: "T02", name: "Adson precision forceps" },
  );

  const holdingAfterRefresh = mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, {
    revision: 2,
    procedure_active: true,
    end_effectors: [{
      robot_id: "humanoid",
      end_effector_id: "left_hand",
      state: "holding",
      instrument_id: "T02",
      instance_id: "T02#1",
      confidence: 1,
      evidence_status: "DT_ACCEPTED",
    }],
  });
  assert.deepEqual(holdingAfterRefresh.tools, [{ id: "T02", name: "Adson precision forceps" }]);
});

test("ranked predictions convert probability to percent without fabricating arm or standby", () => {
  const mapper = new MainLayoutScenarioMapper();
  mapper.map(PUBLIC_TOPIC_NAMES.catalog, catalogMessage());
  const result = mapper.map(PUBLIC_TOPIC_NAMES.toolPredictions, {
    revision: 5,
    procedure_active: true,
    predictions: [
      { rank: 1, instrument_id: "T02", confidence: 0.823 },
      { rank: 2, instrument_id: "T07", confidence: 0.516 },
    ],
  });
  assert.deepEqual(result.patch.predictions, [
    { rank: 1, toolId: "T02", confidence: 82.3 },
    { rank: 2, toolId: "T07", confidence: 51.6 },
  ]);
  assert.equal("arm" in result.patch.predictions[0], false);
  assert.equal("status" in result.patch.predictions[0], false);
});

test("redacted speech renders listening state and lower replay revisions are accepted", () => {
  const mapper = new MainLayoutScenarioMapper();
  const recognized = mapper.map(PUBLIC_TOPIC_NAMES.speech, {
    revision: 9,
    procedure_active: true,
    available: true,
    connected: true,
    state: "listening",
    text: "Bipolar cautery, please",
  });
  assert.deepEqual(recognized.patch.voice, {
    status: "listening",
    text: "Bipolar cautery, please",
  });

  const speech = mapper.map(PUBLIC_TOPIC_NAMES.speech, {
    revision: 10,
    procedure_active: true,
    available: true,
    connected: true,
    state: "listening",
    text: "",
  });
  assert.deepEqual(speech.patch.voice, { status: "listening", text: "Listening..." });

  const replayed = mapper.map(PUBLIC_TOPIC_NAMES.speech, {
    revision: 1,
    procedure_active: true,
    available: true,
    connected: true,
    state: "ready",
    text: "Replay restarted",
  });
  assert.deepEqual(replayed.patch.voice, { status: "ready", text: "Replay restarted" });
});

test("missing snapshots preserve last-known values while lower replay revisions apply in arrival order", () => {
  const mapper = new MainLayoutScenarioMapper();
  mapper.map(PUBLIC_TOPIC_NAMES.catalog, catalogMessage());
  const uiState = {};

  applyScenarioResult(uiState, mapper.map(PUBLIC_TOPIC_NAMES.context, {
    revision: 20,
    procedure_active: true,
    current_phase: "P03",
    phase_confidence: 0.9,
    phase_uncertain: false,
    execution_state: "running",
  }));
  applyScenarioResult(uiState, mapper.map(PUBLIC_TOPIC_NAMES.instruments, {
    revision: 20,
    instruments: [
      {
        instrument_id: "T02",
        holder_role: "surgeon",
        location_type: "surgeon",
        state: "in_use",
        confidence: 0.95,
      },
      {
        instrument_id: "T08",
        holder_role: "none",
        location_type: "mayo_stand",
        state: "awaiting_retrieval",
        confidence: 0.85,
      },
    ],
  }));
  applyScenarioResult(uiState, mapper.map(PUBLIC_TOPIC_NAMES.toolPredictions, {
    revision: 20,
    procedure_active: true,
    predictions: [{ rank: 1, instrument_id: "T07", confidence: 0.75 }],
  }));

  const lastKnown = structuredClone(uiState);

  // A receipt timeout or a topic with no usable message produces no mapper
  // transition. The UI reducer must not synthesize an empty snapshot for it.
  applyScenarioResult(uiState, null);
  applyScenarioResult(uiState, mapper.map(PUBLIC_TOPIC_NAMES.instruments, null));
  assert.deepEqual(uiState, lastKnown);

  const replayedResults = [
    mapper.map(PUBLIC_TOPIC_NAMES.context, {
      revision: 19,
      procedure_active: true,
      current_phase: "P01",
    }),
    mapper.map(PUBLIC_TOPIC_NAMES.instruments, { revision: 19, instruments: [] }),
    mapper.map(PUBLIC_TOPIC_NAMES.toolPredictions, {
      revision: 19,
      procedure_active: true,
      predictions: [],
    }),
  ];
  replayedResults.forEach((result) => {
    assert.notEqual(result, null);
    applyScenarioResult(uiState, result);
  });

  assert.equal(uiState.phase.code, "P01");
  assert.deepEqual(uiState.instrumentFlow, { inUse: [], mayo: [] });
  assert.deepEqual(uiState.predictions, []);

  applyScenarioResult(uiState, mapper.map(PUBLIC_TOPIC_NAMES.instruments, {
    revision: 21,
    instruments: [],
  }));
  applyScenarioResult(uiState, mapper.map(PUBLIC_TOPIC_NAMES.toolPredictions, {
    revision: 21,
    procedure_active: true,
    predictions: [],
  }));
  applyScenarioResult(uiState, mapper.map(PUBLIC_TOPIC_NAMES.context, {
    revision: 21,
    procedure_active: false,
    current_phase: "",
    phase_uncertain: true,
  }));

  assert.deepEqual(uiState.retrieval, {
    retrievedToolId: "none",
    location: "MAYO",
    inUseToolId: "none",
  });
  assert.deepEqual(uiState.predictions, []);
  assert.equal(uiState.phase.code, "—");
  assert.equal(uiState.phase.index, 0);
  assert.equal(uiState.phase.total, 3);
});

test("an active-idle-active replay loop recovers from a lower gateway revision", () => {
  const mapper = new MainLayoutScenarioMapper();
  const activeIdentity = replayIdentity();

  const firstGateway = mapper.map(PUBLIC_TOPIC_NAMES.gatewayInfo, gatewayMessage({
    revision: 100,
    procedureActive: true,
    procedureRunId: "run-replay",
  }));
  assert.equal(firstGateway.meta.resetScope, "initial");

  assert.notEqual(mapper.map(PUBLIC_TOPIC_NAMES.context, {
    revision: 100,
    procedure_active: true,
    current_phase: "P03",
  }), null);
  assert.notEqual(mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, {
    ...activeIdentity,
    revision: 100,
    end_effectors: [{
      robot_id: "humanoid",
      end_effector_id: "left_hand",
      state: "holding",
      instrument_id: "T02",
      instance_id: "T02#first-loop",
      confidence: 0.9,
      evidence_status: "DT_ACCEPTED",
    }],
  }), null);
  assert.notEqual(mapper.map(PUBLIC_TOPIC_NAMES.toolPredictions, {
    ...activeIdentity,
    revision: 100,
    predictions: [{ rank: 1, instrument_id: "T02", confidence: 0.8 }],
  }), null);
  assert.notEqual(mapper.map(PUBLIC_TOPIC_NAMES.speech, {
    ...activeIdentity,
    revision: 100,
    available: true,
    connected: true,
    state: "ready",
    text: "First loop",
  }), null);

  const idleGateway = mapper.map(PUBLIC_TOPIC_NAMES.gatewayInfo, gatewayMessage({
    revision: 101,
    procedureActive: false,
    procedureRunId: "",
  }));
  assert.equal(idleGateway.meta.resetScope, "run");
  assert.equal(idleGateway.meta.procedureActive, false);

  const replayedGateway = mapper.map(PUBLIC_TOPIC_NAMES.gatewayInfo, gatewayMessage({
    revision: 1,
    procedureActive: true,
    procedureRunId: "run-replay",
  }));
  assert.notEqual(replayedGateway, null);
  assert.equal(replayedGateway.meta.resetScope, "run");
  assert.equal(replayedGateway.meta.procedureActive, true);
  assert.equal(replayedGateway.meta.procedureRunId, "run-replay");

  assert.equal(mapper.map(PUBLIC_TOPIC_NAMES.toolPredictions, {
    ...activeIdentity,
    procedure_run_id: "run-from-previous-recording",
    revision: 1,
    predictions: [{ rank: 1, instrument_id: "T02", confidence: 0.9 }],
  }), null);

  const replayedContext = mapper.map(PUBLIC_TOPIC_NAMES.context, {
    revision: 1,
    procedure_active: true,
    current_phase: "P01",
  });
  assert.notEqual(replayedContext, null);
  assert.equal(replayedContext.meta.procedureActive, true);

  const replayedEndEffectors = mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, {
    ...activeIdentity,
    revision: 1,
    end_effectors: [{
      robot_id: "humanoid",
      end_effector_id: "left_hand",
      state: "holding",
      instrument_id: "T07",
      instance_id: "T07#second-loop",
      confidence: 0.95,
      evidence_status: "EVENT_ACCEPTED",
    }],
  });
  assert.deepEqual(replayedEndEffectors.patch.arms[1], {
    status: "holding",
    toolId: "T07",
    instanceId: "T07#second-loop",
    confidence: 0.95,
    evidenceStatus: "EVENT_ACCEPTED",
  });

  const replayedPredictions = mapper.map(PUBLIC_TOPIC_NAMES.toolPredictions, {
    ...activeIdentity,
    revision: 1,
    predictions: [{ rank: 1, instrument_id: "T08", confidence: 0.7 }],
  });
  assert.deepEqual(replayedPredictions.patch.predictions, [
    { rank: 1, toolId: "T08", confidence: 70 },
  ]);

  const replayedSpeech = mapper.map(PUBLIC_TOPIC_NAMES.speech, {
    ...activeIdentity,
    revision: 1,
    available: true,
    connected: true,
    state: "ready",
    text: "Second loop",
  });
  assert.deepEqual(replayedSpeech.patch.voice, { status: "ready", text: "Second loop" });
});

test("replay compatibility keeps identity and structured-message validation fail closed", () => {
  const mapper = new MainLayoutScenarioMapper();
  const activeIdentity = replayIdentity();
  mapper.map(PUBLIC_TOPIC_NAMES.gatewayInfo, gatewayMessage({
    revision: 50,
    procedureActive: true,
    procedureRunId: "run-replay",
  }));

  const uiState = {};
  applyScenarioResult(uiState, mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, {
    ...activeIdentity,
    revision: 50,
    end_effectors: [{
      robot_id: "humanoid",
      end_effector_id: "left_hand",
      state: "holding",
      instrument_id: "T02",
      instance_id: "T02#valid",
      confidence: 0.9,
      evidence_status: "DT_ACCEPTED",
    }],
  }));
  applyScenarioResult(uiState, mapper.map(PUBLIC_TOPIC_NAMES.toolPredictions, {
    ...activeIdentity,
    revision: 50,
    predictions: [{ rank: 1, instrument_id: "T02", confidence: 0.8 }],
  }));
  const lastKnown = structuredClone(uiState);

  const rejected = [
    mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, {
      ...activeIdentity,
      procedure_run_id: "another-run",
      revision: 1,
      end_effectors: [],
    }),
    mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, {
      ...activeIdentity,
      revision: 1,
      end_effectors: [{
        robot_id: "humanoid",
        end_effector_id: "left_hand",
        state: "moving",
        instrument_id: "T07",
        instance_id: "T07#invalid",
        confidence: 0.8,
      }],
    }),
    mapper.map(PUBLIC_TOPIC_NAMES.toolPredictions, {
      ...activeIdentity,
      revision: 1,
      predictions: [{ rank: 2, instrument_id: "T08", confidence: 0.6 }],
    }),
  ];
  rejected.forEach((result) => {
    assert.equal(result, null);
    applyScenarioResult(uiState, result);
  });
  assert.deepEqual(uiState, lastKnown);

  const validLowerRevision = mapper.map(PUBLIC_TOPIC_NAMES.robotEndEffectors, {
    ...activeIdentity,
    revision: 1,
    end_effectors: [{
      robot_id: "humanoid",
      end_effector_id: "left_hand",
      state: "holding",
      instrument_id: "T07",
      instance_id: "T07#valid-replay",
      confidence: 0.85,
      evidence_status: "EVENT_ACCEPTED",
    }],
  });
  assert.notEqual(validLowerRevision, null);
  assert.equal(validLowerRevision.patch.arms[1].toolId, "T07");
});

test("gateway contract and identity changes fail closed and reset the run scope", () => {
  const mapper = new MainLayoutScenarioMapper();
  const first = mapper.map(PUBLIC_TOPIC_NAMES.gatewayInfo, {
    revision: 1,
    schema_version: "1.1.0",
    interface_version: "0.3.0",
    catalog_version: "catalog-1",
    gateway_instance_id: "gateway-1",
    procedure_run_id: "run-1",
    procedure_type: "thyroidectomy_demo",
    procedure_active: true,
  });
  assert.equal(first.meta.contractCompatible, true);
  assert.equal(first.meta.resetScope, "initial");

  const matchingPrediction = {
    revision: 2,
    schema_version: "1.1.0",
    catalog_version: "catalog-1",
    gateway_instance_id: "gateway-1",
    procedure_run_id: "run-1",
    procedure_type: "thyroidectomy_demo",
    procedure_active: true,
    predictions: [{ rank: 1, instrument_id: "T02", confidence: 0.8 }],
  };
  assert.notEqual(mapper.map(PUBLIC_TOPIC_NAMES.toolPredictions, matchingPrediction), null);
  assert.equal(
    mapper.map(PUBLIC_TOPIC_NAMES.toolPredictions, { ...matchingPrediction, procedure_run_id: "old-run", revision: 3 }),
    null,
  );

  const nextRun = mapper.map(PUBLIC_TOPIC_NAMES.gatewayInfo, {
    ...first,
    revision: 2,
    schema_version: "1.1.0",
    interface_version: "0.3.0",
    catalog_version: "catalog-1",
    gateway_instance_id: "gateway-1",
    procedure_run_id: "run-2",
    procedure_type: "thyroidectomy_demo",
    procedure_active: true,
  });
  assert.equal(nextRun.meta.resetScope, "run");

  const mismatch = mapper.map(PUBLIC_TOPIC_NAMES.gatewayInfo, {
    revision: 3,
    schema_version: "9.9.9",
    interface_version: "0.3.0",
    catalog_version: "catalog-1",
    gateway_instance_id: "gateway-1",
    procedure_run_id: "run-2",
    procedure_type: "thyroidectomy_demo",
    procedure_active: true,
  });
  assert.equal(mismatch.meta.contractCompatible, false);
});

test("mapper exposes structured rejection reasons without retaining raw messages", () => {
  const mapper = new MainLayoutScenarioMapper();

  assert.equal(mapper.map(PUBLIC_TOPIC_NAMES.context, null), null);
  assert.deepEqual(mapper.getLastRejection(), {
    topic: PUBLIC_TOPIC_NAMES.context,
    reason: "message_not_object",
    details: {},
  });

  assert.equal(mapper.map("/surgery/private_topic", { text: "must not leak" }), null);
  const unsupported = mapper.getLastRejection();
  assert.equal(unsupported.topic, "/surgery/private_topic");
  assert.equal(unsupported.reason, "unsupported_topic");
  assert.doesNotMatch(JSON.stringify(unsupported), /must not leak/);

  assert.equal(mapper.map(PUBLIC_TOPIC_NAMES.instruments, {
    revision: 22,
    instruments: "invalid rows",
    text: "must not leak either",
  }), null);
  const validation = mapper.getLastRejection();
  assert.equal(validation.topic, PUBLIC_TOPIC_NAMES.instruments);
  assert.equal(validation.reason, "validation_failed");
  assert.doesNotMatch(JSON.stringify(validation), /invalid rows|must not leak either/);

  assert.notEqual(mapper.map(PUBLIC_TOPIC_NAMES.instruments, {
    revision: 1,
    instruments: [],
  }), null);
  assert.equal(mapper.getLastRejection(), null, "an accepted snapshot clears the prior rejection");
});

test("identity rejection summarizes expected and received scope while gateway metadata exposes revision", () => {
  const mapper = new MainLayoutScenarioMapper();
  const gateway = mapper.map(PUBLIC_TOPIC_NAMES.gatewayInfo, gatewayMessage({
    revision: 42,
    procedureActive: true,
    procedureRunId: "run-current",
  }));

  assert.equal(gateway.meta.revision, 42);
  assert.equal(gateway.meta.procedureRunId, "run-current");
  assert.equal(gateway.meta.procedureType, "thyroidectomy_demo");
  assert.equal(gateway.meta.procedureActive, true);

  assert.equal(mapper.map(PUBLIC_TOPIC_NAMES.toolPredictions, {
    ...replayIdentity({ procedureRunId: "run-previous" }),
    revision: 99,
    predictions: [],
    payload: { secret: "must not be retained" },
  }), null);
  const rejection = mapper.getLastRejection();
  assert.equal(rejection.topic, PUBLIC_TOPIC_NAMES.toolPredictions);
  assert.equal(rejection.reason, "identity_mismatch");
  assert.equal(rejection.details.expectedProcedureRunId, "run-current");
  assert.equal(rejection.details.receivedProcedureRunId, "run-previous");
  assert.equal(rejection.details.expectedProcedureActive, true);
  assert.equal(rejection.details.receivedProcedureActive, true);
  assert.doesNotMatch(JSON.stringify(rejection), /must not be retained/);
});

test("health and moving_to_target robot state map to UI-safe metadata", () => {
  const mapper = new MainLayoutScenarioMapper();
  const robots = mapper.map(PUBLIC_TOPIC_NAMES.robots, {
    revision: 1,
    robots: [{ robot_id: "bed-1", robot_type: "bed_retraction_arm", execution_state: "moving_to_target" }],
  });
  assert.equal(robots.patch.arms[3].status, "moving");
  assert.equal(robots.patch.arms[4].status, "unknown");
  assert.equal(robots.patch.arms[3].toolName, "Retraction");
  assert.equal(robots.patch.arms[4].toolName, "Suction");

  const health = mapper.map(PUBLIC_TOPIC_NAMES.health, {
    revision: 2,
    healthy: false,
    state: "degraded",
    unavailable_sources: ["speech"],
    stale_sources: ["flir"],
    error_codes: ["SOURCE_TIMEOUT"],
  });
  assert.deepEqual(health.meta.health, {
    healthy: false,
    state: "degraded",
    unavailableSources: ["speech"],
    staleSources: ["flir"],
    errorCodes: ["SOURCE_TIMEOUT"],
  });
});
