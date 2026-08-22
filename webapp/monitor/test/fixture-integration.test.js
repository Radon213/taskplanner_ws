import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { PUBLIC_TOPIC_NAMES } from "../ros/public-contract.js";
import { MainLayoutScenarioMapper } from "../ros/scenario-mapper.js";

function catalogFromCsv(csv) {
  const rows = csv.trim().split(/\r?\n/).slice(1).map((line) => line.split(","));
  const procedure = rows.find(([type]) => type === "procedure");
  return {
    revision: 1,
    catalog_version: "synthetic-catalog-v1",
    procedure_display_name: procedure[3],
    phases: rows.filter(([type]) => type === "phase").map((row) => ({
      ordinal: Number(row[1]),
      phase_id: row[2],
      display_name: row[3],
      display_name_ko: row[4],
      phase_kind: "normal",
    })),
    instruments: rows.filter(([type]) => type === "instrument").map((row) => ({
      instrument_id: row[2],
      display_name: row[3],
    })),
  };
}

test("synthetic handoff fixture drives phase, instrument cards, and Top-3", async () => {
  const [fixtureText, catalogText] = await Promise.all([
    readFile(new URL("../../public/monitor/fixtures/UI_PUBLIC_CONTRACT_FIXTURE.json", import.meta.url), "utf8"),
    readFile(new URL("../../public/monitor/fixtures/thyroidectomy_demo_catalog.csv", import.meta.url), "utf8"),
  ]);
  const fixture = JSON.parse(fixtureText);
  assert.equal(fixture.synthetic, true);

  const mapper = new MainLayoutScenarioMapper();
  mapper.map(PUBLIC_TOPIC_NAMES.catalog, catalogFromCsv(catalogText));
  const context = mapper.map(PUBLIC_TOPIC_NAMES.context, fixture[PUBLIC_TOPIC_NAMES.context]);
  assert.equal(context.patch.phase.code, "P05");
  assert.equal(context.patch.phase.index, 5);
  assert.equal(context.patch.phase.total, 10);
  assert.equal(context.patch.phase.name, "견인 유지 하 표적 조직 조작");
  assert.equal(context.patch.phase.description, "Retraction-supported target manipulation");

  const instruments = mapper.map(PUBLIC_TOPIC_NAMES.instruments, fixture[PUBLIC_TOPIC_NAMES.instruments]);
  assert.deepEqual(instruments.patch.instrumentFlow, {
    inUse: [{
      toolId: "T03",
      instanceId: "T03#1",
      state: "in_use",
      confidence: 0.96,
      evidenceStatus: "DT_ACCEPTED",
    }],
    mayo: [
      {
        toolId: "T04",
        instanceId: "T04#1",
        state: "parked_for_reuse",
        confidence: 0.91,
        evidenceStatus: "DT_ACCEPTED",
      },
      {
        toolId: "T07",
        instanceId: "T07#1",
        state: "awaiting_retrieval",
        confidence: 0.88,
        evidenceStatus: "DT_ACCEPTED",
      },
    ],
  });

  const predictions = mapper.map(
    PUBLIC_TOPIC_NAMES.toolPredictions,
    fixture[PUBLIC_TOPIC_NAMES.toolPredictions],
  );
  assert.deepEqual(predictions.patch.predictions.map(({ toolId, confidence }) => ({
    toolId,
    confidence: Math.round(confidence),
  })), [
    { toolId: "T02", confidence: 86 },
    { toolId: "T04", confidence: 72 },
    { toolId: "T07", confidence: 58 },
  ]);
});
