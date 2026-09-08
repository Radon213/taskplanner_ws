import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import ts from "typescript";

const sourceRoot = new URL("../src/ros/", import.meta.url);
const testBuildDir = await mkdtemp(join(tmpdir(), "taskplanner-tool-belief-test-"));

async function transpileSource(sourceName, outputName) {
  const source = await readFile(new URL(sourceName, sourceRoot), "utf8");
  const result = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2020,
    },
  });
  await writeFile(join(testBuildDir, outputName), result.outputText, "utf8");
}

await transpileSource("rosMessageBounds.ts", "rosMessageBounds.mjs");
await transpileSource("toolBeliefContract.ts", "toolBeliefContract.mjs");
await transpileSource("toolBeliefMessages.ts", "toolBeliefMessages.mjs");
const beliefModulePath = join(testBuildDir, "toolBeliefMessages.mjs");
const beliefModuleSource = await readFile(beliefModulePath, "utf8");
await writeFile(
  beliefModulePath,
  beliefModuleSource
    .replace('"./rosMessageBounds"', '"./rosMessageBounds.mjs"')
    .replace('"./toolBeliefContract"', '"./toolBeliefContract.mjs"'),
  "utf8",
);
const {
  default: createToolBeliefSubscription,
  normalizeToolBeliefSnapshot,
  reduceToolBeliefSnapshot,
} = await import(pathToFileURL(beliefModulePath));

function validMessage() {
  return {
    header: { stamp: { sec: 10, nanosec: 20 }, frame_id: "map" },
    schema_version: "taskplanner.fixed_inventory_tool_belief.v1",
    procedure_id: "thyroidectomy_demo",
    procedure_run_id: "run-1",
    tracker_revision: "fixed-inventory-v1",
    observation_only: true,
    robot_motion_active: false,
    robot_motion_negative_scale: 0.1,
    ignored_out_of_inventory_count: 0,
    ignored_class_names: [],
    tools: [{
      track_id: "T02#1",
      instrument_id: "T02",
      instance_id: "1",
      display_name: "Adson",
      existence_probability: 1,
      status: "uncertain",
      committed_location_id: "",
      committed_location_probability: 0,
      most_likely_location_id: "tray",
      most_likely_probability: 0.7,
      locations: [
        { location_id: "tray", probability: 0.7 },
        { location_id: "unknown", probability: 0.3 },
      ],
      last_positive_age_sec: -1,
      evidence_sources: ["scenario_initial"],
      motion_mode: "idle",
      status_flags: ["fixed_inventory", "observation_only"],
    }],
  };
}

test("accepts an empty committed location below the release threshold", () => {
  const snapshot = normalizeToolBeliefSnapshot(validMessage(), 1234);
  assert.ok(snapshot);
  assert.equal(snapshot.tools[0].committedLocationId, "");
  assert.equal(snapshot.tools[0].mostLikelyLocationId, "tray");
  assert.equal(snapshot.tools[0].lastPositiveAgeSec, null);
  assert.equal(snapshot.receivedAt, 1234);
});

test("rejects a non-fixed existence probability", () => {
  const message = validMessage();
  message.tools[0].existence_probability = 0.9;
  assert.equal(normalizeToolBeliefSnapshot(message), null);
});

test("unknown schema versions cannot weaken the fixed-row existence invariant", () => {
  const message = validMessage();
  message.schema_version = "taskplanner.future_tool_belief.v99";
  message.tools[0].existence_probability = 0.9;
  assert.equal(normalizeToolBeliefSnapshot(message), null);
});

test("rejects forged partial capacity flags even when existence is one", () => {
  const message = validMessage();
  message.schema_version = "taskplanner.exchangeable_tool_capacity_belief.v2";
  message.tools[0].status_flags = ["capacity_slot_active"];
  assert.equal(normalizeToolBeliefSnapshot(message), null);
});

test("exact fixed v1 rejects a complete capacity row even when existence is one", () => {
  const message = validMessage();
  message.tools[0].status_flags = [
    "scenario_bounded_capacity",
    "observation_only",
    "logical_instance_exchangeable",
    "physical_identity_not_asserted",
    "capacity_slot_active",
  ];
  assert.equal(normalizeToolBeliefSnapshot(message), null);

  const optionalFlagOnly = validMessage();
  optionalFlagOnly.tools[0].status_flags = [
    "fixed_inventory",
    "observation_only",
    "exchangeable_slot_assigned",
  ];
  assert.equal(normalizeToolBeliefSnapshot(optionalFlagOnly), null);
});

test("accepts bounded existence for the exact scenario-capacity flags without weakening fixed v1", () => {
  const message = validMessage();
  message.schema_version = "taskplanner.exchangeable_tool_capacity_belief.v2";
  message.tools[0].existence_probability = 0.65;
  message.tools[0].status_flags = [
    "scenario_bounded_capacity",
    "observation_only",
    "logical_instance_exchangeable",
    "physical_identity_not_asserted",
    "capacity_slot_probable",
    "exchangeable_slot_assigned",
  ];
  const snapshot = normalizeToolBeliefSnapshot(message);
  assert.ok(snapshot);
  assert.equal(snapshot.tools[0].existenceProbability, 0.65);
});

test("unknown future schemas accept only a complete capacity contract", () => {
  const message = validMessage();
  message.schema_version = "taskplanner.future_capacity_belief.v99";
  message.tools[0].existence_probability = 0.35;
  message.tools[0].status_flags = [
    "scenario_bounded_capacity",
    "logical_instance_exchangeable",
    "physical_identity_not_asserted",
    "capacity_slot_probable",
    "exchangeable_slot_rebound",
  ];
  const snapshot = normalizeToolBeliefSnapshot(message);
  assert.ok(snapshot);
  assert.equal(snapshot.tools[0].existenceProbability, 0.35);

  const partial = validMessage();
  partial.schema_version = "taskplanner.future_capacity_belief.v99";
  partial.tools[0].status_flags = [
    "scenario_bounded_capacity",
    "capacity_slot_active",
  ];
  assert.equal(normalizeToolBeliefSnapshot(partial), null);
});

test("retains an inactive capacity slot in the normalized data contract", () => {
  const message = validMessage();
  message.schema_version = "taskplanner.exchangeable_tool_capacity_belief.v2";
  message.tools[0].existence_probability = 0;
  message.tools[0].status_flags = [
    "scenario_bounded_capacity",
    "observation_only",
    "logical_instance_exchangeable",
    "physical_identity_not_asserted",
    "capacity_slot_inactive",
  ];
  const snapshot = normalizeToolBeliefSnapshot(message);
  assert.ok(snapshot);
  assert.equal(snapshot.tools.length, 1);
  assert.equal(snapshot.tools[0].existenceProbability, 0);
  assert.ok(snapshot.tools[0].statusFlags.includes("capacity_slot_inactive"));
});

test("rejects out-of-range existence in the capacity schema", () => {
  const message = validMessage();
  message.schema_version = "taskplanner.exchangeable_tool_capacity_belief.v2";
  message.tools[0].existence_probability = 1.01;
  message.tools[0].status_flags = [
    "scenario_bounded_capacity",
    "logical_instance_exchangeable",
    "capacity_slot_active",
  ];
  assert.equal(normalizeToolBeliefSnapshot(message), null);
});

test("rejects capacity rows with more than one lifecycle state", () => {
  const message = validMessage();
  message.schema_version = "taskplanner.exchangeable_tool_capacity_belief.v2";
  message.tools[0].existence_probability = 0.5;
  message.tools[0].status_flags = [
    "scenario_bounded_capacity",
    "logical_instance_exchangeable",
    "physical_identity_not_asserted",
    "capacity_slot_active",
    "capacity_slot_probable",
  ];
  assert.equal(normalizeToolBeliefSnapshot(message), null);
});

test("rejects a non-observation-only payload", () => {
  const message = validMessage();
  message.observation_only = false;
  assert.equal(normalizeToolBeliefSnapshot(message), null);
});

test("rejects the removed synthetic occluded status", () => {
  const message = validMessage();
  message.tools[0].status = "occluded";
  assert.equal(normalizeToolBeliefSnapshot(message), null);
});

test("rejects a location vector whose probabilities do not sum to one", () => {
  const message = validMessage();
  message.tools[0].locations[1].probability = 0.1;
  assert.equal(normalizeToolBeliefSnapshot(message), null);
});

test("rejects duplicate location ids instead of hiding their mass", () => {
  const message = validMessage();
  message.tools[0].locations = [
    { location_id: "tray", probability: 0.5 },
    { location_id: "tray", probability: 0.5 },
    { location_id: "unknown", probability: 0.5 },
  ];
  message.tools[0].most_likely_probability = 0.5;
  assert.equal(normalizeToolBeliefSnapshot(message), null);
});

test("rejects a most-likely summary that disagrees with the vector", () => {
  const message = validMessage();
  message.tools[0].most_likely_location_id = "unknown";
  message.tools[0].most_likely_probability = 0.3;
  assert.equal(normalizeToolBeliefSnapshot(message), null);
});

test("rejects a nonzero probability for an empty committed location", () => {
  const message = validMessage();
  message.tools[0].committed_location_probability = 0.7;
  assert.equal(normalizeToolBeliefSnapshot(message), null);
});

test("normalizes harmless float transport drift and derives consistent summaries", () => {
  const message = validMessage();
  message.tools[0].locations[0].probability = 0.7000001;
  message.tools[0].most_likely_probability = 0.7000001;
  const snapshot = normalizeToolBeliefSnapshot(message);
  assert.ok(snapshot);
  const tool = snapshot.tools[0];
  const total = tool.locations.reduce((sum, location) => sum + location.probability, 0);
  assert.ok(Math.abs(total - 1) < 1e-12);
  assert.equal(
    tool.mostLikelyProbability,
    tool.locations.find((location) => location.locationId === "tray").probability,
  );
});

test("reducer keeps the current snapshot for malformed or older payloads", () => {
  const current = normalizeToolBeliefSnapshot(validMessage(), 1234);
  assert.ok(current);
  assert.equal(reduceToolBeliefSnapshot(current, { tools: "invalid" }), current);

  const older = validMessage();
  older.header.stamp = { sec: 9, nanosec: 999 };
  assert.equal(reduceToolBeliefSnapshot(current, older), current);
});

test("Live subscription factory uses the exact contract and stops stale generations", () => {
  const subscriptions = new Map();
  const topicOptions = [];
  let runtime = null;
  let isCurrent = true;
  class FakeTopic {
    constructor(value) {
      this.name = value.name;
      topicOptions.push(value);
    }

    subscribe(callback) {
      subscriptions.set(this.name, callback);
    }

    unsubscribe() {}
  }
  const ros = {
    on() {},
    off() {},
    callOnConnection() {},
  };
  const topic = createToolBeliefSubscription(
    FakeTopic,
    ros,
    () => isCurrent,
    (update) => { runtime = update; },
  );
  assert.ok(topic);
  assert.equal(topicOptions[0].name, "/surgery/perception/tool_beliefs");
  assert.equal(
    topicOptions[0].messageType,
    "surgical_perception_msgs/msg/TrackedToolBeliefArray",
  );
  assert.equal(
    topicOptions[1].name,
    "/surgery/perception/tool_beliefs/enabled",
  );
  assert.equal(topicOptions[1].messageType, "std_msgs/msg/Bool");
  subscriptions.get("/surgery/perception/tool_beliefs/enabled")({ data: true });
  subscriptions.get("/surgery/perception/tool_beliefs")(validMessage());
  assert.ok(runtime.snapshot);
  assert.equal(runtime.enabled, true);

  isCurrent = false;
  const newer = validMessage();
  newer.header.stamp = { sec: 11, nanosec: 0 };
  subscriptions.get("/surgery/perception/tool_beliefs")(newer);
  assert.equal(runtime.snapshot.header.stamp.sec, 10);
  assert.equal(
    createToolBeliefSubscription(
      FakeTopic,
      {},
      () => false,
      () => {},
    ),
    null,
  );
});

test("SetBool resolves only after ACK plus authoritative status and clears off snapshots", async () => {
  const subscriptions = new Map();
  const listeners = new Map();
  let runtime = null;
  let serviceRequest = null;
  class FakeTopic {
    constructor(value) { this.name = value.name; }
    subscribe(callback) { subscriptions.set(this.name, callback); }
    unsubscribe() {}
  }
  const ros = {
    on(event, callback) { listeners.set(event, callback); },
    off(event) { listeners.delete(event); },
    callOnConnection(request) {
      serviceRequest = request;
      listeners.get(request.id)({
        result: true,
        values: { success: true, message: "accepted" },
      });
    },
  };
  createToolBeliefSubscription(
    FakeTopic,
    ros,
    () => true,
    (update) => { runtime = update; },
  );
  const enabledStatus = subscriptions.get("/surgery/perception/tool_beliefs/enabled");
  enabledStatus({ data: true });
  subscriptions.get("/surgery/perception/tool_beliefs")(validMessage());
  const pending = runtime.setEnabled(false);
  assert.equal(serviceRequest.service, "/surgery/perception/tool_beliefs/set_enabled");
  assert.equal(serviceRequest.args.data, false);
  let settled = false;
  void pending.then(() => { settled = true; });
  await Promise.resolve();
  assert.equal(settled, false);
  enabledStatus({ data: false });
  assert.deepEqual(await pending, { accepted: true, message: "accepted" });
  assert.equal(runtime.enabled, false);
  assert.equal(runtime.snapshot, null);
});

test.after(async () => {
  await rm(testBuildDir, { recursive: true, force: true });
});
