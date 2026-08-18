const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");
const crypto = require("node:crypto");

const root = path.resolve(__dirname, "..");
const dist = path.join(root, "dist");
const manifestPath = path.join(dist, ".vite", "manifest.json");

if (!fs.existsSync(manifestPath)) {
  console.error("Bundle budget check requires a completed Vite build.");
  process.exit(1);
}

const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
const violations = [];

function sizeFor(file) {
  const bytes = fs.readFileSync(path.join(dist, file));
  return { raw: bytes.length, gzip: zlib.gzipSync(bytes, { level: 9 }).length };
}

function check(label, key, rawBudget, gzipBudget) {
  const record = manifest[key];
  if (!record) {
    violations.push(`${label}: manifest entry is missing (${key})`);
    return null;
  }
  const size = sizeFor(record.file);
  if (size.raw > rawBudget) {
    violations.push(`${label}: ${size.raw} raw bytes exceeds ${rawBudget}`);
  }
  if (size.gzip > gzipBudget) {
    violations.push(`${label}: ${size.gzip} gzip bytes exceeds ${gzipBudget}`);
  }
  return { label, file: record.file, ...size };
}

function findKeyByName(name) {
  return Object.entries(manifest).find(([, value]) => value.name === name)?.[0];
}

const rows = [];
rows.push(check("Mission entry", "index.html", 220_000, 66_000));
rows.push(check("Debug workspace", "src/components/debug/DebugWorkspace.tsx", 95_000, 26_000));
rows.push(check("Multicam workspace", "src/components/multicam/MulticamOpsWorkspace.tsx", 55_000, 19_000));
rows.push(check("Deferred motion features", "src/motion-features.ts", 95_000, 31_000));

const modelAsset = "models/humanoid-tray-tag1.glb";
const modelPath = path.join(dist, modelAsset);
const modelMetadataPath = path.join(dist, "models/humanoid-tray-tag1.anchor.json");
if (!fs.existsSync(modelPath)) {
  violations.push(`3D operating-room model: built asset is missing (${modelAsset})`);
} else {
  const raw = fs.statSync(modelPath).size;
  rows.push({ label: "3D operating-room model", file: modelAsset, raw, gzip: null });
  if (raw > 14_000_000) {
    violations.push(`3D operating-room model: ${raw} raw bytes exceeds 14000000`);
  }
  if (!fs.existsSync(modelMetadataPath)) {
    violations.push("3D operating-room model: optimization metadata is missing");
  } else {
    const metadata = JSON.parse(fs.readFileSync(modelMetadataPath, "utf8"));
    const digest = crypto.createHash("sha256").update(fs.readFileSync(modelPath)).digest("hex");
    if (metadata.webOptimization?.outputBytes !== raw) {
      violations.push("3D operating-room model: metadata byte count does not match the built asset");
    }
    if (metadata.webOptimization?.outputSha256 !== digest) {
      violations.push("3D operating-room model: metadata SHA-256 does not match the built asset");
    }
  }
}

for (const [label, name, rawBudget, gzipBudget] of [
  ["React vendor", "react-vendor", 145_000, 47_000],
  ["ROS vendor", "ros-vendor", 70_000, 20_000],
  ["Icon vendor", "icons-vendor", 30_000, 11_000],
  ["Deferred Three.js vendor", "three-vendor", 620_000, 160_000],
]) {
  const key = findKeyByName(name);
  if (!key) {
    violations.push(`${label}: named chunk is missing (${name})`);
  } else {
    rows.push(check(label, key, rawBudget, gzipBudget));
  }
}

const entry = manifest["index.html"];
if (entry?.css?.length !== 1) {
  violations.push(`Mission entry: expected one consolidated stylesheet, found ${entry?.css?.length ?? 0}`);
} else {
  const cssSize = sizeFor(entry.css[0]);
  rows.push({ label: "Styles", file: entry.css[0], ...cssSize });
  if (cssSize.raw > 200_000 || cssSize.gzip > 35_000) {
    violations.push(`Styles: ${cssSize.raw} raw / ${cssSize.gzip} gzip bytes exceeds 200000 / 35000`);
  }
}

const eagerKeys = new Set();
function addEager(key) {
  if (!key || eagerKeys.has(key)) return;
  eagerKeys.add(key);
  for (const imported of manifest[key]?.imports ?? []) addEager(imported);
}
addEager("index.html");

let eagerGzip = 0;
for (const key of eagerKeys) eagerGzip += sizeFor(manifest[key].file).gzip;
if (eagerGzip > 155_000) {
  violations.push(`Initial eager JavaScript: ${eagerGzip} gzip bytes exceeds 155000`);
}

const threeKey = findKeyByName("three-vendor");
if (threeKey && eagerKeys.has(threeKey)) {
  violations.push("Three.js must remain outside the initial Mission bundle.");
}
if (!entry?.dynamicImports?.includes("src/motion-features.ts")) {
  violations.push("Motion feature definitions must remain dynamically loaded.");
}

console.log("Bundle budgets (raw / gzip bytes):");
for (const row of rows.filter(Boolean)) {
  const sizes = row.gzip === null ? `${row.raw} raw` : `${row.raw} / ${row.gzip}`;
  console.log(`- ${row.label}: ${sizes} (${row.file})`);
}
console.log(`- Initial eager JavaScript: ${eagerGzip} gzip bytes`);

if (violations.length) {
  console.error("Bundle budget check failed:");
  for (const violation of violations) console.error(`- ${violation}`);
  process.exit(1);
}

console.log("Bundle budget check passed.");
