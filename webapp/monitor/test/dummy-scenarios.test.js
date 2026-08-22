import test from "node:test";
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";

import { replayDummyFixture } from "../ros/dummy-fixture.js";
import { PUBLIC_TOPIC_NAMES } from "../ros/public-contract.js";
import { validateDummyFixture } from "../runtime-settings.js";

const SCENARIO_DIRECTORY = new URL("../../public/monitor/dummy-scenarios/", import.meta.url);
const BUNDLED_FIXTURE_URL = new URL("../../public/monitor/dummy-data.json", import.meta.url);

const EXPECTED_SCENARIOS = Object.freeze([
  "01-preparation-empty.json",
  "02-incision-single-tool.json",
  "03-multi-tool.json",
  "04-retrieval-queue.json",
  "05-health-warning.json",
  "06-idle.json",
]);

const ALLOWED_ROOT_KEYS = new Set([
  "fixture_schema",
  "synthetic",
  "usage",
  "baseline",
  PUBLIC_TOPIC_NAMES.gatewayInfo,
  PUBLIC_TOPIC_NAMES.catalog,
  PUBLIC_TOPIC_NAMES.context,
  PUBLIC_TOPIC_NAMES.instruments,
  PUBLIC_TOPIC_NAMES.robots,
  PUBLIC_TOPIC_NAMES.robotEndEffectors,
  PUBLIC_TOPIC_NAMES.toolPredictions,
  PUBLIC_TOPIC_NAMES.speech,
  PUBLIC_TOPIC_NAMES.health,
]);

const REQUIRED_SCENARIO_TOPICS = Object.freeze([
  PUBLIC_TOPIC_NAMES.context,
  PUBLIC_TOPIC_NAMES.instruments,
  PUBLIC_TOPIC_NAMES.toolPredictions,
]);

async function readJson(url) {
  return JSON.parse(await readFile(url, "utf8"));
}

async function discoverScenarios() {
  const entries = await readdir(SCENARIO_DIRECTORY, { withFileTypes: true });
  return entries
    .filter((entry) => entry.isFile() && entry.name.endsWith(".json"))
    .map((entry) => entry.name)
    .sort();
}

function uiSignature(patch = {}) {
  const summarizeRows = (rows) => (rows || []).map((row) => [
    row.toolId,
    row.state,
    row.confidence,
  ]);
  return JSON.stringify({
    phase: [
      patch.phase?.code,
      patch.phase?.index,
      patch.phase?.uncertain,
      patch.phase?.executionState,
    ],
    inUse: summarizeRows(patch.instrumentFlow?.inUse),
    mayo: summarizeRows(patch.instrumentFlow?.mayo),
    arms: [1, 2, 3, 4].map((arm) => [
      patch.arms?.[arm]?.status,
      patch.arms?.[arm]?.toolId,
    ]),
    predictions: (patch.predictions || []).map((row) => [
      row.rank,
      row.toolId,
      row.confidence,
    ]),
    voice: [patch.voice?.status, patch.voice?.text],
  });
}

test("all public Dummy scenarios are safe public-contract fixtures and replay successfully", async () => {
  const scenarioNames = await discoverScenarios();
  assert.deepEqual(
    scenarioNames,
    EXPECTED_SCENARIOS,
    "the documented selectable Dummy scenario set must stay complete and deterministic",
  );

  const bundledFixture = validateDummyFixture(await readJson(BUNDLED_FIXTURE_URL));
  for (const name of scenarioNames) {
    const raw = await readJson(new URL(name, SCENARIO_DIRECTORY));

    assert.equal(raw.fixture_schema, "taskplanner.ui_contract_fixture.v1", `${name}: fixture schema`);
    assert.equal(raw.synthetic, true, `${name}: real records must never be used as Dummy data`);
    assert.match(raw.usage || "", /dummy\s+scenario/i, `${name}: Dummy scenario purpose notice`);

    for (const key of Object.keys(raw)) {
      assert.ok(ALLOWED_ROOT_KEYS.has(key), `${name}: unsupported or private root/topic key ${key}`);
    }
    for (const topic of REQUIRED_SCENARIO_TOPICS) {
      assert.equal(typeof raw[topic], "object", `${name}: missing ${topic}`);
      assert.ok(!Array.isArray(raw[topic]), `${name}: ${topic} must be a snapshot object`);
    }

    const fixture = validateDummyFixture(raw);
    assert.equal(
      fixture.baseline.procedure_type,
      bundledFixture.baseline.procedure_type,
      `${name}: bundled catalog fallback requires matching procedure_type`,
    );
    assert.equal(
      fixture.baseline.catalog_version,
      bundledFixture.baseline.catalog_version,
      `${name}: bundled catalog fallback requires matching catalog_version`,
    );

    const replay = replayDummyFixture(fixture, { fallbackFixture: bundledFixture });
    assert.equal(replay.meta.catalogSource, "bundled", `${name}: scenario must use the bundled catalog`);
    assert.ok(replay.meta.replayedTopics.includes(PUBLIC_TOPIC_NAMES.catalog), `${name}: catalog replay`);
    for (const topic of REQUIRED_SCENARIO_TOPICS) {
      assert.ok(replay.meta.replayedTopics.includes(topic), `${name}: replay ${topic}`);
    }
    assert.ok(Number(replay.patch.phase?.total) > 0, `${name}: phase frames come from the catalog`);
    assert.ok(Array.isArray(replay.patch.instrumentFlow?.inUse), `${name}: In Use rows`);
    assert.ok(Array.isArray(replay.patch.instrumentFlow?.mayo), `${name}: Mayo rows`);
    assert.ok(Array.isArray(replay.patch.predictions), `${name}: prediction rows`);
  }
});

test("the selectable Dummy set exercises meaningfully different UI states", async () => {
  const [scenarioNames, bundledFixture] = await Promise.all([
    discoverScenarios(),
    readJson(BUNDLED_FIXTURE_URL),
  ]);
  const replays = await Promise.all(scenarioNames.map(async (name) => ({
    name,
    replay: replayDummyFixture(
      await readJson(new URL(name, SCENARIO_DIRECTORY)),
      { fallbackFixture: bundledFixture },
    ),
  })));

  const signatures = replays.map(({ replay }) => uiSignature(replay.patch));
  assert.equal(
    new Set(signatures).size,
    replays.length,
    "every file must produce a distinct visible phase/tool/arm/prediction/speech state",
  );

  const patches = replays.map(({ replay }) => replay.patch);
  const phaseIndexes = patches.map((patch) => Number(patch.phase?.index) || 0);
  const inUseCounts = patches.map((patch) => patch.instrumentFlow.inUse.length);
  const mayoCounts = patches.map((patch) => patch.instrumentFlow.mayo.length);
  const predictionCounts = patches.map((patch) => patch.predictions.length);

  assert.ok(new Set(phaseIndexes).size >= 4, "scenarios must cover several procedure stages");
  assert.ok(phaseIndexes.includes(0), "scenarios must cover waiting/idle phase highlighting");
  assert.ok(phaseIndexes.includes(1), "scenarios must cover the preparation phase");
  assert.ok(patches.some((patch) => patch.phase?.uncertain === true), "uncertain phase styling must be covered");
  assert.ok(inUseCounts.includes(0) && inUseCounts.some((count) => count >= 2), "In Use must cover None and multiple rows");
  assert.ok(mayoCounts.includes(0) && mayoCounts.some((count) => count > 0), "Mayo must cover None and populated rows");
  assert.ok(
    predictionCounts.includes(0) && predictionCounts.includes(1) && predictionCounts.includes(3),
    "predictions must cover None, one row, and the full Top-3",
  );
});
