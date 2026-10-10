"""Runs pytest and writes test_results.md, pytest_output.txt and junit.xml to the test results folder."""

import json
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

RESULTS_DIR = config.TEST_RESULTS_DIR

TC_NAMES = {
    "TC1": "Deposit",
    "TC2": "Withdrawal",
    "TC3": "Insufficient Balance",
    "TC4": "Successful Distributed Transfer",
    "TC5a": "Concurrent Transfers (same source)",
    "TC5b": "Concurrent Transfers (opposite directions)",
    "TC6": "Concurrent Withdrawal",
    "TC6b": "Concurrent Withdrawal (§5 demo)",
    "TC6×50": "Concurrent Withdrawal ×50",
    "TC7a": "Server Failure: down before txn",
    "TC7b": "Server Failure: crash during PREPARE",
    "TC7c": "Server Failure: crash during COMMIT",
    "TC7d": "Server Failure: destination unavailable",
    "TC8": "Invalid Account",
    "Stress": "Money conservation stress",
    "Log": "Transaction log completeness",
    "2PC-timeout": "2PC: vote timeout releases hold",
    "2PC-coord-crash": "2PC: coordinator crash before decision",
    "Idempotent commit": "2PC: duplicate COMMITs",
    "Durability": "Durability across restart",
}

LOG_FIELDS = ["txn_id", "type", "source", "destination", "amount", "timestamp",
              "status", "failure_reason", "decision"]


def run_pytest() -> tuple[int, str]:
    """Run pytest, echoing output live (tee) and returning (exit code, output)."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", str(config.TEST_CASES_DIR), "-v", "-p", "no:cacheprovider",
         "--junitxml=" + str(RESULTS_DIR / "junit.xml"), "--tb=short"],
        cwd=str(config.PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines = []
    for line in proc.stdout:
        print(line, end="")
        lines.append(line)
    return proc.wait(), "".join(lines)


def junit_counts() -> dict:
    """Totals from junit.xml."""
    path = RESULTS_DIR / "junit.xml"
    if not path.exists():
        return {}
    suite = ET.parse(path).getroot()
    if suite.tag == "testsuites":
        suite = suite[0]
    total = int(suite.get("tests", 0))
    failed = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
    skipped = int(suite.get("skipped", 0))
    return {"total": total, "failed": failed, "skipped": skipped,
            "passed": total - failed - skipped, "time": suite.get("time")}


def md(value) -> str:
    """Escape a value for a markdown table cell."""
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    config.ensure_dirs()
    obs_path = config.DATA_DIR / "test_observations.json"  # written by tests/conftest.py
    obs_path.unlink(missing_ok=True)  # never report stale observations

    print("Running pytest...")
    returncode, pytest_output = run_pytest()
    (RESULTS_DIR / "pytest_output.txt").write_text(pytest_output)

    observations = json.loads(obs_path.read_text()) if obs_path.exists() else []
    order = list(TC_NAMES)
    observations.sort(key=lambda o: order.index(o["tc_id"]) if o["tc_id"] in order else len(order))
    counts = junit_counts()

    report_path = RESULTS_DIR / "test_results.md"
    with open(report_path, "w") as f:
        f.write("# Test Results Report\n\n")
        f.write(f"- **Run date:** {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"- **Python:** {platform.python_version()} ({platform.python_implementation()})\n")
        f.write(f"- **Platform:** {platform.platform()}\n")
        f.write("- **Cluster:** 3 bank servers + gateway as separate processes on ports 9000–9003, "
                "fresh state before every test\n\n")

        f.write("## Summary\n\n")
        if counts:
            f.write(f"- **pytest:** {counts['passed']} passed, {counts['failed']} failed, "
                    f"{counts['skipped']} skipped (total {counts['total']}, {counts['time']} s)\n")
        f.write(f"- **Recorded test cases:** {sum(o['passed'] for o in observations)} / "
                f"{len(observations)} passed\n")
        f.write(f"- **pytest exit code:** {returncode}\n\n")

        if observations:
            f.write("## Test Case Results\n\n")
            f.write("| # | Test Case | Input/Scenario | Expected | Actual | Status |\n")
            f.write("|---|-----------|----------------|----------|--------|--------|\n")
            for o in observations:
                status = "✅" if o["passed"] else "❌"
                f.write(f"| {md(o['tc_id'])} | {md(TC_NAMES.get(o['tc_id'], o['tc_id']))} | "
                        f"{md(o['input'])} | {md(o['expected'])} | {md(o['actual'])} | {status} |\n")
            f.write("\n")

            f.write("## Transaction Log Excerpts\n\n")
            f.write("Global transaction log records captured during each test "
                    "(the same records are mirrored to `logs/transactions.jsonl`).\n\n")
            for o in observations:
                if not o.get("log"):
                    continue
                f.write(f"### {o['tc_id']} — {TC_NAMES.get(o['tc_id'], '')}\n\n")
                f.write("| " + " | ".join(LOG_FIELDS) + " |\n")
                f.write("|" + "---|" * len(LOG_FIELDS) + "\n")
                for rec in o["log"]:
                    f.write("| " + " | ".join(md(rec.get(k)) for k in LOG_FIELDS) + " |\n")
                f.write("\n")

        f.write("## Full pytest Output\n\n")
        f.write("Also in pytest_output.txt and junit.xml.\n\n")
        f.write("```\n")
        f.write(pytest_output)
        f.write("\n```\n")

    print(f"\nReport saved to {report_path}")
    sys.exit(returncode)


if __name__ == "__main__":
    main()
