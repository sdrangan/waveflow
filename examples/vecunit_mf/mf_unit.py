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
    """A vector unit for the matched filter: chunks of ``VLMAX`` samples, up to ``nrx_max`` antennas."""

    fx: Format = FX
    fr: Format = FR
    fy: Format = FY
    VLMAX: int = 64
    nrx_max: int = 8
    L: int = 4
    build_fields: ClassVar[tuple[str, ...]] = ("cfg", "fx", "fr", "fy", "VLMAX", "nrx_max", "L")

    def __post_init__(self) -> None:
        if self.cfg is None:
            self.cfg = mf_config(self.fx, self.fr, self.fy, L=self.L, VLMAX=self.VLMAX,
                                 nrx_max=self.nrx_max)
        super().__post_init__()

    def frames(self, kind: str = "test") -> list[tuple[int, int, int]]:
        """``(nsamp, nrx, seed)`` per frame.  ``"test"``: every antenna count shape -- below, at and
        above ``L`` -- with whole and short last chunks.  ``"steady"``: four full frames of four
        chunks at ``nrx_max`` (the input floor's measurement)."""
        V, N = int(self.VLMAX), int(self.nrx_max)
        if kind == "test":
            return [(2 * V, N, 1), (V + 5, max(1, N - 1), 2), (V, min(N, self.L), 3), (7, 1, 4)]
        if kind == "steady":
            return [(4 * V, N, s) for s in range(4)]
        raise ValueError(f"no workload {kind!r}")

    def jobs(self, kind: str = "test") -> list:
        out = []
        for j, (nsamp, nrx, seed) in enumerate(self.frames(kind)):
            rng = np.random.default_rng(seed)
            lo, hi = -(1 << 15), 1 << 15
            xr, xi = rng.integers(lo, hi, nsamp), rng.integers(lo, hi, nsamp)
            rr, ri = rng.integers(lo, hi, (nsamp, nrx)), rng.integers(lo, hi, (nsamp, nrx))
            out.append((300 + j, mf_programs(self.cfg, nsamp, nrx),
                        mf_inputs(self.cfg, xr, xi, rr, ri)))
        return out
