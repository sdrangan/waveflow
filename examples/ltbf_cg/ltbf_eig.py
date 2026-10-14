"""ltbf_eig.py -- the A53's share of the LTBF long-term update, priced on the processor model.

``plans/mimo_cg/ltbf_cg_dse.md`` step 4.8 (AC6).  Per UE the engine hands ``A_i`` to the A53, which
forms ``M_i = sym(H_i^H A_i)`` (8 x 8), eigendecomposes it by cyclic complex Jacobi and forms
``G_i = (A_i Phi)^H`` for the top ``r`` eigenvectors.  ``cpu/ltbf_eig.c`` is that program;
:func:`ltbf_eig` is its bit-exact twin (outputs and work counters), :data:`KERNEL` its calibration
:class:`~waveflow.cpu.calib.kernels.Kernel` and :data:`FAMILY` the cost model's form: cycles linear in
``n`` (the 64 n complex products of ``M``), ``n r`` (the 8 n r of ``G``), the rotations and the
sweeps.

The campaign follows ``waveflow/cpu/calib``'s rules: the points and their roles are registered in
``waveflow/cpu/calib/sweep.py`` (:func:`~waveflow.cpu.calib.sweep.ltbf_eig_rows`) and committed as
``cpu/sweep_plan.csv`` before any is measured; gem5 runs from a work directory inside this example's
``data/``; the corpus and the fitted model are local (``data/a53_ltbf/``).  Fit on the fit points,
look at validation, score the test points once.

    python -m examples.ltbf_cg.ltbf_eig plan       # write cpu/sweep_plan.csv (then commit it)
    python -m examples.ltbf_cg.ltbf_eig smoke      # twin == host build == gem5 on the smoke points
    python -m examples.ltbf_cg.ltbf_eig campaign   # gem5 on every registered point (resumable)
    python -m examples.ltbf_cg.ltbf_eig fit        # fit, validation report, the test score once
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

from waveflow.cpu.calib.calibrate import Family
from waveflow.cpu.calib.kernels import KERNEL_DIR, Kernel
from waveflow.cpu.calib.kernels.common import Rng

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "cpu" / "ltbf_eig.c"
PLAN = HERE / "cpu" / "sweep_plan.csv"
DATA = HERE / "data" / "a53_ltbf"
K = 8
MAX_SWEEPS = 12


def _bits(x: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", x))[0]


def _jacobi(m: list[list[list[float]]]) -> tuple[list[list[list[float]]], int, int]:
    """Cyclic complex Jacobi on the Hermitian ``m`` (``[re, im]`` entries), in place: the
    eigenvectors, rotations and sweeps.  The same operations in the same order as ``jacobi()``.
    """
    total = 0.0
    v = [[[1.0 if p == q else 0.0, 0.0] for q in range(K)] for p in range(K)]
    for p in range(K):
        for q in range(K):
            total += m[p][q][0] * m[p][q][0] + m[p][q][1] * m[p][q][1]
    n_rot = n_sweeps = 0
    for _ in range(MAX_SWEEPS):
        off = 0.0
        for p in range(K):
            for q in range(p + 1, K):
                off += m[p][q][0] * m[p][q][0] + m[p][q][1] * m[p][q][1]
        if off <= 1e-24 * total:
            break
        n_sweeps += 1
        for p in range(K - 1):
            for q in range(p + 1, K):
                mag2 = m[p][q][0] * m[p][q][0] + m[p][q][1] * m[p][q][1]
                if mag2 <= 1e-30 * total:
                    continue
                mag = math.sqrt(mag2)
                er, ei = m[p][q][0] / mag, m[p][q][1] / mag
                tau = (m[q][q][0] - m[p][p][0]) / (2.0 * mag)
                t = 1.0 / (abs(tau) + math.sqrt(1.0 + tau * tau))
                if tau < 0.0:
                    t = -t
                c = 1.0 / math.sqrt(1.0 + t * t)
                s = t * c
                for mat in (m, v):
                    for k in range(K):
                        ar, ai = mat[k][p]
                        br, bi = mat[k][q]
                        mat[k][p] = [
                            c * ar - s * (er * br + ei * bi),
                            c * ai - s * (er * bi - ei * br),
                        ]
                        mat[k][q] = [
                            s * (er * ar - ei * ai) + c * br,
                            s * (er * ai + ei * ar) + c * bi,
                        ]
                for k in range(K):
                    ar, ai = m[p][k]
                    br, bi = m[q][k]
                    m[p][k] = [
                        c * ar - s * (er * br - ei * bi),
                        c * ai - s * (er * bi + ei * br),
                    ]
                    m[q][k] = [
                        s * (er * ar + ei * ai) + c * br,
                        s * (er * ai - ei * ar) + c * bi,
                    ]
                n_rot += 1
    return v, n_rot, n_sweeps


def _eig_project(a, h, n: int, r: int) -> tuple[list, float, int, int]:
    """``M``, its Jacobi eigendecomposition and ``G`` (r x n, ``[re, im]``), as ``ltbf_eig()``."""
    m = [[[0.0, 0.0] for _ in range(K)] for _ in range(K)]
    for p in range(K):
        for q in range(K):
            re = im = 0.0
            for i in range(n):
                hr, hi = h[i][p]
                ar, ai = a[i][q]
                re += hr * ar + hi * ai
                im += hr * ai - hi * ar
            m[p][q] = [re, im]
    for p in range(K):
        for q in range(p, K):
            re = 0.5 * (m[p][q][0] + m[q][p][0])
            im = 0.5 * (m[p][q][1] - m[q][p][1])
            m[p][q] = [re, im]
            m[q][p] = [re, -im]
    v, n_rot, n_sweeps = _jacobi(m)
    top = [0, 0]
    for j in range(r):
        best = -1
        for k in range(K):
            if j == 1 and k == top[0]:
                continue
            if best < 0 or m[k][k][0] > m[best][best][0]:
                best = k
        top[j] = best
    g = []
    for j in range(r):
        row = []
        for i in range(n):
            re = im = 0.0
            for k in range(K):
                ar, ai = a[i][k]
                br, bi = v[k][top[j]]
                re += ar * br - ai * bi
                im += ar * bi + ai * br
            row.append([re, -im])
        g.append(row)
    return g, m[top[0]][top[0]][0], n_rot, n_sweeps


def ltbf_eig(n: int, r: int, rank: int = 8, seed: int = 1) -> dict[str, Any]:
    """The twin of ``cpu/ltbf_eig.c``: its inputs, its computation, its printed JSON."""
    n, r, rank = int(n), int(r), int(rank)
    rng = Rng(int(seed))

    def draw() -> float:
        x = rng.next()
        return float(x - (1 << 32) if x & 0x80000000 else x) / 2147483648.0

    u = [[[draw(), draw()] for _ in range(rank)] for _ in range(n)]
    w = [[[draw(), draw()] for _ in range(K)] for _ in range(rank)]
    h, a = [], []
    for i in range(n):
        hrow, arow = [], []
        for q in range(K):
            re = im = 0.0
            for k in range(rank):
                re += u[i][k][0] * w[k][q][0] - u[i][k][1] * w[k][q][1]
                im += u[i][k][0] * w[k][q][1] + u[i][k][1] * w[k][q][0]
            hre = re + draw() * 0.0009765625
            him = im + draw() * 0.0009765625
            hrow.append([hre, him])
            arow.append([draw(), draw()])
        h.append(hrow)
        a.append(arow)
    g, lam, n_rot, n_sweeps = _eig_project(a, h, n, r)
    total = 0.0
    for j in range(r):
        for i in range(n):
            total += g[j][i][0] + g[j][i][1]
    return {
        "kernel": "ltbf_eig", "n": n, "r": r, "rank": rank, "n_rot": n_rot,
        "n_sweeps": n_sweeps, "g_sum_bits": _bits(total), "lam_bits": _bits(lam),
    }  # fmt: skip


def eig_counters(a_i: np.ndarray, h_i: np.ndarray, r: int) -> dict[str, float]:
    """The cost model's counters for one real UE (``a_i``, ``h_i``: ``n x 8``): what the DSE prices."""
    n = a_i.shape[0]
    to = lambda x: [[[float(v.real), float(v.imag)] for v in row] for row in x]
    _, _, n_rot, n_sweeps = _eig_project(to(a_i), to(h_i), n, r)
    return {
        "n": float(n),
        "r": float(r),
        "n_rot": float(n_rot),
        "n_sweeps": float(n_sweeps),
    }


def _features(row: dict) -> dict:
    return {
        "n": float(row["n"]), "nr": float(row["n"]) * float(row["r"]),
        "n_rot": float(row["n_rot"]), "n_sweeps": float(row["n_sweeps"]),
    }  # fmt: skip


KERNEL = Kernel(
    name="ltbf_eig",
    twin=ltbf_eig,
    args=("n", "r", "rank", "seed"),
    counters=("n", "r", "n_rot", "n_sweeps"),
    working_set=lambda o: 2.0 * 16.0 * K * o["n"],  # A and H, complex doubles
    smoke=(
        {"n": 8, "r": 1, "rank": 1, "seed": 1},
        {"n": 40, "r": 2, "rank": 8, "seed": 7},
    ),
    symbols=("ltbf_eig", "form_m", "jacobi"),
    source_path=SOURCE,
)

FAMILY = Family(
    "ltbf_eig", "ltbf_eig", ("n", "r", "n_rot", "n_sweeps"), ("n", "nr", "n_rot", "n_sweeps"),
    transform=_features,
)  # fmt: skip


# --- the campaign -----------------------------------------------------------------------------


def host_run(point: dict[str, Any], workdir: Path = DATA / "host") -> dict:
    """The C program built and run on the host (no gem5), for the twin check."""
    workdir.mkdir(parents=True, exist_ok=True)
    exe = workdir / "ltbf_eig_host"
    if not exe.is_file() or exe.stat().st_mtime < SOURCE.stat().st_mtime:
        subprocess.run(
            ["gcc", "-O2", f"-I{KERNEL_DIR}", str(SOURCE), "-lm", "-o", str(exe)],
            check=True, capture_output=True, text=True,
        )  # fmt: skip
    run = subprocess.run(
        [str(exe), *KERNEL.argv(point)], check=True, capture_output=True, text=True
    )
    return json.loads(run.stdout)


def runner():
    from waveflow.cpu.calib.gem5 import Gem5Runner

    return Gem5Runner(workdir=DATA / "gem5")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("cmd", choices=("plan", "smoke", "campaign", "fit"))
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args(argv)
    if args.cmd == "plan":
        from waveflow.cpu.calib.prereg import SweepPlan
        from waveflow.cpu.calib.sweep import ltbf_eig_rows

        print(
            f"wrote {SweepPlan.write(PLAN, ltbf_eig_rows())}; commit it before measuring"
        )
        return 0
    if args.cmd == "smoke":
        run = runner()
        for pt in KERNEL.smoke:
            twin, host = ltbf_eig(**pt), host_run(pt)
            row = run.measure(KERNEL, pt, empty_cycles=0.0, smoke=True)
            print(
                pt,
                "twin == host:",
                twin == host,
                "| gem5 row:",
                {k: row.get(k) for k in ("cycles", "n_rot", "twin_ok")},
            )
        return 0
    if args.cmd == "campaign":
        return campaign(args.workers)
    return fit()


CORPUS = DATA / "corpus.csv"


def campaign(workers: int = 4) -> int:
    """gem5 on every registered point not yet in the local corpus (resumable)."""
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    import pandas as pd  # type: ignore[import-untyped]

    from waveflow.cpu.calib.prereg import SweepPlan, point_key

    plan = SweepPlan.load(PLAN)
    run = runner()
    why = run.unavailable()
    if why:
        raise RuntimeError(f"cannot measure: {why}")
    done: set[str] = set()
    if CORPUS.is_file():
        done = {
            point_key(KERNEL.name, json.loads(p)) for p in pd.read_csv(CORPUS)["point"]
        }
    todo = [
        json.loads(r.point)
        for r in pd.read_csv(plan.path).itertuples()
        if r.kernel == KERNEL.name
        and point_key(r.kernel, json.loads(r.point)) not in done
    ]
    run.build(KERNEL)
    empty = run.empty_region_cycles()
    print(
        f"{len(todo)} points, {len(done)} done; empty region {empty:g} cycles",
        flush=True,
    )
    t0, bad = time.monotonic(), 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(run.measure, KERNEL, pt, empty_cycles=empty, plan=plan): pt
            for pt in todo
        }
        for i, fut in enumerate(as_completed(futs), 1):
            pt = futs[fut]
            try:
                row = fut.result()
            except Exception as exc:  # noqa: BLE001 - record and keep going
                bad += 1
                print(f"[{i}/{len(todo)}] FAILED {pt}: {exc}", flush=True)
                continue
            CORPUS.parent.mkdir(parents=True, exist_ok=True)
            new = pd.DataFrame([row])
            if CORPUS.is_file():
                new = pd.concat([pd.read_csv(CORPUS), new], ignore_index=True)
            new.to_csv(CORPUS, index=False)
            bad += not row["output_matches_twin"]
            print(f"[{i}/{len(todo)}] {pt} {row['role']}: {row['cycles']:g} cycles, "
                  f"twin {'ok' if row['output_matches_twin'] else 'MISMATCH'}", flush=True)  # fmt: skip
    print(f"{(time.monotonic() - t0) / 60:.1f} min", flush=True)
    return 1 if bad else 0


def fit() -> int:
    """Fit the cycle model on the fit points; report validation; score the test points once."""
    import pandas as pd  # type: ignore[import-untyped]

    df = FAMILY.select(pd.read_csv(CORPUS))
    model = FAMILY.model("cycles")
    model.fit(df[df["role"] == "fit"])
    out = {}
    for role in ("fit", "validation", "test"):
        part = df[df["role"] == role]
        pred = np.array([model.predict_feat(r) for r in part.to_dict("records")])
        err = np.abs(pred - part["cycles"].to_numpy()) / part["cycles"].to_numpy()
        out[role] = {
            "points": len(part),
            "median": float(np.median(err)),
            "max": float(err.max()),
        }
        print(
            f"{role:10s} {len(part):3d} points: median {np.median(err):.1%}, max {err.max():.1%}"
        )
    (DATA / "model.json").write_text(
        json.dumps({"params": model.to_params(), "errors": out}, indent=1)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
