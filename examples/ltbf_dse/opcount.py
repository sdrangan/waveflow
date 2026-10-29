"""opcount.py -- arithmetic that logs its own cost, for the Python stages' complexity.

``plans/ltbf_dse/ltbf_dse.md`` step 2.2 (spec ``plans/ltbf_dse/ltbf_dse_spec.md`` §2).  The channel
estimation runs in Python (user, 2026-10-28) but is written so its complexity can be read off later:
every operation goes through an :class:`OpLog`, which computes it with numpy and records

    (stage, op, shapes, cmacs, divides, sqrts, bytes_in, bytes_out)

``cmacs`` are complex multiply-accumulates (one complex multiply and one complex add each), the unit
the paper's Table "complexity" counts in; a real operand still counts one.  ``bytes`` are of the
memory elements the stage reads and writes (4 bytes a complex value: 16-bit real and imaginary, the
Shared Mem format), so a stage's traffic can be priced on the bus model too.  Products are logged with
their shapes, so a stage can later be priced on the systolic engine's models as well.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

#: Bytes a complex memory element (16-bit real + 16-bit imaginary).
ELEM_BYTES = 4


@dataclass(frozen=True)
class Op:
    stage: str
    op: str
    shapes: tuple
    cmacs: int = 0
    divides: int = 0
    sqrts: int = 0
    bytes_in: int = 0
    bytes_out: int = 0


@dataclass
class OpLog:
    """The operations of one run; ``stage`` names what is logged until it is changed."""

    stage: str = ""
    ops: list[Op] = field(default_factory=list)

    def _log(
        self,
        op: str,
        shapes: tuple,
        out: np.ndarray,
        *,
        cmacs=0,
        divides=0,
        sqrts=0,
        n_in=0,
    ) -> np.ndarray:
        self.ops.append(
            Op(
                self.stage,
                op,
                shapes,
                int(cmacs),
                int(divides),
                int(sqrts),
                int(n_in) * ELEM_BYTES,
                int(np.size(out)) * ELEM_BYTES,
            )
        )
        return out

    # -- operations ---------------------------------------------------------------------------------

    def matmul(
        self, a: np.ndarray, b: np.ndarray, *, conj_a: bool = False
    ) -> np.ndarray:
        """``a @ b`` (``a.conj().T @ b`` with *conj_a*): ``m * k * n`` cmacs."""
        a2 = a.conj().T if conj_a else a
        out = a2 @ b
        m, k = a2.shape
        n = b.shape[1] if b.ndim == 2 else 1
        return self._log(
            "matmul_ah" if conj_a else "matmul",
            (a.shape, b.shape),
            out,
            cmacs=m * k * n,
            n_in=a.size + b.size,
        )

    def mul(self, a: np.ndarray, b: np.ndarray, *, conj_b: bool = False) -> np.ndarray:
        """Elementwise ``a * b`` (``a * conj(b)``), broadcast: one cmac an output element."""
        out = a * (np.conj(b) if conj_b else b)
        return self._log(
            "mul",
            (np.shape(a), np.shape(b)),
            out,
            cmacs=np.size(out),
            n_in=np.size(a) + np.size(b),
        )

    def sumsq(self, a: np.ndarray, axis: int) -> np.ndarray:
        """``sum |a|^2`` along *axis*: one cmac an input element."""
        out = np.sum(np.abs(a) ** 2, axis=axis)
        return self._log("sumsq", (a.shape, axis), out, cmacs=a.size, n_in=a.size)

    def add(self, a: np.ndarray, b) -> np.ndarray:
        """``a + b``: free in cmacs (an add is inside a cmac); logged for its traffic."""
        out = a + b
        return self._log(
            "add", (np.shape(a), np.shape(b)), out, n_in=np.size(a) + np.size(b)
        )

    def divide(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """``a / b`` with *b* real: one divide an output element (a complex numerator's two parts by
        one reciprocal)."""
        out = a / b
        return self._log(
            "divide",
            (np.shape(a), np.shape(b)),
            out,
            divides=np.size(b),
            cmacs=np.size(out),
            n_in=np.size(a) + np.size(b),
        )

    def sqrt(self, a: np.ndarray) -> np.ndarray:
        out = np.sqrt(a)
        return self._log("sqrt", (np.shape(a),), out, sqrts=np.size(a), n_in=np.size(a))

    def copy(self, a: np.ndarray, *, what: str = "") -> np.ndarray:
        """A rearrangement or copy (a layout for an engine): traffic only."""
        out = np.array(a, copy=True)
        return self._log(
            f"copy:{what}" if what else "copy", (np.shape(a),), out, n_in=np.size(a)
        )

    # -- reading the log ----------------------------------------------------------------------------

    def totals(self, stage: str | None = None) -> dict:
        """Sums of every counter, for one stage or all."""
        t = defaultdict(int)
        for o in self.ops:
            if stage is None or o.stage == stage:
                for k in ("cmacs", "divides", "sqrts", "bytes_in", "bytes_out"):
                    t[k] += getattr(o, k)
        return dict(t)

    def by_stage(self) -> dict:
        return {s: self.totals(s) for s in dict.fromkeys(o.stage for o in self.ops)}
