"""Run a small CPU demonstration and export an offline evidence-review page.

No model calls, vendor tools, replayed hardware, or optimization claims. Each
output directory is a new study; source-bound roots are never overwritten.
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .service import ExecutionService

OBJECTIVE = (
    "Explore hardware designs through the same parameter discovery, Python simulation, "
    "synthesis and RTL interface. Keep workload/checks fixed and search policy outside "
    "the tools. This demo verifies CPU execution and evidence handling, not live hardware."
)


def run_demo(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    cases = (
        ("fir", {"nsamp": 1024, "nfft": 2048}, [{"ntap": 8}, {"ntap": 32}]),
        ("mem_copy", {"bytes_per_job": 512, "num_cmds": 4},
         [{"mem_dwidth": 32}, {"mem_dwidth": 64}]),
    )
    experiments, verdicts = [], []
    for name, conditions, candidates in cases:
        service = ExecutionService(output / name, name, conditions)
        service.invoke("get_param_list", {})
        simulations = [service.invoke("pysim", {"params": p})
                       for p in [*candidates, candidates[-1]]]
        invalid = service.invoke("pysim", {"params": {"not_a_parameter": 1}})
        synthesis = service.invoke("synth", {"params": candidates[-1]})
        rtl = service.invoke("rtlsim", {"synthesis_id": synthesis["run_id"]})
        ok = (all(r["status"] == "ok" for r in simulations)
              and simulations[1]["request_id"] == simulations[2]["request_id"]
              and simulations[1]["run_id"] != simulations[2]["run_id"]
              and invalid["status"] == "invalid"
              and synthesis["status"] == "unavailable" and rtl["status"] == "invalid")
        verdicts.append(ok)
        experiments.append({"design_id": name, "experiment_id": service.experiment_id,
                            "description": service.describe(), "runs": service.results()})
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
        capture_output=True, text=True, check=False,
    )
    # Source hashes in each descriptor identify the executed bytes, including
    # uncommitted edits. A Git base alone is not a claim of clean-commit execution.
    bundle = {
        "schema_version": "waveflow-study-v1", "objective": OBJECTIVE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": (revision.stdout.strip() if revision.returncode == 0 else "unversioned")
                           + " (base; executed source hashes in discovery)",
        "environment": {"python": platform.python_version(), "system": platform.system(),
                        "machine": platform.machine(), "cpu": platform.processor(),
                        "method": "Sequential calls; three fresh Python attempts per design; "
                                  "first-use imports included; no speedup inference."},
        "experiments": experiments,
        "review_criteria": [
            {"id": "cross_design", "label": "Shared CPU execution and negative controls",
             "status": "passed" if all(verdicts) else "failed",
             "evidence": "FIR and MemCopy each execute two parameter points plus a repeated "
                         "attempt. Invalid parameters are rejected; unavailable synthesis "
                         "cannot be reused as a successful RTL artifact. See all attempt records."},
            {"id": "timing", "label": "Wall time distinguished from design timing",
             "status": "observed",
             "evidence": "Fresh wall_seconds on every attempt. MemCopy end_cycles is model "
                         "simulation end, not last completion. FIR throughput is analytic. "
                         "These runs do not estimate a hardware speedup."},
            {"id": "hardware", "label": "Synthesis-to-RTL continuity",
             "status": "not_checked", "evidence": "Live adapters are not integrated. "
             "No generated RTL, hardware resource observation or RTL correctness proof."},
            {"id": "search", "label": "DSE or agent advantage",
             "status": "not_checked", "evidence": "Fixed demonstration points, not an "
             "optimizer benchmark. No model calls or equal-budget search comparison."},
        ],
    }
    encoded = json.dumps(bundle, indent=2, allow_nan=False)
    (output / "study.json").write_text(encoded + "\n", encoding="utf-8")
    template = Path(__file__).with_name("visualizer.html").read_text(encoding="utf-8")
    # Script data must not be able to terminate its HTML element.
    safe = encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html, count = re.subn(
        r'(<script type="application/json" id="study-data">).*?(</script>)',
        lambda m: m[1] + safe + m[2], template, count=1, flags=re.DOTALL,
    )
    if count != 1:
        raise ValueError("visualizer template is missing its study-data slot")
    (output / "review.html").write_text(html, encoding="utf-8")
    return {"passed": all(verdicts), "output": str(output),
            "study": str(output / "study.json"), "review": str(output / "review.html"),
            "attempts": sum(len(e["runs"]) for e in experiments)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="New directory; refuses overwrite")
    args = parser.parse_args()
    try:
        result = run_demo(args.output)
    except (OSError, ValueError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}))
        return 1
    print(json.dumps(result))
    return int(not result["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
