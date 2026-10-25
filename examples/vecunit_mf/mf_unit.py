"""The vector unit configured and fed for the matched filter: ``MfVecUnit`` is a
:class:`~waveflow.vecunit.unit.VecUnit` whose scenarios are frames (the configuration from three
formats, the jobs from :mod:`examples.vecunit_mf.mf_program`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import numpy as np

from examples.vecunit_mf.mf_program import mf_config, mf_inputs, mf_programs
from waveflow.utils.fixputils import Format, OMode, QMode
from waveflow.vecunit.unit import VecUnit

FX = Format(16, 2, True, QMode.AP_RND, OMode.AP_SAT)
FR = Format(16, 3, True, QMode.AP_RND, OMode.AP_SAT)
FY = Format(16, 3, True, QMode.AP_TRN, OMode.AP_SAT)


@dataclass
class MfVecUnit(VecUnit):
    """A vector unit for the matched filter: chunks of ``VLMAX`` samples, up to ``nrx_max`` antennas,
    its data ports ``word_bits`` wide."""

    fx: Format = FX
    fr: Format = FR
    fy: Format = FY
    VLMAX: int = 64
    nrx_max: int = 8
    L: int = 4
    word_bits: int = 64
    build_fields: ClassVar[tuple[str, ...]] = ("cfg", "fx", "fr", "fy", "VLMAX", "nrx_max", "L",
                                               "word_bits")

    def __post_init__(self) -> None:
        if self.cfg is None:
            self.cfg = mf_config(self.fx, self.fr, self.fy, L=self.L, VLMAX=self.VLMAX,
                                 nrx_max=self.nrx_max, word_bits=self.word_bits)
        super().__post_init__()

    def frames(self, kind: str = "test") -> list[tuple[int, int, int]]:
        """``(nsamp, nrx, seed)`` per frame.  ``"test"``: every antenna count shape -- below, at and
        above ``L`` -- with whole and short last chunks.  ``"steady"``: four full frames of four
        chunks at ``nrx_max`` (the input floor's measurement).  ``"frame_<nsamp>"``: one frame of
        ``nsamp`` samples at ``nrx_max`` (the simulation-speed sizes,
        ``docs/evaluation/sim_speed/vecunit_mf.md``).  ``"steady_nrx<n>"``: the four steady frames at
        ``n`` antennas (the RTL catalogue's workloads, Stage 9c)."""
        V, N = int(self.VLMAX), int(self.nrx_max)
        if kind.startswith("frame_"):
            return [(int(kind[6:]), N, 1)]
        if kind.startswith("steady_nrx"):
            nrx = int(kind[len("steady_nrx"):])
            if not 1 <= nrx <= N:
                raise ValueError(f"no workload {kind!r}: nrx must be 1..{N}")
            return [(4 * V, nrx, s) for s in range(4)]
        if kind == "test":
            return [(2 * V, N, 1), (V + 5, max(1, N - 1), 2), (V, min(N, self.L), 3), (7, 1, 4)]
        if kind == "steady":
            return [(4 * V, N, s) for s in range(4)]
        raise ValueError(f"no workload {kind!r}")

    @staticmethod
    def frame(nsamp: int, nrx: int, seed: int):
        """A frame's ``X`` (``nsamp``) and ``R`` (``nsamp x nrx``): random 16-bit stored integers."""
        rng = np.random.default_rng(seed)
        lo, hi = -(1 << 15), 1 << 15
        xr, xi = rng.integers(lo, hi, nsamp), rng.integers(lo, hi, nsamp)
        rr, ri = rng.integers(lo, hi, (nsamp, nrx)), rng.integers(lo, hi, (nsamp, nrx))
        return xr, xi, rr, ri

    def jobs(self, kind: str = "test") -> list:
        """The frames of :meth:`frames` as jobs; ``"calib"``, ``"holdout2"``: the calibration's
        pre-registered jobs (:data:`waveflow.vecunit.calib.MF_JOBS`) -- random jobs over every
        instruction form on this configuration, then frames."""
        from waveflow.vecunit.calib import MF_JOBS

        if kind in MF_JOBS:
            from waveflow.vecunit.workload import random_jobs

            spec = MF_JOBS[kind]
            out = random_jobs(self.cfg, spec["random"], seed=spec["seed"], npass=spec["npass"],
                              n_max=spec["n_max"], k_max=spec["k_max"])
            for nsamp, nrx, seed in spec["frames"]:
                out.append((300 + len(out), mf_programs(self.cfg, nsamp, nrx),
                            mf_inputs(self.cfg, *self.frame(nsamp, nrx, seed))))
            return out
        out = []
        for j, (nsamp, nrx, seed) in enumerate(self.frames(kind)):
            out.append((300 + j, mf_programs(self.cfg, nsamp, nrx),
                        mf_inputs(self.cfg, *self.frame(nsamp, nrx, seed))))
        return out
