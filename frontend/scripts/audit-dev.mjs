// Runs `npm audit` over everything and applies the policy in audit-policy.mjs.
import { spawnSync } from "node:child_process";
import { ALLOWED, evaluate } from "./audit-policy.mjs";

const run = spawnSync("npm", ["audit", "--json"], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
let report;
try {
  report = JSON.parse(run.stdout);
} catch {
  console.error("Could not read the output of `npm audit` (is the registry reachable?).");
  console.error(run.stderr || run.stdout);
  process.exit(2);
}
if (report.error) {
  console.error(`npm audit failed: ${report.error.summary ?? JSON.stringify(report.error)}`);
  process.exit(2);
}

const today = new Date().toISOString().slice(0, 10);
const { ok, problems, accepted } = evaluate(report, ALLOWED, today);
for (const line of accepted) console.log(`accepted: ${line}`);
for (const line of problems) console.error(`FAIL: ${line}`);
console.log(ok ? "Dependency audit (all packages): OK" : "Dependency audit (all packages): FAILED");
process.exit(ok ? 0 : 1);
