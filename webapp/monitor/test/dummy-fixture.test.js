import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

function replayPatch(result) {
  return result?.patch ?? result?.statePatch ?? result?.state ?? result;
}

test("the bundled full Dummy fixture replays gateway and catalog topics through the scenario mapper", async () => {
  const [{ replayDummyFixture }, source, fixtureText] = await Promise.all([
    import("../ros/dummy-fixture.js"),
    readFile(new URL("../ros/dummy-fixture.js", import.meta.url), "utf8"),
    readFile(new URL("../../public/monitor/dummy-data.json", import.meta.url), "utf8"),
  ]);
  assert.equal(typeof replayDummyFixture, "function");
  assert.match(source, /MainLayoutScenarioMapper/);
  assert.match(source, /\.map\(/, "fixture topics must use the production scenario mapper");

  const fixture = JSON.parse(fixtureText);
  assert.equal(fixture.fixture_schema, "taskplanner.ui_contract_fixture.v1");
  assert.equal(fixture.synthetic, true);
  assert.equal(typeof fixture["/surgery/gateway_info"], "object");
  assert.equal(typeof fixture["/surgery/catalog"], "object");

  const replay = replayDummyFixture(fixture);
  const patch = replayPatch(replay);
  assert.ok(patch && typeof patch === "object");
  assert.ok(Number(patch.phase?.total) > 0, "catalog phases must become UI phase segments");
  assert.ok(Number(patch.phase?.index) > 0, "context.current_phase must select a catalog phase");
  assert.notEqual(patch.phase?.name, "Waiting for phase data");
  assert.ok(patch.procedure?.name && patch.procedure.name !== "None");
  assert.ok(Array.isArray(patch.instrumentFlow?.inUse));
  assert.ok(Array.isArray(patch.instrumentFlow?.mayo));
  assert.ok(Array.isArray(patch.predictions));
  assert.ok(Array.isArray(replay.tools), "catalog/tool snapshots must expose tool definitions for rendering");
  assert.ok(replay.tools.length > 0);
});

test("a three-topic UI fixture joins only a matching bundled catalog fallback", async () => {
  const [{ replayDummyFixture }, minimalText, bundledText] = await Promise.all([
    import("../ros/dummy-fixture.js"),
    readFile(new URL("../../public/monitor/fixtures/UI_PUBLIC_CONTRACT_FIXTURE.json", import.meta.url), "utf8"),
    readFile(new URL("../../public/monitor/dummy-data.json", import.meta.url), "utf8"),
  ]);
  const minimal = JSON.parse(minimalText);
  const bundled = JSON.parse(bundledText);
  const topicKeys = Object.keys(minimal).filter((key) => key.startsWith("/surgery/"));
  assert.deepEqual(topicKeys.sort(), [
    "/surgery/context",
    "/surgery/instruments",
    "/surgery/tool_predictions",
  ]);

  const replay = replayDummyFixture(minimal, { fallbackFixture: bundled });
  const patch = replayPatch(replay);
  assert.equal(patch.phase.index, 5);
  assert.equal(patch.phase.total, 10);
  assert.equal(patch.phase.name, "견인 유지 하 표적 조직 조작");
  assert.equal(patch.phase.description, "Retraction-supported target manipulation");
  assert.deepEqual(patch.instrumentFlow.inUse.map(({ toolId }) => toolId), ["T03"]);
  assert.deepEqual(patch.instrumentFlow.mayo.map(({ toolId }) => toolId), ["T04", "T07"]);
  assert.deepEqual(patch.predictions.map(({ toolId }) => toolId), ["T02", "T04", "T07"]);
  assert.equal(replay.tools.find(({ id }) => id === "T02")?.name, "Adson Forceps");

  const mismatchedFallback = structuredClone(bundled);
  mismatchedFallback.baseline.procedure_type = "different_procedure";
  if (mismatchedFallback["/surgery/catalog"]) {
    mismatchedFallback["/surgery/catalog"].procedure_type = "different_procedure";
  }
  assert.throws(
    () => replayDummyFixture(minimal, { fallbackFixture: mismatchedFallback }),
    /procedure_type|catalog_version|catalog|카탈로그/i,
    "a foreign procedure catalog must never be combined with the selected fixture",
  );
});

test("replay rejects a topic snapshot that the production mapper cannot accept", async () => {
  const { replayDummyFixture } = await import("../ros/dummy-fixture.js");
  const fixtureText = await readFile(
    new URL("../../public/monitor/fixtures/UI_PUBLIC_CONTRACT_FIXTURE.json", import.meta.url),
    "utf8",
  );
  const fixture = JSON.parse(fixtureText);
  fixture["/surgery/instruments"] = { revision: 1002, instruments: "invalid" };
  assert.throws(
    () => replayDummyFixture(fixture),
    /fixture|topic|instrument|mapper|invalid|reject/i,
  );
});
