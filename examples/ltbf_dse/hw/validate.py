"""validate.py -- AC11's scores (plan step 5.5): the cost and time models against held-out builds, full
pysim and RTL.

``plans/ltbf_dse/ltbf_dse.md`` AC11.  Writes ``paper_data/model_validation.csv`` (local), one row a
score:

* ``heldout``: the frozen models (``hw/prereg/models.json``) on the held-out builds -- each resource's
  and each part kind's per-job cycles' median and worst relative error (limits: 10% cycles and DSP,
  15% LUT, FF, BRAM, URAM, on the medians);
* ``pysim``: the job-level simulator (``jobsim.fitted``: the frozen models, Shared Mem's port shared)
  against a full pysim run of each of AC11's five configurations -- N_rx 64 and 256, factored and
  explicit, the dynamic policy, and N_rx 64 factored with the static one, each one long-term update
  and one slot with Shared Mem calibrated and the O-RU at ``N_rx / 64`` ms a symbol -- its end and its
  long-term update's (limit: 5%);
* ``rtl``: the simulator against AC9's XSI run (``xsi_work/ltbf``, the system flow's) (limit: 5%).

    python -m examples.ltbf_dse.hw.validate
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "paper_data" / "model_validation.csv"
#: AC11's configurations: (array side, form, policy).
CONFIGS = ((8, "factored", "dynamic"), (8, "explicit", "dynamic"), (16, "factored", "dynamic"),
           (16, "explicit", "dynamic"), (8, "factored", "static"))  # fmt: skip
LIMITS = {"cycles": 0.10, "DSP": 0.10, "LUT": 0.15, "FF": 0.15, "BRAM_18K": 0.15, "URAM": 0.15,
          "time": 0.05}  # fmt: skip


def _params(n: int, form: str, policy: str):
    from examples.ltbf_dse.hw import system as sy

    return sy.SysParams(n=n, form=form, policy=policy, sym_period=1e-3 * n * n / 64)


def simulate(S, model: dict) -> dict:
    from examples.ltbf_dse.hw import jobsim as js

    cfg, timing = js.fitted(S, model)
    out = js.JobSim(cfg, timing).run()
    return {"end": out["end"] * js.F_HOST, "t_lt": out["t_lt"] * js.F_HOST}


def pysim(n: int, form: str, policy: str) -> tuple[dict, object]:
    """A full pysim run: its end and long-term end (cycles), and its system."""
    from examples.ltbf_dse.hw import jobsim as js
    from examples.ltbf_dse.hw import system as sy

    hw, S, host = sy.build(sy.inputs(_params(n, form, policy)))
    hw.run()
    sch = S.sch
    t_lt = max(sch.t_done[g] for g in range(len(S.lt.jobs)))
    return {"end": host.env.now * js.F_HOST, "t_lt": t_lt * js.F_HOST}, S


def rtl_end(work: Path) -> float | None:
    rep = work / "report.json"
    if not rep.is_file():
        return None
    m = re.search(
        r"DONE done=1 cycles=(\d+)", json.loads(rep.read_text()).get("output", "")
    )
    return float(m.group(1)) if m else None


def main() -> list[dict]:
    from examples.ltbf_dse.hw import build as hb
    from examples.ltbf_dse.hw import models as hm
    from examples.ltbf_dse.hw import system as sy

    model = json.loads(hm.MODELS.read_text())
    rows: list[dict] = []
    sc = hm.score(model, "heldout")
    for c, v in sc["resources"].items():
        rows.append({"check": "heldout", "what": c, "n": v["n"], "median": v["median"], "worst": v["worst"],
                     "limit": LIMITS[c], "ok": v["median"] <= LIMITS[c]})  # fmt: skip
    for k, v in sc["cycles"].items():
        rows.append({"check": "heldout", "what": f"cycles_{k}", "n": v["n"], "median": v["median"],
                     "worst": v["worst"], "limit": LIMITS["cycles"], "ok": v["median"] <= LIMITS["cycles"]})  # fmt: skip
    for n, form, policy in CONFIGS:
        ref, S = pysim(n, form, policy)
        sim = simulate(S, model)
        name = f"n{n * n}_{form}_{policy}"
        for k in ("end", "t_lt"):
            err = sim[k] / ref[k] - 1
            rows.append({"check": "pysim", "what": f"{name}_{k}", "n": 1, "median": err, "worst": abs(err),
                         "limit": LIMITS["time"] if k == "end" else "", "ok": abs(err) <= LIMITS["time"]
                         if k == "end" else "", "model": sim[k], "reference": ref[k]})  # fmt: skip
    t_rtl = rtl_end(hb.ROOT / hb.WORK_DIR / "ltbf")
    if t_rtl is not None:
        sim = simulate(sy.LtbfSystem(sy.inputs(sy.AC9)), model)
        err = sim["end"] / t_rtl - 1
        rows.append({"check": "rtl", "what": "ac9_end", "n": 1, "median": err, "worst": abs(err),
                     "limit": LIMITS["time"], "ok": abs(err) <= LIMITS["time"], "model": sim["end"],
                     "reference": t_rtl})  # fmt: skip
    OUT.parent.mkdir(parents=True, exist_ok=True)
    keys = [
        "check",
        "what",
        "n",
        "median",
        "worst",
        "limit",
        "ok",
        "model",
        "reference",
    ]
    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    k: (f"{r[k]:.6g}" if isinstance(r.get(k), float) else r.get(k, ""))
                    for k in keys
                }
            )
    return rows


if __name__ == "__main__":
    for r in main():
        print(
            f"{r['check']:8} {r['what']:34} median {r['median']:+.2%} worst {r['worst']:.2%} "
            f"limit {r['limit']} ok {r['ok']}"
        )
