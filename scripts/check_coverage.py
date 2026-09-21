"""Enforce final aggregate and security-critical coverage floors."""

import json
from pathlib import Path

STATEMENT_BASELINE = 90.0
BRANCH_BASELINE = 80.0
SECURITY_CRITICAL = (
    "src/environment_harness/operations.py",
    "src/environment_harness/runtime.py",
    "src/environment_harness/server.py",
    "src/environment_harness/store.py",
)

coverage = json.loads(Path("coverage.json").read_text())
totals = coverage["totals"]
statement_coverage = 100 * totals["covered_lines"] / totals["num_statements"]
branch_coverage = 100 * totals["covered_branches"] / totals["num_branches"]

if statement_coverage < STATEMENT_BASELINE:
    raise SystemExit(f"statement coverage regressed: {statement_coverage:.2f}% < {STATEMENT_BASELINE:.2f}%")
if branch_coverage < BRANCH_BASELINE:
    raise SystemExit(f"branch coverage regressed: {branch_coverage:.2f}% < {BRANCH_BASELINE:.2f}%")
for filename in SECURITY_CRITICAL:
    summary = coverage["files"][filename]["summary"]
    statements = summary["percent_statements_covered"]
    branches = summary["percent_branches_covered"]
    if statements < STATEMENT_BASELINE or branches < BRANCH_BASELINE:
        raise SystemExit(
            f"security-critical coverage failed for {filename}: "
            f"{statements:.2f}% statements, {branches:.2f}% branches"
        )
print(f"coverage ratchet passed: {statement_coverage:.2f}% statements, {branch_coverage:.2f}% branches")
