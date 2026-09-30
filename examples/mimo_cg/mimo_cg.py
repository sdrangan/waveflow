"""mimo_cg.py — the study's scenarios and their floating-point simulations.

Step 1.4 of ``plans/mimo_cg/mimo_cg_paper_sims.md``.  The link and detectors live in
:mod:`examples.mimo_cg.mimo_link` and :mod:`examples.mimo_cg.detectors`.  This module runs them
over the plan's grid and writes the committed tables under ``paper_data/``:

* :func:`run_float_ber` — BER vs SNR for ZF, exact MMSE and CG-MMSE at ``nit ∈ {1, 2, 3, 4, K}``
  over all 27 (M, K, modulation) configurations and ρ ∈ −20…+20 dB (1 dB steps).
* :func:`zf_crossing_db` — the analytical ZF SNR at BER 1e-3, which later sets each
  configuration's Phase 3 SNR window.
* :func:`run_float_ranges` — the dynamic range of every CG variable (Phase 2's formats).

Monte Carlo design (plan §14)
-----------------------------
* Block fading: each channel realization carries ``NS = 32`` received vectors, so CG solves the
  multi-RHS system ``A X = HᴴY`` exactly as the hardware will.
* All detectors see the same samples.  MMSE and CG estimates are divided by the exact μ
  (:func:`~examples.mimo_cg.detectors.bias_from_system`) before hard slicing.  This μ is a
  simulation-side genie that leaves CG slightly pessimistic at 2–3 iterations (see
  :mod:`examples.mimo_cg.detectors`).
* Stop rule per point: every detector has ≥ ``MIN_ERRORS`` bit errors, or ``MAX_BITS`` bits have
  been simulated.  Samples are drawn in fixed chunks of about ``CHUNK_BITS`` bits, and the rule
  is checked only at chunk boundaries, so a point's result is a pure function of its seed.
* Every point gets its own generator (:func:`~examples.mimo_cg.mimo_link.point_rng`), and the
  runner pins BLAS to one thread in each worker process.  Results are therefore identical for
  any worker count.
"""

from __future__ import annotations

import csv
import io
import itertools
import math
import multiprocessing as mp
import os
from collections.abc import Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np

from examples.mimo_cg.detectors import (
    RANGE_VARIABLES,
    ber_zf_rayleigh,
    bias_from_system,
    cg_multi_rhs,
    profile_ranges,
)
from examples.mimo_cg.mimo_link import (
    BASE_SEED,
    MODULATIONS,
    Qam,
    noise_variance,
    point_rng,
    rayleigh,
    snr_key,
)

M_VALUES = (32, 64, 128)
K_VALUES = (4, 8, 16)
SNR_DB = tuple(range(-20, 21))
NS = 32
MIN_ERRORS = 100
MAX_BITS = 10_000_000
CHUNK_BITS = 1 << 20
TARGET_BER = 1e-3
DEFAULT_CONFIG = (64, 8, "16qam")

#: Seed-key namespaces, so the BER runs, the range runs and the tests never share samples.
_BER_STREAM, _RANGE_STREAM = 20, 30

#: Environment variables that pin BLAS/OpenMP to one thread in each spawned worker.
_SINGLE_THREAD_ENV = {
    name: "1" for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
}


@dataclass(frozen=True)
class Config:
    """One (M, K, modulation) configuration of the study."""

    M: int
    K: int
    modulation: str

    @property
    def order(self) -> int:
        return MODULATIONS[self.modulation]

    @property
    def nits(self) -> tuple[int, ...]:
        """The CG iteration counts simulated: 1, 2, 3, 4 and K."""
        return tuple(sorted({1, 2, 3, 4, self.K}))

    @property
    def detectors(self) -> tuple[str, ...]:
        return ("zf", "mmse") + tuple(f"cg{n}" for n in self.nits)


def all_configs() -> list[Config]:
    """The 27 configurations, modulation-major, then M, then K."""
    return [
        Config(M, K, mod) for mod in MODULATIONS for M in M_VALUES for K in K_VALUES
    ]


# --- floating-point BER ------------------------------------------------------------------


def simulate_point(
    cfg: Config,
    rho_db: float,
    *,
    max_bits: int = MAX_BITS,
    min_errors: int = MIN_ERRORS,
) -> list[dict]:
    """BER of every detector of ``cfg`` at SNR ``rho_db``: one row per detector."""
    qam = Qam(cfg.order)
    b = qam.bits_per_symbol
    M, K = cfg.M, cfg.K
    sigma2 = noise_variance(rho_db)
    sigma = math.sqrt(sigma2)
    bits_per_block = NS * K * b
    chunk = max(1, CHUNK_BITS // bits_per_block)
    rng = point_rng(_BER_STREAM, M, K, cfg.order, snr_key(rho_db))
    nit_max = max(cfg.nits)
    eye = np.eye(K)
    errors = dict.fromkeys(cfg.detectors, 0)
    bits = blocks = 0
    while True:
        H = rayleigh(rng, (chunk, M, K))
        tx = rng.integers(0, 2, size=(chunk, NS, K * b), dtype=np.int8)
        X = np.swapaxes(qam.modulate(tx), -1, -2)
        Y = H @ X + sigma * rayleigh(rng, (chunk, M, NS))
        Hh = np.conj(np.swapaxes(H, -1, -2))
        G = Hh @ H
        A = G + sigma2 * eye
        B = Hh @ Y
        mu = bias_from_system(A, sigma2)
        estimates = {"zf": np.linalg.solve(G, B), "mmse": np.linalg.solve(A, B) / mu}
        for n, Xn in cg_multi_rhs(A, B, nit_max, iterates=cfg.nits).items():
            estimates[f"cg{n}"] = Xn / mu
        for name, est in estimates.items():
            rx = qam.demodulate(np.swapaxes(est, -1, -2))
            errors[name] += int(np.count_nonzero(rx != tx))
        bits += chunk * bits_per_block
        blocks += chunk
        if bits >= max_bits or min(errors.values()) >= min_errors:
            break
    return [
        {
            "M": M,
            "K": K,
            "modulation": cfg.modulation,
            "rho_db": rho_db,
            "detector": name,
            "nit": int(name[2:]) if name.startswith("cg") else "",
            "bit_errors": errors[name],
            "bits": bits,
            "blocks": blocks,
            "ber": errors[name] / bits,
        }
        for name in cfg.detectors
    ]


def _simulate_task(task: tuple[Config, float, int, int]) -> list[dict]:
    cfg, rho_db, max_bits, min_errors = task
    return simulate_point(cfg, rho_db, max_bits=max_bits, min_errors=min_errors)


def _run_pinned(fn, tasks: Sequence, workers: int) -> list:
    """Map ``fn`` over ``tasks`` in spawned, single-BLAS-thread workers, preserving order."""
    saved = {k: os.environ.get(k) for k in _SINGLE_THREAD_ENV}
    os.environ.update(_SINGLE_THREAD_ENV)
    try:
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=max(1, workers), mp_context=ctx) as pool:
            return list(pool.map(fn, tasks, chunksize=1))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_float_ber(
    configs: Iterable[Config] | None = None,
    snrs: Iterable[float] = SNR_DB,
    *,
    workers: int = 1,
    max_bits: int = MAX_BITS,
    min_errors: int = MIN_ERRORS,
) -> list[dict]:
    """Simulate every (config, SNR) point; rows in config, then SNR, then detector order."""
    configs = all_configs() if configs is None else list(configs)
    tasks = [(cfg, float(r), max_bits, min_errors) for cfg in configs for r in snrs]
    return [row for rows in _run_pinned(_simulate_task, tasks, workers) for row in rows]


def mmse_crossing_db(rows: Sequence[dict], target: float = TARGET_BER) -> float | None:
    """SNR where one configuration's exact-MMSE BER first falls to ``target`` (log-BER interpolation).

    ``rows`` are that configuration's ``detector == "mmse"`` rows, in SNR order.  Returns ``None``
    if the curve never reaches the target on the grid.
    """
    pts = [(r["rho_db"], r["ber"]) for r in rows]
    for (s0, b0), (s1, b1) in itertools.pairwise(pts):
        if b0 >= target > b1:
            if b1 <= 0:
                return float(s1)
            t = (math.log10(b0) - math.log10(target)) / (
                math.log10(b0) - math.log10(b1)
            )
            return float(s0 + t * (s1 - s0))
    return None


# --- analytical ZF crossings -------------------------------------------------------------


def zf_crossing_db(
    M: int,
    K: int,
    order: int,
    target: float = TARGET_BER,
    *,
    n_grid: int = 4001,
    tol_db: float = 1e-3,
) -> float:
    """SNR (dB) at which ZF in i.i.d. Rayleigh reaches ``target`` BER (bisection on log-BER)."""

    def f(s: float) -> float:
        ber = ber_zf_rayleigh(s, M, K, order, n_grid=n_grid)
        return math.log10(max(ber, 1e-300)) - math.log10(target)

    lo, hi = -40.0, 40.0
    if not f(lo) > 0 > f(hi):
        raise RuntimeError(
            f"ZF BER does not cross {target} in [-40, 40] dB for {M}x{K}, {order}"
        )
    while hi - lo > tol_db:
        mid = 0.5 * (lo + hi)
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def run_zf_crossings(configs: Iterable[Config] | None = None) -> list[dict]:
    configs = all_configs() if configs is None else list(configs)
    return [
        {
            "M": c.M,
            "K": c.K,
            "modulation": c.modulation,
            "zf_crossing_db": zf_crossing_db(c.M, c.K, c.order),
        }
        for c in configs
    ]


# --- dynamic ranges ----------------------------------------------------------------------

RANGE_SNR_DB = (-10, 0, 10, 20)
RANGE_BLOCKS = 256
RANGE_MODULATION = "16qam"


def _ranges_task(task: tuple[int, int, float]) -> list[dict]:
    M, K, rho_db = task
    qam = Qam(MODULATIONS[RANGE_MODULATION])
    rng = point_rng(_RANGE_STREAM, M, K, snr_key(rho_db))
    sigma2 = noise_variance(rho_db)
    H = rayleigh(rng, (RANGE_BLOCKS, M, K))
    tx = rng.integers(
        0, 2, size=(RANGE_BLOCKS, NS, K * qam.bits_per_symbol), dtype=np.int8
    )
    X = np.swapaxes(qam.modulate(tx), -1, -2)
    Y = H @ X + math.sqrt(sigma2) * rayleigh(rng, (RANGE_BLOCKS, M, NS))
    Hh = np.conj(np.swapaxes(H, -1, -2))
    A = Hh @ H + sigma2 * np.eye(K)
    B = Hh @ Y
    rows = []
    for normalization, scale in (("none", 1.0), ("M", float(M))):
        table = profile_ranges(A, B, K, scale=scale)
        for name in (*RANGE_VARIABLES, "ps_min", "rz_min"):
            for n, value in enumerate(table[name]):
                if not math.isnan(value):
                    rows.append(
                        {
                            "M": M,
                            "K": K,
                            "rho_db": rho_db,
                            "normalization": normalization,
                            "variable": name,
                            "iteration": n,
                            "value": value,
                        }
                    )
    return rows


def run_float_ranges(workers: int = 1) -> list[dict]:
    """Range profile of CG (nit = K) for every (M, K) at ρ ∈ ``RANGE_SNR_DB``, 16-QAM, both scalings."""
    tasks = [(M, K, float(r)) for M in M_VALUES for K in K_VALUES for r in RANGE_SNR_DB]
    return [row for rows in _run_pinned(_ranges_task, tasks, workers) for row in rows]


# --- tables ------------------------------------------------------------------------------


def _fmt(v) -> str:
    if isinstance(v, float):
        return "nan" if math.isnan(v) else f"{v:.6e}"
    return str(v)


def write_table(path: os.PathLike, rows: Sequence[dict], comment: str) -> None:
    """Write ``rows`` as CSV behind one ``#`` provenance line, with fixed float formatting."""
    buf = io.StringIO()
    buf.write(f"# {comment}\n")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(list(rows[0]))
    for row in rows:
        writer.writerow([_fmt(v) for v in row.values()])
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(buf.getvalue())


def read_table(path: os.PathLike) -> list[dict]:
    """Read a table written by :func:`write_table` (values as strings)."""
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(line for line in fh if not line.startswith("#")))


def provenance(what: str, **extra) -> str:
    items = {"numpy": np.__version__, "base_seed": BASE_SEED, **extra}
    return f"mimo_cg {what}: " + ", ".join(f"{k}={v}" for k, v in items.items())
