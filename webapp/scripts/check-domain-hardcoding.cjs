const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..", "src");

const allowedFiles = new Set([
  "layouts.ts",
  "visualLayouts.ts",
  "types.ts",
  "utils/uiCopy.ts",
  "hooks/useDigitalTwinViewModel.ts",
]);

const forbidden = [
  "metzenbaum",
  "cautery",
  "right_angle",
  "clip_applier",
  "suction_irrigator",
  "access_exposure",
  "hilar_dissection",
  "pedicle_control",
  "VLMProposalAccepted",
  "VLMProposalRejected",
  "ToolHandoverCompleted",
  "ToolCleaningCompleted",
  "RobotTaskStarted",
  "RobotTaskCompleted",
];

function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const absolute = path.join(dir, entry.name);
    if (entry.isDirectory()) return walk(absolute);
    return absolute;
  });
}

const violations = [];
for (const file of walk(root)) {
  if (!/\.(ts|tsx)$/.test(file)) continue;
  const relative = path.relative(root, file).replaceAll(path.sep, "/");
  if (allowedFiles.has(relative)) continue;
  const source = fs.readFileSync(file, "utf8");
  for (const token of forbidden) {
    if (source.includes(token)) {
      violations.push(`${relative}: forbidden domain token '${token}'`);
    }
  }
}

if (violations.length) {
  console.error("Domain hardcoding guard failed:");
  for (const violation of violations) console.error(`- ${violation}`);
  process.exit(1);
}

console.log("Domain hardcoding guard passed.");
