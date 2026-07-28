import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const contractsUrl = new URL("../../docs/contracts/", import.meta.url);
const contractsDirectory = fileURLToPath(contractsUrl);
const operationPattern =
  /^\s{4}(get|post|put|patch|delete|head|options|trace):\s*$/gim;
const violations = [];
let contractCount = 0;
let operationCount = 0;

for (const entry of readdirSync(contractsDirectory, { withFileTypes: true })) {
  if (
    !entry.isFile() ||
    !entry.name.endsWith(".openapi.yaml") ||
    entry.name.includes("data-sync")
  ) {
    continue;
  }

  contractCount += 1;
  const source = readFileSync(new URL(entry.name, contractsUrl), "utf8");

  if (!source.includes("x-http-method-policy: post-only")) {
    violations.push(`${entry.name}: missing x-http-method-policy: post-only`);
  }

  for (const match of source.matchAll(operationPattern)) {
    operationCount += 1;
    if (match[1]?.toLowerCase() !== "post") {
      violations.push(
        `${entry.name}: forbidden ${match[1]?.toUpperCase()} operation`,
      );
    }
  }
}

if (contractCount === 0 || operationCount === 0) {
  throw new Error("No public service-api OpenAPI operations were checked.");
}

if (violations.length > 0) {
  throw new Error(
    `service-api POST-only contract policy failed:\n${violations.join("\n")}`,
  );
}

console.log(
  `Checked ${operationCount} POST operations across ${contractCount} contracts.`,
);
