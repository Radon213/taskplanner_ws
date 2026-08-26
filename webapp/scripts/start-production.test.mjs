import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const START_SCRIPT = fileURLToPath(new URL("./start-production.sh", import.meta.url));

function writeFixtureFile(root, relativePath, body) {
  const filePath = join(root, relativePath);
  mkdirSync(dirname(filePath), { recursive: true });
  writeFileSync(filePath, body);
  return filePath;
}

function writeExecutable(filePath, body) {
  mkdirSync(dirname(filePath), { recursive: true });
  writeFileSync(filePath, body);
  chmodSync(filePath, 0o755);
}

function readJsonLines(filePath) {
  const body = readFileSync(filePath, "utf8").trim();
  return body ? body.split("\n").map((line) => JSON.parse(line)) : [];
}

function runStartup(root, environment) {
  const result = spawnSync("bash", [START_SCRIPT], {
    cwd: root,
    encoding: "utf8",
    env: environment,
  });
  assert.equal(
    result.status,
    0,
    `startup failed\nstdout:\n${result.stdout}\nstderr:\n${result.stderr}`,
  );
}

test("production startup keeps argv intact and rebuilds only when source changes", (context) => {
  const root = mkdtempSync(join(tmpdir(), "taskplanner-web-startup-test-"));
  context.after(() => rmSync(root, { recursive: true, force: true }));

  const packageLock = '{"lockfileVersion":3}\n';
  for (const [relativePath, body] of [
    ["index.html", '<main id="root"></main>\n'],
    ["package.json", '{"scripts":{}}\n'],
    ["package-lock.json", packageLock],
    ["tsconfig.json", "{}\n"],
    ["vite.config.ts", "export default {};\n"],
    ["scripts/start-production.sh", readFileSync(START_SCRIPT, "utf8")],
    ["scripts/serve-production.mjs", "export {};\n"],
    ["scripts/check-dev-server-contract.mjs", "export {};\n"],
    ["scripts/check-monitor-bundle.cjs", "module.exports = {};\n"],
    ["src/main.tsx", "export const revision = 1;\n"],
    ["public/runtime-config.js", "window.RUNTIME_CONFIG = {};\n"],
    ["monitor/index.html", '<main class="app-shell"></main>\n'],
  ]) writeFixtureFile(root, relativePath, body);

  const fakeBin = join(root, "fake-bin");
  const npmLog = join(root, "npm-calls.jsonl");
  const setprivLog = join(root, "setpriv-calls.jsonl");
  mkdirSync(fakeBin);
  writeExecutable(join(root, "node_modules/.bin/vite"), "#!/usr/bin/env bash\nexit 0\n");
  writeFixtureFile(
    root,
    "node_modules/.taskplanner-package-lock.sha256",
    `${createHash("sha256").update(packageLock).digest("hex")}\n`,
  );

  writeExecutable(join(fakeBin, "npm"), `#!/usr/bin/env bash
set -euo pipefail
node -e 'require("node:fs").appendFileSync(process.env.TASKPLANNER_TEST_NPM_LOG, JSON.stringify(process.argv.slice(1)) + "\\n")' "$@"
if [[ "\${1:-}" == "run" && "\${2:-}" == "build:runtime" ]]; then
  mkdir -p dist/monitor
  printf '<main id="root"></main>\\n' > dist/index.html
  printf '<main class="app-shell"></main>\\n' > dist/monitor/index.html
fi
`);
  writeExecutable(join(fakeBin, "setpriv"), `#!/usr/bin/env bash
set -euo pipefail
node -e 'require("node:fs").appendFileSync(process.env.TASKPLANNER_TEST_SETPRIV_LOG, JSON.stringify(process.argv.slice(1)) + "\\n")' -- "$@"
while (( \$# > 0 )) && [[ "\$1" != "env" ]]; do shift; done
[[ "\${1:-}" == "env" ]]
exec "\$@"
`);

  const portArgument = "4173; printf argv-injection-must-not-run";
  const environment = {
    ...process.env,
    PATH: `${fakeBin}:${process.env.PATH}`,
    TASKPLANNER_TEST_NPM_LOG: npmLog,
    TASKPLANNER_TEST_SETPRIV_LOG: setprivLog,
    TASKPLANNER_WEBAPP_UID: String(process.getuid()),
    TASKPLANNER_WEBAPP_GID: String(process.getgid()),
    WEBAPP_BUILD_ON_START: "false",
    WEBAPP_INSTALL_ON_START: "false",
    WEBAPP_PORT: portArgument,
  };

  runStartup(root, environment);
  const firstStamp = readFileSync(join(root, "dist/.taskplanner-source.sha256"), "utf8");
  runStartup(root, environment);
  assert.equal(
    readFileSync(join(root, "dist/.taskplanner-source.sha256"), "utf8"),
    firstStamp,
  );

  writeFixtureFile(root, "index.html", '<main id="root" data-revision="2"></main>\n');
  runStartup(root, environment);
  assert.notEqual(
    readFileSync(join(root, "dist/.taskplanner-source.sha256"), "utf8"),
    firstStamp,
  );

  const npmCalls = readJsonLines(npmLog);
  assert.deepEqual(npmCalls.map((call) => call.slice(0, 2)), [
    ["run", "build:runtime"],
    ["run", "start:production"],
    ["run", "start:production"],
    ["run", "build:runtime"],
    ["run", "start:production"],
  ]);
  for (const call of npmCalls.filter((entry) => entry[1] === "start:production")) {
    assert.deepEqual(call, [
      "run",
      "start:production",
      "--",
      "--host",
      "127.0.0.1",
      "--port",
      portArgument,
      "--media",
      "/var/run/taskplanner-monitor-media",
    ]);
  }

  const setprivCalls = readJsonLines(setprivLog);
  assert.equal(setprivCalls.length, 3);
  for (const call of setprivCalls) {
    assert.deepEqual(call, [
      `--reuid=${process.getuid()}`,
      `--regid=${process.getgid()}`,
      "--clear-groups",
      "env",
      "HOME=/tmp/taskplanner-webapp-home",
      "TASKPLANNER_WEBAPP_DROPPED_PRIVILEGES=true",
      "bash",
      START_SCRIPT,
    ]);
  }
});
