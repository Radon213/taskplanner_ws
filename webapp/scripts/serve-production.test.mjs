import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { fileURLToPath } from "node:url";

import {
  DEFAULT_DIST_ROOT,
  cacheControlFor,
  createProductionServer,
  safeResolve,
} from "./serve-production.mjs";

test("default static root is the webapp production dist directory", () => {
  assert.equal(DEFAULT_DIST_ROOT, fileURLToPath(new URL("../dist", import.meta.url)));
});

test("safeResolve rejects traversal outside the static root", () => {
  const root = "/srv/taskplanner/dist";
  assert.equal(safeResolve(root, "/assets/app.js"), "/srv/taskplanner/dist/assets/app.js");
  assert.equal(safeResolve(root, "/../secret"), null);
  assert.equal(safeResolve(root, "/%2e%2e/secret"), null);
});

test("cache policy distinguishes HTML, hashed assets, and live media", () => {
  assert.equal(cacheControlFor("/index.html"), "no-cache");
  assert.equal(cacheControlFor("/assets/app-AbCdEf1234.js"), "public, max-age=31536000, immutable");
  assert.equal(cacheControlFor("/media/flir.m3u8", { media: true }), "no-store");
});

test("production server redirects webOS, keeps HLS whole, and supports static byte ranges", async (context) => {
  const root = mkdtempSync(join(tmpdir(), "taskplanner-web-test-"));
  const dist = join(root, "dist");
  const media = join(root, "media");
  mkdirSync(dist);
  mkdirSync(media);
  writeFileSync(join(dist, "index.html"), "<main id=\"root\"></main>");
  writeFileSync(join(dist, "artifact.bin"), "abcdefghij");
  writeFileSync(join(media, "flir-00001.ts"), "0123456789");
  const server = createProductionServer({ distRoot: dist, mediaRoot: media, runtimeProxy: null });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  context.after(() => new Promise((resolve) => server.close(resolve)));
  const { port } = server.address();

  const redirect = await fetch(`http://127.0.0.1:${port}/`, {
    redirect: "manual",
    headers: { "user-agent": "Mozilla/5.0 (Web0S; Linux/SmartTV)" },
  });
  assert.equal(redirect.status, 302);
  assert.equal(redirect.headers.get("location"), "/monitor/index.html?profile=tv");

  const hlsRange = await fetch(`http://127.0.0.1:${port}/media/flir-00001.ts`, {
    headers: { range: "bytes=2-5" },
  });
  assert.equal(hlsRange.status, 200);
  assert.equal(hlsRange.headers.get("accept-ranges"), "none");
  assert.equal(await hlsRange.text(), "0123456789");

  const range = await fetch(`http://127.0.0.1:${port}/artifact.bin`, {
    headers: { range: "bytes=2-5" },
  });
  assert.equal(range.status, 206);
  assert.equal(await range.text(), "cdef");
});

test("static health stays ready when runtime control is unavailable", async (context) => {
  const root = mkdtempSync(join(tmpdir(), "taskplanner-static-health-test-"));
  const dist = join(root, "dist");
  mkdirSync(dist);
  writeFileSync(join(dist, "index.html"), '<main id="root"></main>');
  const server = createProductionServer({ distRoot: dist, runtimeProxy: null });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  context.after(() => new Promise((resolve) => server.close(resolve)));
  const { port } = server.address();

  const health = await fetch(`http://127.0.0.1:${port}/healthz`);
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), {
    service: "taskplanner-webapp",
    build: createHash("sha256").update(dist).digest("hex").slice(0, 12),
  });

  const index = await fetch(`http://127.0.0.1:${port}/`);
  assert.equal(index.status, 200);
  assert.equal(await index.text(), '<main id="root"></main>');

  const runtime = await fetch(`http://127.0.0.1:${port}/api/runtime/status`);
  assert.equal(runtime.status, 503);
  assert.deepEqual(await runtime.json(), { error: "runtime_control_unavailable" });
});

test("runtime transition proxy preserves Content-Length and request body", async (context) => {
  const root = mkdtempSync(join(tmpdir(), "taskplanner-runtime-proxy-test-"));
  const dist = join(root, "dist");
  mkdirSync(dist);
  writeFileSync(join(dist, "index.html"), "<main id=\"root\"></main>");

  let received = null;
  const upstream = createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      received = {
        body: Buffer.concat(chunks).toString("utf8"),
        contentLength: request.headers["content-length"],
        token: request.headers["x-taskplanner-runtime-control-token"],
        requestId: request.headers["x-taskplanner-request-id"],
        url: request.url,
      };
      response.writeHead(202, { "Content-Type": "application/json" });
      response.end('{"phase":"starting"}');
    });
  });
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  context.after(() => new Promise((resolve) => upstream.close(resolve)));
  const upstreamAddress = upstream.address();

  const server = createProductionServer({
    distRoot: dist,
    runtimeProxy: {
      target: new URL(`http://127.0.0.1:${upstreamAddress.port}`),
      token: "test-runtime-token",
    },
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  context.after(() => new Promise((resolve) => server.close(resolve)));
  const { port } = server.address();
  const body = JSON.stringify({ mode: "live" });

  const response = await fetch(`http://127.0.0.1:${port}/api/runtime/transition`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Taskplanner-Request-Id": "11111111-1111-4111-8111-111111111111",
    },
    body,
  });

  assert.equal(response.status, 202);
  assert.deepEqual(received, {
    body,
    contentLength: String(Buffer.byteLength(body)),
    token: "test-runtime-token",
    requestId: "11111111-1111-4111-8111-111111111111",
    url: "/v1/runtime/transition",
  });
});
