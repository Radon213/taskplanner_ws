import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import ts from "typescript";

const source = new URL("../src/presentation/toolCardPresentation.ts", import.meta.url);
const displaySource = new URL("../src/utils/display.ts", import.meta.url);
const testBuildDir = await mkdtemp(join(tmpdir(), "taskplanner-tool-card-presentation-test-"));
const outputPath = join(testBuildDir, "toolCardPresentation.mjs");
const transpiled = ts.transpileModule(await readFile(source, "utf8"), {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2020,
  },
});
await writeFile(
  outputPath,
  transpiled.outputText.replace('"../utils/display"', '"./display.mjs"'),
  "utf8",
);
const displayTranspiled = ts.transpileModule(await readFile(displaySource, "utf8"), {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2020,
  },
});
await writeFile(join(testBuildDir, "display.mjs"), displayTranspiled.outputText, "utf8");
const policySource = new URL("../src/ros/toolPolicyMessages.ts", import.meta.url);
const policyTranspiled = ts.transpileModule(await readFile(policySource, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
});
await writeFile(
  join(testBuildDir, "toolPolicyMessages.mjs"),
  policyTranspiled.outputText.replace('"../utils/display"', '"./display.mjs"'),
  "utf8",
);
const { activeToolPolicyStatus, configureToolPolicySubscription, normalizeToolPolicyStatus } =
  await import(pathToFileURL(join(testBuildDir, "toolPolicyMessages.mjs")));

const {
  mayoObservedToolIds,
  projectToolCardPresentation,
  systemToolPredictionsById,
  toolDemandForecastById,
} = await import(pathToFileURL(outputPath));

test.after(async () => {
  await rm(testBuildDir, { force: true, recursive: true });
});

function chip(holderId) {
  return {
    holderId,
    instrumentId: "T02",
  };
}

test("tool demand forecast decodes bounded [tool, probability] rows only", () => {
  const forecasts = toolDemandForecastById({
    raw_json: JSON.stringify({
      tool_demand_forecast: [
        ["T02", 0.63],
        ["T02", 0.71],
        ["T04", 0],
        ["T07", 1.1],
        ["T08"],
        ["", 0.5],
      ],
    }),
  });

  assert.deepEqual([...forecasts.entries()], [
    ["T02", 0.71],
    ["T04", 0],
  ]);
});

test("Mayo observer sidecar exposes only current observation identity", () => {
  const observed = mayoObservedToolIds({
    raw_json: JSON.stringify({
      mayo: [["T02", "reuse", 0.99]],
      mayo_observation: [
        ["T04", "recover", 0.23],
        ["T07", "reuse", 2],
      ],
    }),
  });

  assert.deepEqual([...observed], ["T04"]);
  assert.equal(observed.has("T02"), false);
});

test("Mayo reuse forecast uses demand sidecar, never camera confidence", () => {
  const forecasts = toolDemandForecastById({
    raw_json: JSON.stringify({ tool_demand_forecast: [["T02", 0.42]] }),
  });
  const observed = mayoObservedToolIds({
    raw_json: JSON.stringify({ mayo_observation: [["T02", "reuse", 0.93]] }),
  });
  const result = projectToolCardPresentation({
    chip: chip("mayo"),
    procedureRunning: true,
    demandForecastReady: true,
    demandForecastByToolId: forecasts,
    mayoObservedToolIdSet: observed,
    systemPrediction: { rank: 1, confidence: 0.91 },
  });

  assert.equal(result.evidence.demandForecastProbability, 0.42);
  assert.equal(result.evidence.mayoObserved, true);
  assert.equal(result.evidence.nextToolProbability, null);
  assert.equal(result.evidence.nextToolRank, null);
});

test("Mayo forecast and observation disappear outside running fresh VLM context", () => {
  const forecasts = new Map([["T02", 0.72]]);
  const observed = new Set(["T02"]);
  const stopped = projectToolCardPresentation({
    chip: chip("mayo"),
    procedureRunning: false,
    demandForecastReady: true,
    demandForecastByToolId: forecasts,
    mayoObservedToolIdSet: observed,
  });
  const stale = projectToolCardPresentation({
    chip: chip("mayo"),
    procedureRunning: true,
    demandForecastReady: false,
    demandForecastByToolId: forecasts,
    mayoObservedToolIdSet: observed,
  });

  for (const result of [stopped, stale]) {
    assert.equal(result.evidence.demandForecastProbability, null);
    assert.equal(result.evidence.mayoObserved, false);
  }
});

test("a final non-Mayo location keeps the system next-tool rank", () => {
  const result = projectToolCardPresentation({
    chip: chip("rack"),
    procedureRunning: true,
    demandForecastReady: true,
    demandForecastByToolId: new Map([["T02", 0.82]]),
    mayoObservedToolIdSet: new Set(["T02"]),
    systemPrediction: { rank: 2, confidence: 0.77 },
  });

  assert.equal(result.evidence.nextToolProbability, 0.77);
  assert.equal(result.evidence.nextToolRank, 2);
  assert.equal(result.evidence.demandForecastProbability, null);
  assert.equal(result.evidence.mayoObserved, false);
});

test("stopped procedure never renders retained prediction or Mayo action tags", () => {
  const result = projectToolCardPresentation({
    chip: {
      ...chip("mayo"),
      canonicalMayoDecision: "recover",
      canonicalMayoDecisionConfidence: 0.91,
    },
    procedureRunning: false,
    demandForecastReady: true,
    demandForecastByToolId: new Map([["T02", 0.82]]),
    mayoObservedToolIdSet: new Set(["T02"]),
    systemPrediction: { rank: 1, confidence: 0.88 },
  });

  assert.equal(result.evidence.nextToolProbability, null);
  assert.equal(result.evidence.nextToolRank, null);
  assert.equal(result.evidence.nextToolConfirmation, null);
  assert.equal(result.evidence.mayoConfirmation, null);
  assert.equal(result.evidence.demandForecastProbability, null);
  assert.equal(result.evidence.mayoObserved, false);
});

test("system prediction projection keeps only valid unique top-three entries", () => {
  const result = systemToolPredictionsById([
    { instrument_id: "T02", rank: 2, confidence: 0.6 },
    { instrument_id: "T02", rank: 1, confidence: 0.9 },
    { instrument_id: "T04", rank: 3, confidence: 0.5 },
    { instrument_id: "T07", rank: 4, confidence: 0.8 },
  ]);

  assert.deepEqual([...result.entries()], [
    ["T02", { rank: 1, confidence: 0.9, stabilitySec: 0 }],
    ["T04", { rank: 3, confidence: 0.5, stabilitySec: 0 }],
  ]);
});

function policyStatus() {
  return {
    schema: "taskplanner.tool_policy_status.v1",
    source: "handover_ngram_0704",
    procedure_id: "thyroidectomy_demo",
    procedure_run_id: "run-1",
    running: true,
    execution_state: "running",
    prepare: {
      probability_threshold: 0.2, comparison: "gte", dwell_sec: 0.8,
      candidate: { instrument_id: "T02", probability: 0.25, stability_sec: 0.2 },
    },
    recovery: {
      probability_threshold: 0.3, comparison: "lte", dwell_sec: 0.6,
      enabled_instrument_ids: ["T02"],
      candidates: [{ instrument_id: "T02", instance_id: "T02#1", probability: 0.1, stability_sec: 0.2 }],
    },
  };
}

test("read-only policy decoder retains live values without UI defaults", () => {
  const status = policyStatus();
  assert.deepEqual(normalizeToolPolicyStatus({ data: JSON.stringify(status) }), status);
  assert.equal(normalizeToolPolicyStatus({ data: "{}" }), null);
  status.prepare.probability_threshold = 1.2;
  assert.equal(normalizeToolPolicyStatus({ data: JSON.stringify(status) }), null);
  status.prepare.probability_threshold = 0.2;
  status.recovery.dwell_sec = 0;
  assert.equal(normalizeToolPolicyStatus({ data: JSON.stringify(status) }), null);
});

test("late/replayed policy is never used for a different run or inactive lifecycle", () => {
  const status = policyStatus();
  assert.equal(activeToolPolicyStatus(status, status), status);
  for (const override of [
    { procedure_run_id: "run-2" }, { procedure_run_id: "" },
    { procedure_id: "other" }, { running: false }, { execution_state: "paused" },
  ]) {
    assert.equal(activeToolPolicyStatus(status, { ...status, ...override }), null);
  }
  assert.equal(activeToolPolicyStatus({ ...status, execution_state: "paused" }, status), null);
});

test("policy subscription requests the latest retained snapshot on every subscribe", () => {
  const sent = [];
  const topic = { callForSubscribeAndAdvertise: (request) => sent.push(request) };
  configureToolPolicySubscription(topic);
  topic.callForSubscribeAndAdvertise({ op: "subscribe", topic: "/twin/tool_policy_status" });
  topic.callForSubscribeAndAdvertise({ op: "subscribe", topic: "/twin/tool_policy_status" });
  topic.callForSubscribeAndAdvertise({ op: "unsubscribe", id: "test" });
  assert.equal(sent[0].qos.durability, "transient_local");
  assert.equal(sent[0].qos.depth, 1);
  assert.deepEqual(sent[1], sent[0]);
  assert.deepEqual(sent[2], { op: "unsubscribe", id: "test" });
});

test("preparation countdown uses live n-gram threshold and duration, not legacy constants", () => {
  const status = policyStatus();
  const args = { chip: chip("rack"), procedureRunning: true, toolPolicyStatus: status };
  let timer = projectToolCardPresentation(args).evidence.nextToolConfirmation;
  assert.equal(timer.confidenceThreshold, 0.2);
  assert.equal(timer.requiredStabilitySec, 0.8);
  assert.ok(Math.abs(timer.remainingSec - 0.6) < 1e-9);
  assert.equal(timer.progress, 0.25);
  status.prepare.dwell_sec = 0.2;
  timer = projectToolCardPresentation(args).evidence.nextToolConfirmation;
  assert.equal(timer.ready, true);
  status.prepare.probability_threshold = 0.3;
  assert.equal(projectToolCardPresentation(args).evidence.nextToolConfirmation, null);
});

test("Mayo recovery timer uses low n-gram probability and exact instance dwell", () => {
  const status = policyStatus();
  const args = {
    chip: { ...chip("mayo"), instanceIds: ["T02#1"], canonicalMayoDecision: {
      disposition: "reuse", confidence: 0.99, stabilitySec: 99,
    } },
    procedureRunning: true, toolPolicyStatus: status,
    demandForecastReady: true, demandForecastByToolId: new Map([["T02", 0.87]]),
  };
  const result = projectToolCardPresentation(args);
  const timer = result.evidence.mayoConfirmation;
  assert.equal(timer.kind, "recovery");
  assert.equal(timer.confidence, 0.1);
  assert.equal(timer.confidenceThreshold, 0.3);
  assert.equal(timer.probabilityComparison, "lte");
  assert.ok(Math.abs(timer.remainingSec - 0.4) < 1e-9);
  assert.equal(result.evidence.demandForecastProbability, 0.87);
  assert.equal(projectToolCardPresentation({
    ...args, chip: { ...args.chip, instanceIds: ["T02#2"] },
  }).evidence.mayoConfirmation, null);
  status.recovery.candidates[0].probability = 0.31;
  assert.equal(projectToolCardPresentation(args).evidence.mayoConfirmation, null);
});

test("missing or paused DT policy hides timers, not existing demand or prediction badges", () => {
  const args = {
    chip: chip("rack"), procedureRunning: true,
    systemPrediction: { rank: 1, confidence: 0.99, stabilitySec: 99 },
  };
  for (const status of [undefined, null, { ...policyStatus(), execution_state: "paused" }]) {
    const evidence = projectToolCardPresentation({ ...args, toolPolicyStatus: status }).evidence;
    assert.equal(evidence.nextToolProbability, 0.99);
    assert.equal(evidence.nextToolConfirmation, null);
    assert.equal(evidence.mayoConfirmation, null);
  }
});
