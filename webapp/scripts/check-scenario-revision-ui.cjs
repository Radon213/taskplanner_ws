const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(path.join(root, relative), "utf8");
const scenario = read("src/ros/scenarioRevision.ts");
const bridge = read("src/hooks/useRosBridge.ts");
const app = read("src/App.tsx");
const control = read("src/components/command/ScenarioRevisionControl.tsx");

const violations = [];
const requirePattern = (source, pattern, message) => {
  if (!pattern.test(source)) violations.push(message);
};

requirePattern(
  scenario,
  /preview_only:\s*true[\s\S]{0,120}reload_if_changed:\s*false/,
  "Revision preview must remain a read-only SelectSimulationBundle request.",
);
requirePattern(
  scenario,
  /preview_only:\s*false[\s\S]{0,120}reload_if_changed:\s*admission\.reloadIfChanged/,
  "Revision apply must carry the explicit additive reload contract.",
);
requirePattern(
  scenario,
  /expected_candidate_revision:\s*expectedCandidateRevision/,
  "Revision apply must bind the request to the server-previewed candidate revision.",
);
requirePattern(
  scenario,
  /revision\.phase\s*!==\s*"previewed"/,
  "Apply admission must require a matching completed preview.",
);
requirePattern(
  scenario,
  /sameBundle[\s\S]{0,260}!fullyStopped\(state\)/,
  "Same-bundle reload must remain locked until the scenario is fully stopped.",
);
requirePattern(
  bridge,
  /scenarioRevisionPreviewRequest\(selectedBundle\)[\s\S]{0,120}requireFreshRuntimeState:\s*false/,
  "Running-state preview must not inherit the mutating runtime-state gate.",
);
requirePattern(
  bridge,
  /computeScenarioRevisionApplyAdmission\([\s\S]{0,900}currentRevision\.result\?\.candidateRevision\.trim\(\)[\s\S]{0,360}scenarioRevisionApplyRequest\([\s\S]{0,120}expectedCandidateRevision/,
  "The bridge must recheck admission immediately before an apply request.",
);
requirePattern(
  bridge,
  /const scenarioRevisionAdmission = computeScenarioRevisionApplyAdmission\(\{[\s\S]{0,240}stateFresh:\s*runtimeStateIsFresh\(\)/,
  "Apply affordance must use the bounded runtime-state age, not socket connectivity alone.",
);
requirePattern(
  app,
  /onBundleChange=\{\(nextBundle\)\s*=>\s*\{\s*ros\.setBundleSelection\(nextBundle\);\s*\}\}/,
  "Selecting a bundle must not immediately apply or reload it.",
);
requirePattern(
  control,
  /disabled=\{applyDisabled\}[\s\S]{0,180}if \(!applyDisabled\) onApply\(\)/,
  "The apply button must keep a handler-side admission guard in addition to disabled styling.",
);
requirePattern(
  control,
  /State is rechecked before apply; the server decides\./,
  "The UI must state that final apply authority remains with the server.",
);

if (/ros\.applyBundle\(nextBundle\)/.test(app)) {
  violations.push("Bundle selection still invokes applyBundle directly.");
}
if (/bundleSwitchAllowed/.test(app) || /bundleSwitchAllowed/.test(control)) {
  violations.push("Legacy selector-level switch authority must not replace revision admission.");
}
if (/active_bundle:\s*appliedBundle[\s\S]{0,160}setSimulationState/.test(bridge)) {
  violations.push("A Service response must not synthesize a hybrid authoritative simulation frame.");
}

if (violations.length) {
  console.error("Scenario revision UI check failed:");
  for (const violation of violations) console.error(`- ${violation}`);
  process.exit(1);
}

console.log("Scenario revision UI check passed.");
