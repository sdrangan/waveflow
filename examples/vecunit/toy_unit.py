"""The vector unit configured and fed for the toy kernels: ``ToyVecUnit`` is a
:class:`~waveflow.vecunit.unit.VecUnit` whose scenarios are jobs of :mod:`examples.vecunit.toy`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from examples.vecunit.toy import (
    PORT,
    chunks,
    cmul_inputs,
    cmul_programs,
    double_programs,
    gain_programs,
    poly_programs,
    random_matrix,
    toy_config,
)
from waveflow.vecunit.unit import VecUnit

#: ``poly``'s constants.
POLY = (0.375, -0.5, 0.25)

#: Each workload's jobs, ``(kernel, rows, columns)`` a job.  ``"test"``: every kernel, a chunk that is
#: whole and one that is short, one column and four, ``cmul`` with a result shift (``cmul_rs3``);
#: the others: four jobs of one kernel at full size (``"stream"``: ``cmul`` on 64 rows, four chunks a
#: job).
WORKLOADS = {
    "test": [("double", 16, 4), ("cmul", 40, 3), ("gain", 16, 4), ("double", 5, 1),
             ("poly", 20, 4), ("cmul_rs3", 16, 4)],
    "double": [("double", 16, 4)] * 4,
    "cmul": [("cmul", 16, 4)] * 4,
    "gain": [("gain", 16, 4)] * 4,
    "poly": [("poly", 16, 4)] * 4,
    "stream": [("cmul", 64, 4)] * 4,
}


@dataclass
class ToyVecUnit(VecUnit):
    """The toy unit (:func:`~examples.vecunit.toy.toy_config`), its data ports ``word_bits`` wide;
    ``VLMAX`` and ``fuse_raw_vl`` as :func:`~examples.vecunit.toy.toy_config` takes them."""

    word_bits: int = 64
    VLMAX: int = 16
    fuse_raw_vl: int = 0
    build_fields: ClassVar[tuple[str, ...]] = ("cfg", "word_bits", "VLMAX", "fuse_raw_vl")

    def __post_init__(self) -> None:
        if self.cfg is None:
            self.cfg = toy_config(word_bits=self.word_bits, VLMAX=self.VLMAX,
                                  fuse_raw_vl=self.fuse_raw_vl)
        super().__post_init__()

    def jobs(self, kind: str = "test") -> list:
        """The jobs of :data:`WORKLOADS` ``[kind]``: ``(tx_id, Programs, inputs)``, random inputs in
        ``[-1, 1)``, ``gain``'s ``y`` half ``x`` plus as much again at random.  ``"poly_<rows>"``:
        four ``poly`` jobs of that many rows."""
        spec = WORKLOADS.get(kind)
        if spec is None and kind.startswith("poly_"):
            spec = [("poly", int(kind[5:]), 4)] * 4
        if spec is None:
            raise ValueError(f"no workload {kind!r}: one of {sorted(WORKLOADS)} or poly_<rows>")
        out = []
        for j, (kernel, k, n) in enumerate(spec):
            xr, xi = random_matrix(k, n, seed=2 * j)
            yr, yi = random_matrix(k, n, seed=2 * j + 1, scale=0.5)
            if kernel == "double":
                progs, mats = double_programs(k, n), [(xr, xi)]
            elif kernel.startswith("cmul"):
                rs = int(kernel[len("cmul_rs"):]) if kernel.startswith("cmul_rs") else 0
                progs = cmul_programs(self.cfg, k, n, rs=rs)
                mats = cmul_inputs(self.cfg, xr, xi, yr, yi)
            elif kernel == "poly":
                progs = poly_programs(self.cfg, k, n, *POLY)
                mats = [(xr[r0:r0 + c], xi[r0:r0 + c])
                        for r0, c in zip(range(0, k, self.cfg.VLMAX), chunks(self.cfg, k))]
            else:
                progs, mats = gain_programs(k, n), [(xr, xi), (yr + xr // 2, yi + xi // 2)]
            out.append((100 + j, progs, {PORT: mats}))
        return out
