"""rtl_anchor.py -- csynth's structural cycle count against RTL (XSI) job durations.

``plans/mimo_cg/ltbf_cg_dse.md`` step 4.9: on a kept build tree, a small program (a joint solve with
``H``, two per-UE solves with ``H`` kept, a second sector) runs through the system flow; each job's
duration (the last X word's arrival minus the previous job's) is divided by
:func:`examples.ltbf_cg.hw.models.job_cycles` with the build's own csynth overheads.

    python -m examples.ltbf_cg.hw.rtl_anchor f_n256_c240_m80_r4x4_w16 --n 256 --cols 240 --m 16 --nit 2
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np

from examples.ltbf_cg.hw import build as hb
from examples.ltbf_cg.hw import engine as eg
from examples.ltbf_cg.hw import models as md
from examples.ltbf_cg.hw import space as sp
from examples.ltbf_cg.hw import system_build as sb
from waveflow.build.build import BuildConfig


def anchor(name: str, n: int, cols: int, m: int, nit: int) -> dict:
    root = hb.BUILD / name
    rec = json.loads((root / "report.json").read_text())
    k = sp.Knobs(**rec["knobs"])
    s = eg.LtbfSystem(
        **k.as_dict(), nit=nit, n_run=n, cols_run=cols, m_run=m, n_cycles=20_000_000
    )
    top = f"ltbf_anchor_{name}"
    res = sb.dag_for(s, top).run(
        BuildConfig(root_dir=root, params={"synth": "check"}), through="compare"
    )
    bad = {a: r.message for a, r in res.items() if not r.success}
    if bad:
        raise RuntimeError(bad)
    work = root / sb.WORK_DIR / top
    cy = np.fromfile(
        work / "xsi_run" / "vectors" / "m_out" / "cycles.bin", dtype=np.uint64
    )
    got = np.fromfile(
        work / "xsi_run" / "vectors" / "m_out" / "words.bin", dtype=np.uint64
    )
    np.testing.assert_array_equal(got, np.concatenate(s.expected()))
    o = md.overheads_from_loops(k, rec["loops"])
    out, end, prev = [], 0, 0
    for job in s.jobs():
        mm = job.b.shape[1]
        end += n * mm
        dur = int(cy[end - 1]) - prev
        prev = int(cy[end - 1])
        pred = md.job_cycles(k, o, n, cols, mm, nit, job.h is not None)
        out.append({"m": mm, "load_h": job.h is not None, "xsi": dur, "structural": pred,
                    "ratio": dur / pred})  # fmt: skip
    return {"name": name, "n": n, "cols": cols, "m": m, "nit": nit, "jobs": out}


def gate_anchor(form: str) -> dict:
    """The RTL gate's own run (``tests/build/_xsi_work``) as an anchor: four jobs at N_rx = 64."""
    from tests.examples._system_run import WORK

    k = sp.Knobs(**sb.GATE[form])
    rec = json.loads((hb.BUILD / k.name / "report.json").read_text())
    o = md.overheads_from_loops(k, rec["loops"])
    s = sb.system(form)
    cy = np.fromfile(
        WORK / sb.top(form) / "xsi_run" / "vectors" / "m_out" / "cycles.bin",
        dtype=np.uint64,
    )
    out, end, prev = [], 0, 0
    for job in s.jobs():
        mm = job.b.shape[1]
        end += k.nmax * mm
        dur = int(cy[end - 1]) - prev
        prev = int(cy[end - 1])
        pred = md.job_cycles(k, o, k.nmax, k.cmax, mm, s.nit, job.h is not None)
        out.append(
            {"m": mm, "load_h": job.h is not None, "xsi": dur, "structural": pred}
        )
    return {"name": k.name, "n": k.nmax, "cols": k.cmax, "nit": s.nit, "jobs": out}


def fit_delta() -> dict:
    """The per-tile correction ``delta`` (least squares over every anchored job) and residuals."""
    anchors = [gate_anchor("factored"), gate_anchor("explicit")]
    anchors += [
        json.loads(p.read_text())
        for p in sorted((hb.BUILD.parent / "reports").glob("anchor_*.json"))
    ]
    xs, ys, rows = [], [], []
    for a in anchors:
        k = sp.Knobs(
            **json.loads((hb.BUILD / a["name"] / "report.json").read_text())["knobs"]
        )
        for j in a["jobs"]:
            tiles = md.product_tiles(
                k, a["n"], a["cols"], j["m"], a["nit"], j["load_h"]
            )
            xs.append(tiles)
            ys.append(j["structural"] - j["xsi"])
            rows.append(
                (a["name"], j["m"], j["load_h"], j["xsi"], j["structural"], tiles)
            )
    x, y = np.array(xs, float), np.array(ys, float)
    delta = float(x @ y / (x @ x))
    res = [
        {"build": b, "m": m, "load_h": lh, "xsi": xv, "predicted": st - delta * t,
         "rel_err": abs(st - delta * t - xv) / xv}
        for b, m, lh, xv, st, t in rows
    ]  # fmt: skip
    out = {"delta": delta, "jobs": res, "max_rel_err": max(r["rel_err"] for r in res)}
    (hb.BUILD.parent / "reports" / "rtl_fit.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("name")
    for f in ("n", "cols", "m", "nit"):
        ap.add_argument(f"--{f}", type=int, required=True)
    a = ap.parse_args()
    r = anchor(a.name, a.n, a.cols, a.m, a.nit)
    (hb.BUILD.parent / "reports").mkdir(exist_ok=True)
    (hb.BUILD.parent / "reports" / f"anchor_{a.name}.json").write_text(
        json.dumps(r, indent=1)
    )
    for j in r["jobs"]:
        print(j)
    sys.exit(0)
