"""The vector unit configured and fed for CG: ``CgVecUnit`` is a :class:`~waveflow.vecunit.unit.VecUnit`
whose scenarios are CG solves (the unit itself stays application-free; this subclass only supplies
the configuration from a ``CgFormats`` and the jobs).

A job's stimulus is its three programs, ``B``, and the ``nit`` matrices ``S`` the systolic array would
send -- ``S = cg.mm_step(A, P)`` on each ``P`` the unit emits, taken from a closed-loop run of the
interpreter (:func:`cg_job`), so a static stream carries exactly what the loop would.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import numpy as np

from examples.vecunit_cg.cg_program import PORT, cg_config, cg_programs
from waveflow.linalg import cg
from waveflow.utils import fixputils as fxu
from waveflow.utils.fixputils import Format, OMode, QMode
from waveflow.vecunit.model import run_job
from waveflow.vecunit.unit import VecUnit


def reg(W: int, I: int) -> Format:
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


#: The guide's centre formats (``docs/guide/dsp/cg_vector.md``): 12-bit vectors, ``alpha``, ``beta``;
#: 20-bit ``ps`` and ``rz``; ``g_div`` 6 -- the CG core's calibration build.
CENTRE = cg.CgFormats(A=reg(12, 3), B=reg(12, 4), P=reg(12, 4), R=reg(12, 4), S=reg(12, 5),
                      X=reg(12, 3), ps=reg(20, 10), rz=reg(20, 9), alpha=reg(12, 5),
                      beta=reg(12, 3), g_div=6)


def problem(f: cg.CgFormats, k: int, n: int, seed: int):
    """A random Hermitian positive definite ``A`` (``k x k``) and a ``B`` (``k x n``), quantized
    (the guide's construction)."""
    rng = np.random.default_rng(seed)
    m = 8 * k
    H = (rng.standard_normal((m, k)) + 1j * rng.standard_normal((m, k))) / np.sqrt(2)
    Y = (rng.standard_normal((m, n)) + 1j * rng.standard_normal((m, n))) / np.sqrt(2)
    A = (H.conj().T @ H + 0.1 * np.eye(k)) / m
    B = H.conj().T @ Y / m
    q = fxu.quantize_real
    return q(A.real, f.A), q(A.imag, f.A), q(B.real, f.B), q(B.imag, f.B)


def cg_job(cfg, f: cg.CgFormats, ar, ai, br, bi, nit: int, tx_id: int, *, skip_dead: bool = True):
    """One CG solve as a job ``(tx_id, Programs, inputs)``: its inputs are ``B`` then the ``S``
    matrices a closed-loop run hands the unit."""
    k, n = np.shape(br)
    progs = cg_programs(cfg, nit, k, n, skip_dead=skip_dead)
    fed = [(np.asarray(br), np.asarray(bi))]

    def systolic(port, result):
        _resp, pr, pi = result.outputs[PORT][-1]
        s = cg.mm_step(ar, ai, pr, pi, f)
        fed.append(s)
        return s

    run_job(cfg, *progs, {PORT: [fed[0]]}, tx_id=tx_id, on_starve=systolic)
    return tx_id, progs, {PORT: fed}


@dataclass
class CgVecUnit(VecUnit):
    """A vector unit for CG on ``k x n`` jobs up to ``Kmax x Nmax`` with ``cg_formats``."""

    cg_formats: cg.CgFormats = CENTRE
    Kmax: int = 8
    Nmax: int = 32
    L: int = 4
    build_fields: ClassVar[tuple[str, ...]] = ("cfg", "cg_formats", "Kmax", "Nmax", "L")

    def __post_init__(self) -> None:
        if self.cfg is None:
            self.cfg = cg_config(self.cg_formats, Kmax=self.Kmax, Nmax=self.Nmax, L=self.L)
        super().__post_init__()

    def jobs(self, kind: str = "test") -> list:
        """``"test"``: four solves -- the full ``Kmax x Nmax`` at three iterations, a short one
        (``k`` 5, ``n`` 13, ``L`` not dividing it), one iteration, and a zero column (the guards).
        ``"steady"``: four full solves of three iterations (the comparison with the CG core).
        ``"calib"``, ``"holdout"``: the calibration's pre-registered jobs
        (:data:`waveflow.vecunit.calib.CG_JOBS`): random jobs over every instruction form on this
        configuration, then CG solves."""
        from waveflow.vecunit.calib import CG_JOBS

        f, K, N = self.cg_formats, int(self.Kmax), int(self.Nmax)
        out = []
        if kind == "test":
            shapes = [(K, N, 3, 1), (min(K, 5), min(N, 13), 2, 2), (K, N, 1, 3), (K, min(N, 9), 4, 4)]
        elif kind == "steady":
            shapes = [(K, N, 3, s) for s in range(4)]
        elif kind in CG_JOBS:  # the calibration's pre-registered sets (waveflow/vecunit/calib.py)
            from waveflow.vecunit.workload import random_jobs

            spec = CG_JOBS[kind]
            out = random_jobs(self.cfg, spec["random"], seed=spec["seed"], npass=spec["npass"],
                              n_max=spec["n_max"])
            shapes = spec["solves"]
        else:
            raise ValueError(f"no workload {kind!r}")
        for j, (k, n, nit, seed) in enumerate(shapes):
            ar, ai, br, bi = problem(f, k, n, seed)
            if kind == "test" and j == 3:
                br[:, 2] = bi[:, 2] = 0  # a zero column: both guards fire
            out.append(cg_job(self.cfg, f, ar, ai, br, bi, nit, 200 + len(out)))
        return out
