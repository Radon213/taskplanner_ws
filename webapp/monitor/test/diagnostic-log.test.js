import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { createDiagnosticLog } from "../ros/diagnostic-log.js";

test("diagnostic log records deterministic structured entries and routes console levels", () => {
  const consoleCalls = [];
  const consoleRef = Object.fromEntries(
    ["debug", "info", "warn", "error"].map((level) => [
      level,
      (...args) => consoleCalls.push([level, ...args]),
    ]),
  );
  const diagnostics = createDiagnosticLog({
    now: () => Date.parse("2026-08-18T07:00:00.000Z"),
    consoleRef,
  });

  const first = diagnostics.record("info", "connection.status", {
    state: "connected",
    retryAttempt: 0,
  });
  const second = diagnostics.record("warn", "topic.stale", {
    topic: "/surgery/context",
    ageMs: 3501,
  });

  assert.deepEqual(first, {
    sequence: 1,
    timestamp: "2026-08-18T07:00:00.000Z",
    level: "info",
    event: "connection.status",
    details: { state: "connected", retryAttempt: 0 },
  });
  assert.equal(Object.isFrozen(first), true);
  assert.equal(second.sequence, 2);
  assert.deepEqual(consoleCalls.map(([level, label]) => [level, label]), [
    ["info", "[SurgiMate] connection.status"],
    ["warn", "[SurgiMate] topic.stale"],
  ]);
  assert.equal(diagnostics.size, 2);

  const snapshot = diagnostics.entries();
  snapshot[0].details.state = "tampered";
  assert.equal(diagnostics.entries()[0].details.state, "connected");

  diagnostics.clear();
  assert.equal(diagnostics.size, 0);
  assert.deepEqual(diagnostics.entries(), []);
});

test("diagnostic log recursively redacts payload bodies and URL credentials", () => {
  const diagnostics = createDiagnosticLog({
    now: () => 0,
    consoleRef: null,
  });

  diagnostics.record("error", "topic.rejected", {
    topic: "/surgery/speech",
    reason: "validation_failed",
    revision: 41,
    bridgeUrl: "wss://operator:secret@example.test/socket?access_token=hidden#fragment",
    message: {
      revision: 41,
      text: "private spoken words",
      payload: { instrument: "private raw payload" },
      nested: {
        data: "base64-image-body",
        transcript: "private transcript",
        authorization: "Bearer credential",
        cookie: "session=credential",
        token: "credential",
        password: "credential",
        image: "raw image",
      },
    },
  });

  const [entry] = diagnostics.entries();
  assert.equal(entry.details.topic, "/surgery/speech");
  assert.equal(entry.details.reason, "validation_failed");
  assert.equal(entry.details.revision, 41);
  assert.equal(entry.details.bridgeUrl, "wss://example.test/socket");
  assert.equal(entry.details.message, "[redacted]");

  const serialized = diagnostics.exportJson();
  for (const secret of [
    "private spoken words",
    "private raw payload",
    "base64-image-body",
    "private transcript",
    "Bearer credential",
    "session=credential",
    "access_token",
    "operator",
    "secret",
  ]) {
    assert.doesNotMatch(serialized, new RegExp(secret));
  }
  const exported = JSON.parse(serialized);
  assert.equal(exported.schema, "surgimate.connection-diagnostics.v1");
  assert.deepEqual(exported.entries, diagnostics.entries());
});

test("diagnostic log remains bounded and preserves monotonic event sequence", () => {
  let now = 0;
  const diagnostics = createDiagnosticLog({
    capacity: 100,
    now: () => now++,
    consoleRef: null,
  });

  for (let index = 0; index < 105; index += 1) {
    diagnostics.record("debug", "camera.frame", { frame: index });
  }

  assert.equal(diagnostics.capacity, 100);
  assert.equal(diagnostics.size, 100);
  assert.equal(diagnostics.entries()[0].sequence, 6);
  assert.equal(diagnostics.entries().at(-1).sequence, 105);
});

test("diagnostic records remain session-only and do not use browser persistence or upload APIs", async () => {
  const source = await readFile(new URL("../ros/diagnostic-log.js", import.meta.url), "utf8");
  assert.doesNotMatch(
    source,
    /localStorage|sessionStorage|indexedDB|document\.cookie|sendBeacon|\bfetch\s*\(|XMLHttpRequest/i,
  );
  assert.match(source, /const records\s*=\s*\[\]/);
});
