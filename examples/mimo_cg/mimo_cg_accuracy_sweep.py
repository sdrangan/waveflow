"""mimo_cg_accuracy_sweep.py — the Phase 3 accuracy sweep of the bit-exact CG detector.

Step 3.2 of ``plans/mimo_cg/mimo_cg_paper_sims.md``, implementing the gate 3.1 decision record
(§14).  No Vitis: every result comes from the bit-exact Python model, which steps 2.2–2.4
proved equal to Vitis.

The grid
--------
364 points: ``case`` (the 27 (M, K, modulation) configurations in the recurrence form, plus
64×8 16-QAM in the explicit form as a spot check) × ``snr_offset`` (−6…+6 dB around the
configuration's analytical ZF crossing, rounded to integer dB).  At every point, ZF, exact MMSE,
float CG and the 21 fixed-point formats (W ∈ {8…20} × g_s ∈ {0, 4, 8} on rᴴr and pᴴAp; α, β at
W; g_div = 6) are evaluated at every nit ∈ {1, 2, 3, 4, 6, 8, 12, 16} ≤ K, **paired on identical
samples**.  Unbiasing uses the exact μ, a simulation-side genie (see
:mod:`examples.mimo_cg.detectors`).

Parallelism and determinism
---------------------------
:class:`~waveflow.build.sweep.SweepRunner` is serial, so a point is parallelized inside its
stage, **across chunks**.  Samples come in chunks of about ``SWEEP_CHUNK_BITS`` bits, and chunk
``c`` has its own generator, ``point_rng(_ACC_STREAM, M, K, order, snr_key(ρ), c)``.  So a
worker computes a whole chunk (samples, the float detectors and all 21 formats) independently,
and only error counts cross processes.  Chunks run in waves of ``workers``, but their results
are folded **in chunk order**, with the stop rule (every detector has ``MIN_ERRORS`` errors,
or ``MAX_BITS`` bits) applied after each chunk.  Results are therefore identical for any worker
count.  The work is memory-bandwidth bound: on the 4-core host, 2 workers and these small,
cache-friendly chunks gave about twice the throughput of 1-Mbit chunks split by format (plan §15).

Run from the repo root::

    python -m examples.mimo_cg.mimo_cg_accuracy_sweep --dry-run --out results/dry_run.json
    python -m examples.mimo_cg.mimo_cg_accuracy_sweep --workers 8 [--resume]
    python -m examples.mimo_cg.mimo_cg_accuracy_sweep merge   # rebuild accuracy_grid.csv only
"""

from __future__ import annotations

import atexit
import dataclasses
import math
import multiprocessing as mp
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import ClassVar

import numpy as np

from examples.mimo_cg.detectors import bias_from_system, cg_multi_rhs
from examples.mimo_cg.mimo_cg import (
    K_VALUES,
    M_VALUES,
    MAX_BITS,
    MIN_ERRORS,
    NS,
    Config,
    provenance,
    read_table,
    write_table,
    zf_crossing_db,
)
from examples.mimo_cg.mimo_cg_fixed import INT_BITS, CgFormats, cg_fixed, register
from examples.mimo_cg.mimo_link import (
    MODULATIONS,
    Qam,
    noise_variance,
    point_rng,
    rayleigh,
    snr_key,
)
from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.sweep import ParamGrid, Stage, SweepRunner, sweep_cli

HERE = Path(__file__).resolve().parent

W_VALUES = (8, 10, 12, 14, 16, 18, 20)
GUARDS = (0, 4, 8)
G_DIV = 6
NITS = (1, 2, 3, 4, 6, 8, 12, 16)
SNR_OFFSETS = tuple(range(-6, 7))
SPOT_CHECK = (64, 8, "16qam")
#: Bits per chunk: small enough to stay cache-friendly (512 blocks at 32×4 QPSK, 42 at 128×16
#: 64-QAM) and to let low-SNR points stop early.
SWEEP_CHUNK_BITS = 1 << 17
_ACC_STREAM = 70  # seed namespace: never shared with Phase 1 (20, 30) or the tests


def sweep_format(W: int, g: int) -> CgFormats:
    """Gate 3.1 formats: width W, guard g on rᴴr and pᴴAp only, α and β at W, g_div = 6."""
    base = CgFormats.from_width(W, 0, G_DIV)
    return dataclasses.replace(
        base, rz=register(W + g, INT_BITS["rz"]), ps=register(W + g, INT_BITS["ps"])
    )


FORMATS: dict[str, CgFormats] = {
    f"W{W}g{g}": sweep_format(W, g) for W in W_VALUES for g in GUARDS
}


@dataclass(frozen=True)
class SweepConfig:
    """One value of the ``case`` axis."""

    name: str
    M: int
    K: int
    modulation: str
    explicit: bool

    @property
    def nits(self) -> tuple[int, ...]:
        return tuple(n for n in NITS if n <= self.K)

    @property
    def config(self) -> Config:
        return Config(self.M, self.K, self.modulation)

    @property
    def detectors(self) -> list[str]:
        float_cg = [f"cg{n}" for n in self.nits]
        fixed = [f"fx:{f}:cg{n}" for f in FORMATS for n in self.nits]
        return ["zf", "mmse", *float_cg, *fixed]


def _configs() -> dict[str, SweepConfig]:
    out = {}
    for mod in MODULATIONS:
        for M in M_VALUES:
            for K in K_VALUES:
                out[f"{mod}_{M}x{K}"] = SweepConfig(f"{mod}_{M}x{K}", M, K, mod, False)
    M, K, mod = SPOT_CHECK
    out[f"{mod}_{M}x{K}_explicit"] = SweepConfig(
        f"{mod}_{M}x{K}_explicit", M, K, mod, True
    )
    return out


CONFIGS = _configs()
GRID = ParamGrid(case=tuple(CONFIGS), snr_offset=SNR_OFFSETS)


@cache
def zf_crossing(M: int, K: int, modulation: str) -> float:
    """The analytical ZF BER-1e-3 SNR: from the committed table, else computed."""
    path = HERE / "paper_data" / "zf_crossings.csv"
    if path.exists():
        for r in read_table(path):
            if (int(r["M"]), int(r["K"]), r["modulation"]) == (M, K, modulation):
                return float(r["zf_crossing_db"])
    return zf_crossing_db(M, K, MODULATIONS[modulation])


def point_snr(config: str, snr_offset: int) -> float:
    c = CONFIGS[config]
    return float(round(zf_crossing(c.M, c.K, c.modulation)) + snr_offset)


def expected_rows(config: str) -> int:
    return len(CONFIGS[config].detectors)


# --- one chunk of one point ----------------------------------------------------------------


def _chunk(c: SweepConfig, rho_db: float, index: int):
    """Chunk ``index`` of a point: channels, bits, A, B and μ.  A pure function of its args."""
    qam = Qam(MODULATIONS[c.modulation])
    b = qam.bits_per_symbol
    blocks = max(1, SWEEP_CHUNK_BITS // (NS * c.K * b))
    rng = point_rng(_ACC_STREAM, c.M, c.K, qam.order, snr_key(rho_db), index)
    sigma2 = noise_variance(rho_db)
    H = rayleigh(rng, (blocks, c.M, c.K))
    tx = rng.integers(0, 2, size=(blocks, NS, c.K * b), dtype=np.int8)
    X = np.swapaxes(qam.modulate(tx), -1, -2)
    Y = H @ X + math.sqrt(sigma2) * rayleigh(rng, (blocks, c.M, NS))
    Hh = np.conj(np.swapaxes(H, -1, -2))
    G = Hh @ H
    A = G + sigma2 * np.eye(c.K)
    return qam, tx, G, A, Hh @ Y, bias_from_system(A, sigma2), blocks


def _chunk_task(task: tuple) -> dict:
    """Errors and mismatches of every detector (float and all formats) on one chunk."""
    config, rho_db, index = task
    names, with_float = list(FORMATS), True
    c = CONFIGS[config]
    qam, tx, G, A, B, mu, blocks = _chunk(c, rho_db, index)

    def decide(est: np.ndarray) -> np.ndarray:
        return qam.demodulate(np.swapaxes(est, -1, -2))

    flt = {
        f"cg{n}": decide(X / mu)
        for n, X in cg_multi_rhs(
            A, B, max(c.nits), explicit_residual=c.explicit, iterates=c.nits
        ).items()
    }
    out = {}
    if with_float:
        out["zf"] = (int(np.count_nonzero(decide(np.linalg.solve(G, B)) != tx)), 0)
        out["mmse"] = (
            int(np.count_nonzero(decide(np.linalg.solve(A, B) / mu) != tx)),
            0,
        )
        for name, rx in flt.items():
            out[name] = (int(np.count_nonzero(rx != tx)), 0)
    for fname in names:
        xs = cg_fixed(
            A, B, max(c.nits), FORMATS[fname], scale=c.M, explicit_residual=c.explicit,
            iterates=c.nits,
        )  # fmt: skip
        for n in c.nits:
            rx = decide(xs[n].real / mu)
            out[f"fx:{fname}:cg{n}"] = (
                int(np.count_nonzero(rx != tx)),
                int(np.count_nonzero(rx != flt[f"cg{n}"])),
            )
    return {"counts": out, "bits": int(tx.size), "blocks": blocks}


# --- the parallel point ----------------------------------------------------------------------

_POOL: dict[int, ProcessPoolExecutor] = {}
_SINGLE_THREAD_ENV = {
    k: "1" for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
}


def _pool(workers: int) -> ProcessPoolExecutor:
    """A persistent spawned pool with single-threaded BLAS, reused across points."""
    if workers not in _POOL:
        # Pinned for the rest of this process: the executor spawns workers lazily, and every one
        # must start with single-threaded BLAS (the parent's own BLAS is already initialized).
        os.environ.update(_SINGLE_THREAD_ENV)
        ctx = mp.get_context("spawn")
        _POOL[workers] = ProcessPoolExecutor(max_workers=workers, mp_context=ctx)
        atexit.register(_POOL[workers].shutdown)
    return _POOL[workers]


def simulate_sweep_point(
    config: str,
    snr_offset: int,
    *,
    workers: int = 1,
    max_bits: int = MAX_BITS,
    min_errors: int = MIN_ERRORS,
) -> list[dict]:
    """Every detector of one point, paired on identical chunks; one row per detector."""
    c = CONFIGS[config]
    rho_db = point_snr(config, snr_offset)
    errors = dict.fromkeys(c.detectors, 0)
    mismatch = dict.fromkeys(c.detectors, 0)
    bits = blocks = index = 0
    done = False
    while not done:
        tasks = [(config, rho_db, index + k) for k in range(max(1, workers))]
        if workers > 1:
            results = list(_pool(workers).map(_chunk_task, tasks))
        else:
            results = [_chunk_task(t) for t in tasks]
        for (
            res
        ) in results:  # in chunk order: the stop rule cannot depend on the worker count
            for det, (e, m) in res["counts"].items():
                errors[det] += e
                mismatch[det] += m
            bits += res["bits"]
            blocks += res["blocks"]
            index += 1
            if bits >= max_bits or min(errors.values()) >= min_errors:
                done = True
                break
    rows = []
    for det in c.detectors:
        W = g = nit = ""
        if det.startswith("fx:"):
            fname, cgn = det[3:].split(":")
            W, g = (int(x) for x in fname[1:].split("g"))
            nit = int(cgn[2:])
        elif det.startswith("cg"):
            nit = int(det[2:])
        rows.append(
            {
                "modulation": c.modulation,
                "M": c.M,
                "K": c.K,
                "residual": "explicit" if c.explicit else "recurrence",
                "rho_db": rho_db,
                "detector": det,
                "W": W,
                "g_s": g,
                "nit": nit,
                "bit_errors": errors[det],
                "mismatch": mismatch[det],
                "bits": bits,
                "ber": errors[det] / bits,
            }
        )
    return rows


# --- the DAG, the runner and the merge -------------------------------------------------------


def _point_csv(root: Path, config: str, snr_offset: int) -> Path:
    return (
        Path(root) / "results" / "accuracy_points" / f"{config}_off{snr_offset:+d}.csv"
    )


_SOURCES = {
    "mimo_link_source": "mimo_link.py",
    "detectors_source": "detectors.py",
    "fixed_source": "mimo_cg_fixed.py",
    "sweep_source": "mimo_cg_accuracy_sweep.py",
}


@dataclass(kw_only=True)
class AccuracyPointStep(BuildStep):
    description = (
        "Simulate one (config, SNR) point: every detector, paired on identical samples."
    )
    consumes: ClassVar[list] = list(_SOURCES)
    produces: ClassVar[dict] = {
        "accuracy_point": Path("results/accuracy_points/last_point.txt")
    }
    params: ClassVar[dict] = {
        "case": "",
        "snr_offset": 0,
        "workers": 1,
        "max_bits": MAX_BITS,
        "min_errors": MIN_ERRORS,
    }

    def run(self, config: BuildConfig, **kw) -> dict:
        root = Path(config.root_dir)
        rows = simulate_sweep_point(
            kw["case"],
            kw["snr_offset"],
            workers=kw["workers"],
            max_bits=kw["max_bits"],
            min_errors=kw["min_errors"],
        )
        path = _point_csv(root, kw["case"], kw["snr_offset"])
        path.parent.mkdir(parents=True, exist_ok=True)
        write_table(path, rows, provenance("accuracy_point", max_bits=kw["max_bits"]))
        marker = root / "results" / "accuracy_points" / "last_point.txt"
        marker.write_text(f"{path.name}\n", encoding="utf-8")
        return {"accuracy_point": marker}


@dataclass(kw_only=True)
class DryRunPointStep(BuildStep):
    description = "Pre-flight one point: its SNR and its row count, without simulating."
    consumes: ClassVar[list] = []
    produces: ClassVar[dict] = {
        "dry_point": Path("results/accuracy_points/dry_point.txt")
    }
    params: ClassVar[dict] = {"case": "", "snr_offset": 0}

    def run(self, config: BuildConfig, **kw) -> dict:
        path = Path(config.root_dir) / "results" / "accuracy_points" / "dry_point.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        rho = point_snr(kw["case"], kw["snr_offset"])
        path.write_text(
            f"{kw['case']} {rho:+.0f} dB {expected_rows(kw['case'])} rows\n"
        )
        return {"dry_point": path}


def build_accuracy_dag() -> BuildDag:
    dag = BuildDag()
    for artifact, path in _SOURCES.items():
        dag.add(
            SourceStep(artifact=artifact, path=HERE / path)
        )  # absolute: any root_dir
    dag.add(AccuracyPointStep(name="accuracy_point"))
    dag.add(DryRunPointStep(name="dry_point"))
    return dag


#: Measured on the 4-core host: memory-bound, so 2 workers beat 8 (plan §15).
DEFAULT_WORKERS = min(2, os.cpu_count() or 1)


def merge_points(root: Path = HERE, points=None) -> Path:
    """Merge the point CSVs (default: the whole grid) into ``paper_data/accuracy_grid.csv``,
    in grid order."""
    points = list(GRID if points is None else points)
    rows = []
    missing = []
    for point in points:
        path = _point_csv(root, point["case"], point["snr_offset"])
        if not path.exists():
            missing.append(path.name)
            continue
        rows += read_table(path)
    if missing:
        raise RuntimeError(
            f"{len(missing)} point(s) missing, e.g. {missing[:3]}; run the sweep"
        )
    out = Path(root) / "paper_data" / "accuracy_grid.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_table(
        out, rows, provenance("accuracy_grid", max_bits=MAX_BITS, points=len(points))
    )
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["merge"]:
        print(f"merged -> {merge_points()}")
        return 0
    total_rows = sum(expected_rows(p["case"]) for p in GRID)
    print(
        f"accuracy sweep: {len(GRID)} points, {len(FORMATS)} formats, {total_rows} expected rows"
    )
    workers = DEFAULT_WORKERS
    if "--workers" in argv:
        i = argv.index("--workers")
        workers = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2 :]
    runner = SweepRunner(
        dag_factory=build_accuracy_dag,
        root_dir=HERE,
        summary=HERE / "results" / "accuracy_sweep.json",
        extra_params={
            "workers": workers,
            "max_bits": MAX_BITS,
            "min_errors": MIN_ERRORS,
        },
    )
    rc = sweep_cli(
        runner,
        GRID,
        description="mimo_cg accuracy sweep (Phase 3)",
        stages=[Stage(through="accuracy_point", use_platform=False)],
        dry_run_stages=[Stage(through="dry_point", use_platform=False)],
        argv=argv,
    )
    if rc == 0 and "--dry-run" not in argv and len(argv) == argv.count("--resume"):
        print(f"merged -> {merge_points()}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
