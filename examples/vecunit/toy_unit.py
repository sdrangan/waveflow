"""The vector unit configured and fed for the toy kernels: ``ToyVecUnit`` is a
:class:`~waveflow.vecunit.unit.VecUnit` whose scenarios are jobs of :mod:`examples.vecunit.toy`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from examples.vecunit.toy import (
    PORT,
    cmul_inputs,
    cmul_programs,
    double_programs,
    gain_programs,
    random_matrix,
    toy_config,
)
from waveflow.vecunit.unit import VecUnit

#: Each workload's jobs, ``(kernel, rows, columns)`` a job.  ``"test"``: every kernel, a chunk that is
#: whole and one that is short, one column and four; the others: four jobs of one kernel at full size
#: (``"stream"``: ``cmul`` on 64 rows, four chunks a job).
WORKLOADS = {
    "test": [("double", 16, 4), ("cmul", 40, 3), ("gain", 16, 4), ("double", 5, 1)],
    "double": [("double", 16, 4)] * 4,
    "cmul": [("cmul", 16, 4)] * 4,
    "gain": [("gain", 16, 4)] * 4,
    "stream": [("cmul", 64, 4)] * 4,
}


@dataclass
class ToyVecUnit(VecUnit):
    """The toy unit (:func:`~examples.vecunit.toy.toy_config`), its data ports ``word_bits`` wide."""

    word_bits: int = 64
    build_fields: ClassVar[tuple[str, ...]] = ("cfg", "word_bits")

    def __post_init__(self) -> None:
        if self.cfg is None:
            self.cfg = toy_config(word_bits=self.word_bits)
        super().__post_init__()

    def jobs(self, kind: str = "test") -> list:
        """The jobs of :data:`WORKLOADS` ``[kind]``: ``(tx_id, Programs, inputs)``, random inputs in
        ``[-1, 1)``, ``gain``'s ``y`` half ``x`` plus as much again at random."""
        if kind not in WORKLOADS:
            raise ValueError(f"no workload {kind!r}: one of {sorted(WORKLOADS)}")
        out = []
        for j, (kernel, k, n) in enumerate(WORKLOADS[kind]):
            xr, xi = random_matrix(k, n, seed=2 * j)
            yr, yi = random_matrix(k, n, seed=2 * j + 1, scale=0.5)
            if kernel == "double":
                progs, mats = double_programs(k, n), [(xr, xi)]
            elif kernel == "cmul":
                progs, mats = cmul_programs(self.cfg, k, n), cmul_inputs(self.cfg, xr, xi, yr, yi)
            else:
                progs, mats = gain_programs(k, n), [(xr, xi), (yr + xr // 2, yi + xi // 2)]
            out.append((100 + j, progs, {PORT: mats}))
        return out
