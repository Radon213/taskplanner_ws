const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");

const root = path.resolve(__dirname, "..");
const dist = path.join(root, "dist");
const manifestPath = path.join(dist, ".vite", "manifest.json");
const monitorEntryKey = "monitor/index.html";
const violations = [];

if (!fs.existsSync(manifestPath)) {
  console.error("Monitor bundle check requires a completed Vite build.");
  process.exit(1);
}

const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
const monitorEntry = manifest[monitorEntryKey];

if (!monitorEntry?.isEntry) {
  violations.push(`Monitor HTML entry is missing from the manifest (${monitorEntryKey})`);
}

for (const requiredFile of [
  "monitor/index.html",
  "monitor/runtime-config.js",
  "monitor/dummy-data.json",
  "monitor/fixtures/UI_PUBLIC_CONTRACT_FIXTURE.json",
]) {
  if (!fs.existsSync(path.join(dist, requiredFile))) {
    violations.push(`Required monitor artifact is missing (${requiredFile})`);
  }
}

function collectEntryFiles(entryKey, collected = new Set(), includeDynamicImports = true) {
  if (!entryKey || collected.has(entryKey)) return collected;
  const record = manifest[entryKey];
  if (!record) {
    violations.push(`Monitor manifest dependency is missing (${entryKey})`);
    return collected;
  }
  collected.add(entryKey);
  const imports = includeDynamicImports
    ? [...(record.imports ?? []), ...(record.dynamicImports ?? [])]
    : record.imports ?? [];
  for (const imported of imports) {
    collectEntryFiles(imported, collected, includeDynamicImports);
  }
  return collected;
}

function findStaticImportCycle(entryKey) {
  const visited = new Set();
  const active = new Set();
  const path = [];

  function visit(key) {
    if (active.has(key)) {
      const cycleStart = path.indexOf(key);
      return [...path.slice(cycleStart), key];
    }
    if (visited.has(key) || !manifest[key]) return null;
    visited.add(key);
    active.add(key);
    path.push(key);
    const record = manifest[key];
    for (const imported of record.imports ?? []) {
      const cycle = visit(imported);
      if (cycle) return cycle;
    }
    path.pop();
    active.delete(key);
    return null;
  }

  return visit(entryKey);
}

function sizeFor(relativePath) {
  const absolutePath = path.join(dist, relativePath);
  if (!fs.existsSync(absolutePath)) {
    violations.push(`Monitor manifest artifact is missing (${relativePath})`);
    return { raw: 0, gzip: 0 };
  }
  const bytes = fs.readFileSync(absolutePath);
  return { raw: bytes.length, gzip: zlib.gzipSync(bytes, { level: 9 }).length };
}

const manifestKeys = monitorEntry ? collectEntryFiles(monitorEntryKey) : new Set();
const monitorImportCycle = monitorEntry ? findStaticImportCycle(monitorEntryKey) : null;
if (monitorImportCycle) {
  violations.push(`Monitor manifest import cycle: ${monitorImportCycle.join(" -> ")}`);
}

const graphFiles = new Set();
for (const key of manifestKeys) {
  const record = manifest[key];
  graphFiles.add(record.file);
  for (const file of [...(record.css ?? []), ...(record.assets ?? [])]) graphFiles.add(file);
}

const totals = {
  all: { raw: 0, gzip: 0 },
  js: { raw: 0, gzip: 0 },
  css: { raw: 0, gzip: 0 },
};

for (const file of graphFiles) {
  const size = sizeFor(file);
  totals.all.raw += size.raw;
  totals.all.gzip += size.gzip;
  const bucket = file.endsWith(".js") ? totals.js : file.endsWith(".css") ? totals.css : null;
  if (bucket) {
    bucket.raw += size.raw;
    bucket.gzip += size.gzip;
  }
}

function enforce(label, actual, maximum) {
  if (actual > maximum) violations.push(`${label}: ${actual} bytes exceeds ${maximum}`);
}

enforce("Monitor JavaScript raw", totals.js.raw, 450_000);
enforce("Monitor JavaScript gzip", totals.js.gzip, 150_000);
enforce("Monitor CSS raw", totals.css.raw, 50_000);
enforce("Monitor CSS gzip", totals.css.gzip, 15_000);
enforce("Monitor entry graph raw", totals.all.raw, 8_000_000);

const missionManifestKeys = manifest["index.html"]
  ? collectEntryFiles("index.html", new Set(), false)
  : new Set();
for (const key of missionManifestKeys) {
  const record = manifest[key];
  if (
    key === monitorEntryKey
    || record?.src?.startsWith("monitor/")
    || record?.name === "monitor"
    || /^assets\/monitor(?:-|\/)/.test(record?.file ?? "")
  ) {
    violations.push(`Mission entry eagerly imports a monitor-only artifact (${key})`);
  }
}

const monitorRosClientPath = path.join(root, "monitor", "ros", "ros-bridge-client.js");
if (!fs.existsSync(monitorRosClientPath)) {
  violations.push("Monitor ROS bridge client source is missing");
} else {
  const monitorRosClient = fs.readFileSync(monitorRosClientPath, "utf8");
  if (!/from\s+["']roslib-monitor["']/.test(monitorRosClient)) {
    violations.push("Monitor ROS bridge client must import the isolated roslib-monitor alias");
  }
}

console.log("Monitor bundle budget (raw / gzip bytes):");
console.log(`- JavaScript: ${totals.js.raw} / ${totals.js.gzip}`);
console.log(`- CSS: ${totals.css.raw} / ${totals.css.gzip}`);
console.log(`- Entry graph: ${totals.all.raw} / ${totals.all.gzip} (${graphFiles.size} files)`);

if (violations.length) {
  console.error("Monitor bundle check failed:");
  for (const violation of violations) console.error(`- ${violation}`);
  process.exit(1);
}

console.log("Monitor bundle check passed.");
