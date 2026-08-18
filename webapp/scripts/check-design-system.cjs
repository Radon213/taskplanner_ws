const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const srcRoot = path.join(root, "src");
const tokenFile = "styles/design-tokens.css";
const violations = [];

function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const absolute = path.join(dir, entry.name);
    return entry.isDirectory() ? walk(absolute) : [absolute];
  });
}

for (const file of walk(srcRoot)) {
  if (!/\.(css|ts|tsx)$/.test(file)) continue;
  const relative = path.relative(srcRoot, file).replaceAll(path.sep, "/");
  const source = fs.readFileSync(file, "utf8");

  if (relative !== tokenFile) {
    const hardcodedHex = source.match(/#[0-9a-fA-F]{3,8}(?![0-9a-zA-Z_-])/g) ?? [];
    if (hardcodedHex.length) {
      violations.push(`${relative}: ${hardcodedHex.length} hardcoded hex color(s); use semantic tokens`);
    }
  }

  if (/import\s*\{[^}]*\bmotion\b[^}]*\}\s*from\s*["']framer-motion["']/.test(source) || /\bmotion\./.test(source)) {
    violations.push(`${relative}: use strict LazyMotion-compatible m components`);
  }

  if (/\.(ts|tsx)$/.test(file) && relative !== "motion-system.ts") {
    for (const match of source.matchAll(/duration\s*:\s*(\d+(?:\.\d+)?)/g)) {
      if (Number(match[1]) > 0.5) {
        violations.push(`${relative}: literal motion duration ${match[1]}s exceeds the 500ms interaction ceiling`);
      }
    }
    if (/type\s*:\s*["']spring["']/.test(source)) {
      violations.push(`${relative}: spring motion conflicts with the product Silk motion language`);
    }
  }
}

const main = fs.readFileSync(path.join(srcRoot, "main.tsx"), "utf8");
const motionSystem = fs.readFileSync(path.join(srcRoot, "motion-system.ts"), "utf8");
const aPlus = fs.readFileSync(path.join(srcRoot, "styles", "a-plus.css"), "utf8");

for (const guard of [
  [main, 'reducedMotion="user"', "MotionConfig must respect the OS reduced-motion preference"],
  [main, "<LazyMotion", "LazyMotion must wrap the application"],
  [main, "strict", "LazyMotion strict mode must prevent full motion components"],
  [motionSystem, "SILK_EASE", "the shared Silk easing token is missing"],
  [motionSystem, "MOTION_DURATION", "the shared motion duration scale is missing"],
  [aPlus, "@media (prefers-reduced-motion: reduce)", "the CSS reduced-motion fallback is missing"],
  [aPlus, "min-height: 44px", "the minimum target-size rule is missing"],
  [aPlus, ":focus-visible", "the visible keyboard-focus rule is missing"],
  [aPlus, ".operation-progress", "indeterminate operation feedback is missing"],
]) {
  if (!guard[0].includes(guard[1])) violations.push(guard[2]);
}

if (violations.length) {
  console.error("Design-system guard failed:");
  for (const violation of violations) console.error(`- ${violation}`);
  process.exit(1);
}

console.log("Design-system guard passed: semantic colors, Silk motion, reduced motion, focus, and target sizes are enforced.");
