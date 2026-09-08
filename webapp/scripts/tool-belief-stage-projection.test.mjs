import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import ts from "typescript";

const source = new URL("../src/components/stage/toolBeliefStageProjection.ts", import.meta.url);
const panelRowsSource = new URL("../src/components/stage/toolBeliefPanelRows.ts", import.meta.url);
const panelSource = new URL("../src/components/stage/ToolBeliefPanel.tsx", import.meta.url);
const rosSourceRoot = new URL("../src/ros/", import.meta.url);
const testBuildDir = await mkdtemp(join(tmpdir(), "taskplanner-tool-belief-stage-test-"));
const outputPath = join(testBuildDir, "toolBeliefStageProjection.mjs");
const transpiled = ts.transpileModule(await readFile(source, "utf8"), {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2020,
  },
});
await writeFile(
  outputPath,
  transpiled.outputText.replace(
    '"../../ros/toolBeliefMessages"',
    '"./toolBeliefMessages.mjs"',
  ),
  "utf8",
);
const panelRowsOutputPath = join(testBuildDir, "toolBeliefPanelRows.mjs");
const panelRowsTranspiled = ts.transpileModule(await readFile(panelRowsSource, "utf8"), {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2020,
  },
});
await writeFile(
  panelRowsOutputPath,
  panelRowsTranspiled.outputText.replace(
    '"../../ros/toolBeliefMessages"',
    '"./toolBeliefMessages.mjs"',
  ),
  "utf8",
);
for (const [sourceName, outputName] of [
  ["rosMessageBounds.ts", "rosMessageBounds.mjs"],
  ["toolBeliefContract.ts", "toolBeliefContract.mjs"],
  ["toolBeliefMessages.ts", "toolBeliefMessages.mjs"],
]) {
  const sourceText = await readFile(new URL(sourceName, rosSourceRoot), "utf8");
  const result = ts.transpileModule(sourceText, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2020,
    },
  });
  await writeFile(join(testBuildDir, outputName), result.outputText, "utf8");
}
const messagesOutputPath = join(testBuildDir, "toolBeliefMessages.mjs");
const messagesSource = await readFile(messagesOutputPath, "utf8");
await writeFile(
  messagesOutputPath,
  messagesSource
    .replace('"./rosMessageBounds"', '"./rosMessageBounds.mjs"')
    .replace('"./toolBeliefContract"', '"./toolBeliefContract.mjs"'),
  "utf8",
);
const {
  TOOL_BELIEF_STAGE_STALE_AFTER_MS,
  aggregateRackTools,
  projectToolBeliefsOntoStage,
} = await import(pathToFileURL(outputPath));
const { aggregateToolBeliefPanelRows } = await import(pathToFileURL(panelRowsOutputPath));

const holders = [
  ["rack", 0, 0, 20, 40],
  ["humanoid_left", 22, 0, 12, 12],
  ["humanoid_right", 22, 14, 12, 12],
  ["surgeon", 36, 0, 20, 26],
  ["cleaner", 0, 42, 20, 12],
  ["mayo", 36, 28, 30, 10],
].map(([id, left, top, width, height]) => ({
  id,
  label: String(id),
  rect: { left, top, width, height },
  contentRect: { left, top, width, height },
  tone: id === "rack" ? "rack" : id.startsWith("humanoid") ? "robot" : id,
  active: false,
}));

const rackSlots = ["T02", "T04", "T07"].map((instrumentId, index) => ({
  id: `slot-${index + 1}`,
  instrumentId,
  label: instrumentId,
  shortLabel: instrumentId,
  occupied: true,
  rect: { left: 5, top: 5 + index * 10, width: 8, height: 6 },
}));

function placement(instrumentId, instanceIds, gridIndex) {
  const slot = rackSlots[gridIndex];
  return {
    id: instanceIds.join("+"),
    instrumentId,
    label: instrumentId,
    shortLabel: instrumentId,
    instanceIds,
    displayInstanceId: instanceIds[0],
    quantity: instanceIds.length,
    holderId: "rack",
    holderLabel: "rack",
    left: slot.rect.left,
    top: slot.rect.top,
    width: slot.rect.width,
    height: slot.rect.height,
    scale: 1,
    compact: false,
    gridIndex,
    displayState: "waiting",
    highlight: "normal",
    lifecycle: "waiting",
    footerBadges: [],
    contaminated: false,
    active: false,
    layoutVariant: "card",
    density: "regular",
  };
}

const basePlacements = [
  placement("T02", ["T02#1"], 0),
  placement("T04", ["T04#1"], 1),
  placement("T07", ["T07#1"], 2),
];

function belief(
  instrumentId,
  instanceId,
  status,
  committedLocationId,
  mostLikelyLocationId = committedLocationId || "unknown",
) {
  return {
    trackId: instanceId,
    instrumentId,
    instanceId,
    displayName: instrumentId,
    existenceProbability: 1,
    status,
    committedLocationId,
    committedLocationProbability: committedLocationId ? 1 : 0,
    mostLikelyLocationId,
    mostLikelyProbability: 1,
    locations: [{ locationId: mostLikelyLocationId, probability: 1 }],
    lastPositiveAgeSec: 0,
    evidenceSources: ["cam3"],
    motionMode: "idle",
    statusFlags: [],
  };
}

function capacityBelief(
  instrumentId,
  instanceId,
  status,
  committedLocationId,
  mostLikelyLocationId = committedLocationId || "unknown",
  existenceProbability = 1,
  capacityState = "capacity_slot_active",
) {
  return {
    ...belief(
      instrumentId,
      instanceId,
      status,
      committedLocationId,
      mostLikelyLocationId,
    ),
    existenceProbability,
    statusFlags: [
      "scenario_bounded_capacity",
      "observation_only",
      "logical_instance_exchangeable",
      "physical_identity_not_asserted",
      capacityState,
    ],
  };
}

function runtime(tools, receivedAt = 10_000, overrides = {}) {
  const { schemaVersion = "taskplanner.tool_beliefs.v1", ...runtimeOverrides } = overrides;
  return {
    enabled: true,
    snapshot: {
      header: { stamp: null, frameId: "map" },
      schemaVersion,
      procedureId: "thyroidectomy_demo",
      procedureRunId: "",
      trackerRevision: "test",
      observationOnly: true,
      robotMotionActive: false,
      robotMotionNegativeScale: 1,
      ignoredOutOfInventoryCount: 0,
      ignoredClassNames: [],
      tools,
      receivedAt,
    },
    setEnabled: async () => ({ accepted: true, message: "ok" }),
    ...runtimeOverrides,
  };
}

function project(runtimeValue, placements = basePlacements, nowMs = 10_100) {
  return projectToolBeliefsOntoStage({
    activeBundle: "thyroidectomy_demo",
    holders,
    nowMs,
    placements,
    rackSlots,
    runtime: runtimeValue,
  });
}

function aggregate(result, inventoryCounts) {
  return aggregateRackTools(result.placements, {
    activeBundle: "thyroidectomy_demo",
    boardHolders: holders,
    boardRackSlots: result.rackSlots,
    displayToolName: (instrumentId) => instrumentId,
    language: "ko",
    layout: {
      metadata: {
        bundles: [{
          id: "thyroidectomy_demo",
          instruments: Object.entries(inventoryCounts).map(([id, inventory_count]) => ({
            id,
            inventory_count,
          })),
        }],
      },
    },
    ui: { waitingState: "대기" },
  }, result.rackSlots, result.hiddenInventoryCounts, result.exchangeableActiveCounts);
}

test("confirmed Mayo beliefs move Bovie and Bipolar while idle and clear their rack slots", () => {
  const runtimeValue = runtime([
    belief("T02", "T02#1", "confirmed", "tray"),
    belief("T04", "T04#1", "confirmed", "mayo"),
    belief("T07", "T07#1", "confirmed", "mayo"),
  ]);
  const result = project(runtimeValue);
  assert.equal(runtimeValue.snapshot.procedureRunId, "", "idle snapshots remain eligible");
  assert.deepEqual(
    result.placements.map(({ instrumentId, holderId }) => [instrumentId, holderId]).sort(),
    [["T02", "rack"], ["T04", "mayo"], ["T07", "mayo"]],
  );
  assert.deepEqual(result.rackSlots.map(({ occupied }) => occupied), [true, false, false]);
  assert.equal(result.projectedHolderIds.has("mayo"), true);
  assert.deepEqual(result.projectedInstanceIds.sort(), ["T04#1", "T07#1"]);
  assert.equal(basePlacements[1].holderId, "rack", "the Digital Twin view model was not mutated");
});

test("Mayo projection clears an inactive stale using presentation without mutating DT", () => {
  const surgeonOwned = [{
    ...placement("T04", ["T04#1"], 1),
    holderId: "surgeon",
    holderLabel: "surgeon",
    displayState: "using",
    lifecycle: "사용중",
    footerBadges: [
      { label: "사용중", tone: "active" },
      { label: "오염", tone: "danger" },
    ],
    contaminated: true,
    active: false,
    s: [true],
  }];
  const result = project(runtime([
    belief("T04", "T04#1", "confirmed", "mayo"),
  ]), surgeonOwned);
  const mayo = result.placements.find(({ instrumentId }) => instrumentId === "T04");

  assert.equal(mayo?.holderId, "mayo");
  assert.equal(mayo?.displayState, "waiting");
  assert.deepEqual(mayo?.footerBadges, [{ label: "오염", tone: "danger" }]);
  assert.equal(surgeonOwned[0].displayState, "using", "the Digital Twin view model was not mutated");
});

test("rack inventory aggregation does not recreate Bovie or Bipolar after the projection", () => {
  const result = project(runtime([
    belief("T02", "T02#1", "confirmed", "tray"),
    belief("T04", "T04#1", "confirmed", "mayo"),
    belief("T07", "T07#1", "confirmed", "mayo"),
  ]));
  const aggregated = aggregateRackTools(result.placements, {
    activeBundle: "thyroidectomy_demo",
    boardRackSlots: result.rackSlots,
    displayToolName: (instrumentId) => instrumentId,
    language: "ko",
    layout: {
      metadata: {
        bundles: [{
          id: "thyroidectomy_demo",
          instruments: ["T02", "T04", "T07"].map((id) => ({
            id,
            inventory_count: 1,
          })),
        }],
      },
    },
    ui: { waitingState: "대기" },
  }, result.rackSlots);

  assert.deepEqual(
    aggregated.filter(({ holderId }) => holderId === "rack").map(({ instrumentId }) => instrumentId),
    ["T02"],
  );
  assert.deepEqual(
    aggregated.filter(({ holderId }) => holderId === "mayo").map(({ instrumentId }) => instrumentId).sort(),
    ["T04", "T07"],
  );
  assert.equal(
    aggregated.reduce((total, item) => total + item.quantity, 0),
    3,
    "all fixed-inventory instances remain represented exactly once",
  );
});

test("off, missing, stale, and other-bundle snapshots fail back to the exact DT layout", () => {
  const confirmed = [belief("T07", "T07#1", "uncertain", "", "unknown")];
  const candidates = [
    null,
    runtime(confirmed, 10_000, { enabled: false }),
    runtime(confirmed, 10_000, { enabled: null }),
    runtime(confirmed, 10_000, { snapshot: null }),
    runtime(confirmed, 10_000, {
      snapshot: { ...runtime(confirmed).snapshot, procedureId: "other_bundle" },
    }),
  ];
  for (const candidate of candidates) {
    const result = project(candidate);
    assert.deepEqual(result.placements, basePlacements);
    assert.deepEqual(result.rackSlots, rackSlots);
  }

  const stale = project(
    runtime(confirmed),
    basePlacements,
    10_000 + TOOL_BELIEF_STAGE_STALE_AFTER_MS + 1,
  );
  assert.deepEqual(stale.placements, basePlacements);
  assert.deepEqual(stale.rackSlots, rackSlots);
});

test("uncommitted known-location and mismatched instances do not override DT", () => {
  const result = project(runtime([
    belief("T02", "T02#1", "probable", "", "mayo"),
    belief("T04", "T04#1", "confirmed", "", "mayo"),
    belief("WRONG", "T04#1", "confirmed", "mayo"),
  ]));
  assert.deepEqual(result.placements, basePlacements);
  assert.deepEqual(result.rackSlots, rackSlots);
});

test("a latched Mayo commit remains projected when certainty drops below confirmed", () => {
  const result = project(runtime([
    belief("T04", "T04#1", "probable", "mayo"),
  ]));
  assert.equal(
    result.placements.find(({ instrumentId }) => instrumentId === "T04")?.holderId,
    "mayo",
  );
  assert.equal(result.rackSlots[1].occupied, false);
});

test("an old commit wins until hysteresis releases it even when unknown is currently most likely", () => {
  const oldCommit = {
    ...belief("T04", "T04#1", "probable", "mayo", "unknown"),
    committedLocationProbability: 0.46,
    mostLikelyProbability: 0.54,
    locations: [
      { locationId: "mayo", probability: 0.46 },
      { locationId: "unknown", probability: 0.54 },
    ],
  };
  const result = project(runtime([oldCommit]));
  assert.equal(
    result.placements.find(({ instrumentId }) => instrumentId === "T04")?.holderId,
    "mayo",
  );
  assert.deepEqual(result.hiddenInstanceIds, []);
});

test("fresh unknown T07 is hidden, vacates its rack slot, and is not recreated from inventory", () => {
  const result = project(runtime([
    belief("T07", "T07#1", "uncertain", "", " Unknown Location "),
  ]));
  assert.equal(result.placements.some(({ instrumentId }) => instrumentId === "T07"), false);
  assert.equal(result.rackSlots[2].occupied, false);
  assert.deepEqual(result.hiddenInstanceIds, ["T07#1"]);
  assert.equal(result.hiddenInventoryCounts.get("T07"), 1);

  const aggregated = aggregateRackTools(result.placements, {
    activeBundle: "thyroidectomy_demo",
    boardRackSlots: result.rackSlots,
    displayToolName: (instrumentId) => instrumentId,
    language: "ko",
    layout: {
      metadata: {
        bundles: [{
          id: "thyroidectomy_demo",
          instruments: ["T02", "T04", "T07"].map((id) => ({
            id,
            inventory_count: 1,
          })),
        }],
      },
    },
    ui: { waitingState: "대기" },
  }, result.rackSlots, result.hiddenInventoryCounts);
  assert.equal(aggregated.some(({ instrumentId }) => instrumentId === "T07"), false);
});

test("unknown belief preserves an instance already delivered to the surgeon", () => {
  const delivered = [{
    ...placement("T07", ["T07#1"], 2),
    holderId: "surgeon",
    holderLabel: "surgeon",
    s: [true],
  }];
  const result = project(runtime([
    belief("T07", "T07#1", "uncertain", "", "unknown"),
  ]), delivered);
  assert.deepEqual(result.placements, delivered);
  assert.deepEqual(result.hiddenInstanceIds, []);
  assert.equal(result.hiddenInventoryCounts.size, 0);
});

test("surgeon-owned DT state wins over an old non-surgeon commit while the current location is unknown", () => {
  const delivered = [{
    ...placement("T07", ["T07#1"], 2),
    holderId: "surgeon",
    holderLabel: "surgeon",
    s: [true],
  }];
  const result = project(runtime([{
    ...belief("T07", "T07#1", "probable", "mayo", "unknown"),
    committedLocationProbability: 0.46,
    mostLikelyProbability: 0.54,
    locations: [
      { locationId: "mayo", probability: 0.46 },
      { locationId: "unknown", probability: 0.54 },
    ],
  }]), delivered);
  assert.deepEqual(result.placements, delivered);
  assert.deepEqual(result.projectedInstanceIds, []);
  assert.deepEqual(result.hiddenInstanceIds, []);
});

test("a handover-zone card is not mistaken for a completed surgeon delivery", () => {
  const awaitingHandover = [{
    ...placement("T07", ["T07#1"], 2),
    holderId: "surgeon",
    holderLabel: "surgeon",
    s: [false],
  }];
  const result = project(runtime([
    belief("T07", "T07#1", "uncertain", "", "unknown"),
  ]), awaitingHandover);
  assert.deepEqual(result.placements, []);
  assert.deepEqual(result.hiddenInstanceIds, ["T07#1"]);
});

test("surgeon-owned preservation remains instance-specific for duplicate tools", () => {
  const surgeonGroup = [{
    ...placement("T02", ["T02#1", "T02#2"], 0),
    holderId: "surgeon",
    holderLabel: "surgeon",
    s: [true, false],
  }];
  const result = project(runtime([
    belief("T02", "T02#1", "uncertain", "", "unknown"),
    belief("T02", "T02#2", "uncertain", "", "unknown"),
  ]), surgeonGroup);
  assert.deepEqual(result.placements[0].instanceIds, ["T02#1"]);
  assert.equal(result.placements[0].quantity, 1);
  assert.deepEqual(result.hiddenInstanceIds, ["T02#2"]);
});

test("hides only the unknown instance from a two-instance tool group", () => {
  const grouped = [placement("T02", ["T02#1", "T02#2"], 0)];
  const result = project(runtime([
    belief("T02", "T02#1", "uncertain", "", "unknown"),
    belief("T02", "T02#2", "confirmed", "tray"),
  ]), grouped);
  assert.equal(result.placements.length, 1);
  assert.deepEqual(result.placements[0].instanceIds, ["T02#2"]);
  assert.equal(result.placements[0].quantity, 1);
  assert.deepEqual(result.hiddenInstanceIds, ["T02#1"]);
  assert.equal(result.rackSlots[0].occupied, true);

  const aggregated = aggregateRackTools(result.placements, {
    activeBundle: "thyroidectomy_demo",
    boardRackSlots: result.rackSlots,
    displayToolName: (instrumentId) => instrumentId,
    language: "ko",
    layout: {
      metadata: {
        bundles: [{
          id: "thyroidectomy_demo",
          instruments: [{ id: "T02", inventory_count: 2 }],
        }],
      },
    },
    ui: { waitingState: "대기" },
  }, result.rackSlots, result.hiddenInventoryCounts);
  assert.equal(aggregated.length, 1);
  assert.deepEqual(aggregated[0].instanceIds, ["T02#2"]);
  assert.equal(aggregated[0].quantity, 1);
});

test("splits a multi-instance visual without losing identity or quantity", () => {
  const grouped = [placement("T02", ["T02#1", "T02#2"], 0)];
  const result = project(runtime([
    belief("T02", "T02#1", "confirmed", "mayo"),
    belief("T02", "T02#2", "probable", "", "mayo"),
  ]), grouped);
  const rack = result.placements.find(({ holderId }) => holderId === "rack");
  const mayo = result.placements.find(({ holderId }) => holderId === "mayo");
  assert.deepEqual(rack?.instanceIds, ["T02#2"]);
  assert.equal(rack?.quantity, 1);
  assert.deepEqual(mayo?.instanceIds, ["T02#1"]);
  assert.equal(mayo?.quantity, 1);
  assert.equal(
    result.placements.reduce((total, item) => total + (item.quantity ?? 1), 0),
    2,
  );
});

test("inactive exchangeable capacity slots hide and metadata capacity cannot recreate them", () => {
  const grouped = [placement("T02", ["T02#1", "T02#2"], 0)];
  const result = project(runtime([
    capacityBelief("T02", "T02#1", "probable", "tray", "tray", 0.65, "capacity_slot_probable"),
    capacityBelief("T02", "T02#2", "uncertain", "", "unknown", 0, "capacity_slot_inactive"),
  ], 10_000, { schemaVersion: "taskplanner.exchangeable_tool_capacity_belief.v2" }), grouped);
  const aggregated = aggregate(result, { T02: 5 });

  assert.equal(result.exchangeableActiveCounts.get("T02"), 1);
  assert.deepEqual(result.hiddenInstanceIds, ["T02#2"]);
  assert.equal(aggregated.length, 1);
  assert.equal(aggregated[0].holderId, "rack");
  assert.equal(aggregated[0].quantity, 1);
  assert.equal(aggregated[0].displayInstanceId, undefined);
  assert.deepEqual(aggregated[0].instanceIds, []);
  assert.equal(aggregated[0].id.includes("#"), false);
});

test("same-type exchangeable tools aggregate by instrument and holder without physical labels", () => {
  const separate = [
    placement("T04", ["T04#1"], 1),
    placement("T04", ["T04#2"], 1),
  ];
  const result = project(runtime([
    capacityBelief("T04", "T04#1", "confirmed", "mayo"),
    capacityBelief("T04", "T04#2", "confirmed", "mayo"),
  ], 10_000, { schemaVersion: "taskplanner.exchangeable_tool_capacity_belief.v2" }), separate);
  const aggregated = aggregate(result, { T04: 5 });

  assert.equal(aggregated.length, 1);
  assert.equal(aggregated[0].id, "belief:T04:mayo");
  assert.equal(aggregated[0].holderId, "mayo");
  assert.equal(aggregated[0].quantity, 2);
  assert.equal(aggregated[0].displayInstanceId, undefined);
  assert.deepEqual(aggregated[0].instanceIds, []);
  assert.equal(aggregated[0].active, false);
  assert.equal(aggregated[0].canonicalMayoDecision, undefined);
});

test("canonical Mayo reuse evidence survives an exchangeable Mayo projection", () => {
  const canonicalMayo = {
    ...placement("T02", ["T02#1"], 0),
    canonicalMayoDecision: {
      disposition: "reuse",
      confidence: 0.8,
      source: "digital_twin",
      evidence: "vlm_mayo_policy",
    },
  };
  const result = project(runtime([
    capacityBelief("T02", "T02#1", "confirmed", "mayo"),
  ], 10_000, { schemaVersion: "taskplanner.exchangeable_tool_capacity_belief.v2" }), [
    canonicalMayo,
  ]);
  const aggregated = aggregate(result, { T02: 1 });

  assert.equal(aggregated.length, 1);
  assert.equal(aggregated[0].holderId, "mayo");
  assert.deepEqual(aggregated[0].canonicalMayoDecision, canonicalMayo.canonicalMayoDecision);
});

test("exchangeable logical id swaps preserve the same surgeon and Mayo quantities", () => {
  const surgeonGroup = [{
    ...placement("T02", ["T02#1", "T02#2"], 0),
    holderId: "surgeon",
    holderLabel: "surgeon",
    s: [true, false],
  }];
  const toolSets = [
    [
      capacityBelief("T02", "T02#1", "uncertain", "", "unknown"),
      capacityBelief("T02", "T02#2", "confirmed", "mayo"),
    ],
    [
      capacityBelief("T02", "T02#1", "confirmed", "mayo"),
      capacityBelief("T02", "T02#2", "uncertain", "", "unknown"),
    ],
  ];
  const signatures = toolSets.map((tools) => {
    const result = project(runtime(
      tools,
      10_000,
      { schemaVersion: "taskplanner.exchangeable_tool_capacity_belief.v2" },
    ), surgeonGroup);
    return aggregate(result, { T02: 2 })
      .map(({ holderId, instrumentId, quantity, displayInstanceId, id }) => ({
        holderId,
        instrumentId,
        quantity,
        displayInstanceId,
        id,
      }))
      .sort((left, right) => left.holderId.localeCompare(right.holderId));
  });

  assert.deepEqual(signatures[0], signatures[1]);
  assert.deepEqual(
    signatures[0].map(({ holderId, quantity }) => [holderId, quantity]),
    [["mayo", 1], ["surgeon", 1]],
  );
  assert.ok(signatures[0].every(({ displayInstanceId, id }) =>
    displayInstanceId === undefined && !id.includes("#")
  ));
});

test("an active exchangeable slot absent from DT is synthesized as observation-only", () => {
  const result = project(runtime([
    capacityBelief("T04", "T04#1", "confirmed", "tray"),
    capacityBelief("T04", "T04#2", "confirmed", "mayo"),
  ], 10_000, { schemaVersion: "taskplanner.exchangeable_tool_capacity_belief.v2" }), [
    placement("T04", ["T04#1"], 1),
  ]);
  const aggregated = aggregate(result, { T04: 2 });

  assert.equal(result.exchangeableActiveCounts.get("T04"), 2);
  assert.deepEqual(
    aggregated.map(({ holderId, quantity }) => [holderId, quantity]).sort(),
    [["mayo", 1], ["rack", 1]],
  );
  assert.ok(aggregated.every((chip) =>
    chip.active === false
    && chip.highlight === "normal"
    && chip.canonicalMayoDecision === undefined
    && chip.displayInstanceId === undefined
    && chip.instanceIds.length === 0
  ));
});

test("an initial-count-zero observation never inherits an unrelated DT card state", () => {
  const unrelatedHandover = {
    ...placement("T02", ["T02#1"], 0),
    holderId: "surgeon",
    holderLabel: "surgeon",
    displayState: "handover",
    lifecycle: "surgeon_handover",
    layoutVariant: "mayoList",
    density: "micro",
    scale: 0.5,
    compact: true,
    highlight: "requested",
    footerBadges: [{ label: "handover", tone: "warning" }],
    contaminated: true,
    active: true,
    s: [true],
  };
  const result = project(runtime([
    capacityBelief("T04", "T04#1", "confirmed", "tray"),
  ], 10_000, { schemaVersion: "taskplanner.exchangeable_tool_capacity_belief.v2" }), [
    unrelatedHandover,
  ]);
  const observed = result.placements.find(({ instrumentId }) => instrumentId === "T04");

  assert.ok(observed);
  assert.equal(observed.holderId, "rack");
  assert.equal(observed.displayState, "waiting");
  assert.equal(observed.lifecycle, "observation_only");
  assert.equal(observed.layoutVariant, "card");
  assert.equal(observed.density, "regular");
  assert.equal(observed.scale, 1);
  assert.equal(observed.compact, false);
  assert.equal(observed.highlight, "normal");
  assert.equal(observed.canonicalMayoDecision, undefined);
  assert.deepEqual(observed.footerBadges, []);
  assert.equal(observed.contaminated, false);
  assert.equal(observed.active, false);
  assert.deepEqual(observed.s, [false]);
});

test("unknown active exchangeable slots disappear instead of falling back to the rack", () => {
  const result = project(runtime([
    capacityBelief("T07", "T07#1", "uncertain", "unknown", "unknown"),
    capacityBelief("T07", "T07#2", "uncertain", "", "unknown", 0, "capacity_slot_inactive"),
  ], 10_000, { schemaVersion: "taskplanner.exchangeable_tool_capacity_belief.v2" }), [
    placement("T07", ["T07#1", "T07#2"], 2),
  ]);

  assert.equal(result.exchangeableActiveCounts.get("T07"), 1);
  assert.equal(result.hiddenInventoryCounts.get("T07"), 1);
  assert.equal(aggregate(result, { T07: 2 }).length, 0);
});

test("panel rows aggregate exchangeable slots by instrument and location without slot ids", async () => {
  const rows = aggregateToolBeliefPanelRows([
    {
      ...capacityBelief("T02", "T02#1", "confirmed", "mayo"),
      mostLikelyProbability: 0.8,
      locations: [
        { locationId: "mayo", probability: 0.8 },
        { locationId: "unknown", probability: 0.2 },
      ],
    },
    {
      ...capacityBelief("T02", "T02#2", "probable", "mayo"),
      mostLikelyProbability: 0.6,
      locations: [
        { locationId: "mayo", probability: 0.6 },
        { locationId: "unknown", probability: 0.4 },
      ],
    },
    capacityBelief("T02", "T02#3", "uncertain", "", "unknown", 0, "capacity_slot_inactive"),
  ]);

  assert.equal(rows.length, 1);
  assert.equal(rows[0].quantity, 2);
  assert.equal(rows[0].exchangeable, true);
  assert.equal(rows[0].tool.instrumentId, "T02");
  assert.equal(rows[0].tool.mostLikelyLocationId, "mayo");
  assert.equal(rows[0].tool.mostLikelyProbability, 0.7);
  assert.equal(rows[0].tool.instanceId, "");
  assert.equal(rows[0].tool.trackId.includes("#"), false);
  const sourceText = await readFile(panelSource, "utf8");
  assert.doesNotMatch(sourceText, /논리 슬롯|Logical slot/);
  assert.doesNotMatch(sourceText, /고정 인벤토리 belief|fixed-inventory belief/);
  assert.match(sourceText, /Digital Twin 상태를 다시 동기화/);
});

test.after(async () => {
  await rm(testBuildDir, { recursive: true, force: true });
});
