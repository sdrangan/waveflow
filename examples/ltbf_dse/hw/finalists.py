"""finalists.py -- the DSE's finalists through Vivado implementation (plan step 6.3, AC14).

``plans/ltbf_dse/ltbf_dse.md`` AC14.  Three finalists, all at 8x8 (N_rx 64, where Shared Mem fits the
1 MiB on-chip gate), each the whole system -- the systolic wrapper, the vector unit's, the Ingest DMA,
the Resp hub, the crossbar and Shared Mem -- built at its parameters in its own root
(``hw/build/fin_<name>/``) and taken through the system flow's synthesis with placement and routing
(``SystemSynthStep(impl=True)``, Vivado 2024.1, the xczu48dr at 4 ns):

* ``F1`` -- the DSE's cheapest pick at 8x8 (``dse_decisions.csv``: a 2 x 4 array, ``W`` 12, 2 lanes;
  built with the explicit form's buffers, the hardware of the factored pick too);
* ``F2`` -- AC9's design, verified at RTL (4 x 8, ``W`` 16, 4 lanes, factored);
* ``F3`` -- the frontier's largest systolic array (8 x 8, ``W`` 16, 4 lanes, factored).

Writes ``paper_data/finalists_impl.csv``: WNS and whether 4 ns is met, the worst path, and
post-implementation resources against the frozen models' prediction (``dse.cost``: the parts, the
hub, Shared Mem).  ``--collect`` rebuilds the table from each finalist's record, its model recomputed.
The form is a runtime choice: F1's hardware is the factored pick's too (the same parts and span).

    python -m examples.ltbf_dse.hw.finalists [F1 F2 F3] [--collect]
"""

from __future__ import annotations

import csv
import dataclasses
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "paper_data" / "finalists_impl.csv"
FINALISTS = {
    "F1": {"R": 2, "C": 4, "W": 12, "L": 2, "form": "explicit"},
    "F2": {"R": 4, "C": 8, "W": 16, "L": 4, "form": "factored"},
    "F3": {"R": 8, "C": 8, "W": 16, "L": 4, "form": "factored"},
}
COUNTERS = ("LUT", "FF", "DSP", "BRAM_18K", "URAM")


def implement(name: str) -> dict:
    """Finalist *name* through the system flow with implementation; its record and the model's."""
    from examples.ltbf_dse.hw import build as hb
    from examples.ltbf_dse.hw import dse
    from examples.ltbf_dse.hw import system as sy
    from waveflow.build.build import BuildDag
    from waveflow.build.system_dag import SystemSynthStep, add_system_steps
    from waveflow.vecunit import build as vb
    from waveflow.vecunit.build import build_config

    k = FINALISTS[name]
    p = sy.SysParams(n=8, **k)
    root = HERE / "build" / f"fin_{name}"
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(hb.ROOT / "src", root / "src", dirs_exist_ok=True)
    dag = BuildDag()
    dag.add(hb.LtbfCodegenStep(name="codegen", sysp=p))
    hw, _S, _host = sy.build(sy.inputs(p), rtl=True)
    add_system_steps(
        dag, hw, work_dir="xsi_work", top="ltbf_top", prefix="f_", workspace="fin"
    )
    synth = next(s for s in dag.steps() if isinstance(s, SystemSynthStep))
    synth.impl = True  # place and route too: the record carries closure
    # The system's synthesis takes its part and clock from the build's platform (none: the
    # framework's default, a Zynq-7020 at 10 ns), so a platform of this root's own, never the
    # packaged one's corpus.
    cfg = dataclasses.replace(build_config(root), platform="ltbf_finalist", part=vb.PART,
                              clk_freq=1e9 / vb.PERIOD_NS, platforms_root=root / "calib" / "platforms")  # fmt: skip
    cfg.params["synth"] = "build"
    t0 = time.time()
    res = dag.run(cfg, through="f_system_synth")
    rec: dict = {"name": name, **k, "minutes": round((time.time() - t0) / 60, 1),
                 "errors": {s: (r.message or "")[-800:] for s, r in res.items() if not r.success}}  # fmt: skip
    js = root / "xsi_work" / "fin" / "system_synth.json"
    if js.is_file():
        d = json.loads(js.read_text())
        rec.update({"wns_ns": d.get("wns_ns"), "met": d.get("met"), "tool": d.get("tool"), "total": d.get("total", {}),
                    "instances": d.get("instances", {})})  # fmt: skip
    rec["model"] = dse.cost(8, k["R"], k["C"], k["W"], k["L"], k["form"])["res"]
    (root / "finalist.json").write_text(json.dumps(rec, indent=1))
    return rec


def _key(total: dict, c: str) -> float:
    """The system synthesis record's counters (``waveflow.build.system_synth``: ``bram`` in BRAM18,
    a RAMB36 counting two)."""
    names = {"LUT": "lut", "FF": "ff", "DSP": "dsp", "BRAM_18K": "bram", "URAM": "uram"}
    return float(total[names[c]]) if names[c] in total else float("nan")


def _path(root: Path) -> str:
    """The worst setup path after routing, as the synthesis script printed it."""
    for log in sorted((root / "xsi_work" / "system_synth").glob("*/vivado.log")):
        for line in log.read_text(errors="replace").splitlines():
            if line.startswith("WF_PATH_impl "):
                return line.split(" ", 1)[1]
    return ""


def collect(name: str) -> dict:
    """Finalist *name*'s record (``finalist.json``), its model recomputed by today's ``dse.cost``."""
    from examples.ltbf_dse.hw import dse

    root = HERE / "build" / f"fin_{name}"
    rec = json.loads((root / "finalist.json").read_text())
    k = FINALISTS[name]
    rec["model"] = dse.cost(8, k["R"], k["C"], k["W"], k["L"], k["form"])["res"]
    rec["path"] = _path(root)
    return rec


def main(names, run: bool = True) -> Path:
    rows = []
    for name in names:
        rec = implement(name) if run else collect(name)
        mem = (rec.get("instances") or {}).get("u_mem_mem", {})
        row = {"finalist": name, **{k: rec[k] for k in ("R", "C", "W", "L", "form")}, "wns_ns": rec.get("wns_ns"),
               "met_4ns": rec.get("met"), "minutes": rec["minutes"], "errors": "; ".join(rec["errors"]),
               "shared_mem_bram18": mem.get("bram", ""), "worst_path": rec.get("path", "")}  # fmt: skip
        for c in COUNTERS:
            impl = _key(rec.get("total", {}), c)
            model = rec["model"].get(c, float("nan"))
            row[f"{c}_impl"], row[f"{c}_model"] = impl, round(model, 1)
            row[f"{c}_err"] = (
                (model / impl - 1) if impl > 0 else ""
            )  # NaN compares false
        rows.append(row)
        print(row, flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return OUT


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    print(main(args or list(FINALISTS), run="--collect" not in sys.argv))
