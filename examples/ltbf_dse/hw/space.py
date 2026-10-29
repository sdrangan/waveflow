"""space.py -- the DSE's knob space and the pre-registered build sets (plan step 5.3).

``plans/ltbf_dse/ltbf_dse.md`` §2, AC11, AC13.  A *design* is one system: its algorithm (``t``, the
operand width ``W``, the form, ``r``), its architecture and its scheduling.  Three parts are built,
each for the knobs that change its hardware (:class:`Part`):

* ``sys`` -- the systolic wrapper: N_rx (so ``Pmax``), ``Mmax``, ``c`` (``Kmax = Nmax``), the array
  ``R x C``, ``W`` (its one format triple) and the bus word;
* ``vec`` -- the vector unit's wrapper: N_rx (``VLMAX = Kmax``), ``c``, the lanes ``L`` (8 does not
  build at these formats: plan §14) and ``W``; 64-bit words only (the wrapper has 64-bit endpoints),
  and at most 4096 lane groups a block (``N_rx·ceil(c/L)``, the unit's queue pointer);
* ``ing`` -- the Ingest DMA: N_rx (its antennas), ``c``, ``J`` (tiles a chunk) and the word.

The rest is modeled: Shared Mem as built (a read word two cycles, a write one, one transaction at a
time; its calibration), with a one-word-a-cycle leaf and banks as what-ifs (user, 2026-10-29); the
ring and FIFO depths, the A53's cores and the policy.  ``Mmax`` holds the sector's streams
``r·U_pad`` in one row tile (10 UEs: 16 at ``r`` 1, 32 at ``r`` 2).  Operands use 16-bit lanes, so
``W <= 16``.

Three sets of parts, fixed here before any of them is built, hashed into the plan (§15):

* **fit** -- per kind, one knob at a time around a centre (N_rx 256, ``Mmax`` 16, ``c`` 8, a 4 x 8
  array, ``W`` 16, 64-bit words, 4 lanes, ``J`` 9), plus each array's centre and corners;
* **held-out** -- seeded draws from the valid parts, none in the fit set: 4 ``sys``, 4 ``vec``,
  2 ``ing`` (AC11: >= 8);
* **brute force** -- every part of AC13's sub-grid of designs: per array, at ``Mmax`` 16, ``c`` 8
  and 64-bit words (one crossbar width a system), the systolic array and width, the vector unit's
  lanes and width, the DMA.

:data:`DECISION_RULE` is AC13's rule, registered with them.  How each part is measured (step 5.4):
a fit or held-out part by csynth (its resources) and by its device gate's XSI run (its per-job
cycles: ``hw/devsys.py``'s job set at its parameters); a brute-force part by csynth (its resources
and latency report, the cycles corrected by the fit parts' XSI anchors).

    python -m examples.ltbf_dse.hw.space     # write the sets and the rule (local) and print hashes
"""

from __future__ import annotations

import hashlib
import itertools
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

NRX = (64, 256, 1024)
MMAX = (16, 32)
CS = (8, 16)
ARRAYS = ((2, 4), (4, 4), (4, 8), (8, 8), (8, 16))
WIDTHS = (12, 14, 16)
WORDS = (64, 128)
LANES = (2, 4)
JS = (4, 9)
KINDS = ("sys", "vec", "ing")
PREREG = Path(__file__).resolve().parent / "prereg"


@dataclass(frozen=True, order=True)
class Part:
    """One build: a part's kind and the knobs that change its hardware (``0`` where a knob is not
    the kind's)."""

    kind: str
    n_rx: int
    c: int
    word_bits: int = 64
    Mmax: int = 0
    R: int = 0
    C: int = 0
    W: int = 0
    L: int = 0
    J: int = 0

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def name(self) -> str:
        k = {f: v for f, v in self.as_dict().items() if f != "kind" and v}
        return self.kind + "_" + "_".join(f"{f}{v}" for f, v in k.items())

    def valid(self) -> bool:
        if self.kind not in KINDS or self.n_rx not in NRX or self.c not in CS or self.word_bits not in WORDS:
            return False
        if self.kind == "sys":
            return (self.Mmax in MMAX and (self.R, self.C) in ARRAYS and self.W in WIDTHS
                    and self.Mmax % self.R == 0 and self.c % self.C == 0
                    and not (self.L or self.J))  # fmt: skip
        if self.kind == "vec":
            return (self.L in LANES and self.W in WIDTHS and self.word_bits == 64
                    and self.n_rx * -(-self.c // self.L) <= 4096
                    and not (self.Mmax or self.R or self.C or self.J))  # fmt: skip
        return self.J in JS and not (self.Mmax or self.R or self.C or self.W or self.L)


def sys_part(n_rx=256, c=8, word_bits=64, Mmax=16, R=4, C=8, W=16) -> Part:
    return Part("sys", n_rx, c, word_bits, Mmax=Mmax, R=R, C=C, W=W)


def vec_part(n_rx=256, c=8, word_bits=64, L=4, W=16) -> Part:
    return Part("vec", n_rx, c, word_bits, L=L, W=W)


def ing_part(n_rx=256, c=8, word_bits=64, J=9) -> Part:
    return Part("ing", n_rx, c, word_bits, J=J)


def fit_set() -> list[Part]:
    out: set[Part] = set()
    for n in NRX:  # each array's centre
        out |= {sys_part(n), vec_part(n), ing_part(n)}
    # sys: one knob at a time at 256, then corners
    for m in MMAX:
        out.add(sys_part(Mmax=m))
    for c in CS:
        out.add(sys_part(c=c, C=min(8, c)))
    for r, cc in ARRAYS:
        out.add(sys_part(R=r, C=cc, c=max(8, cc)))
    for w in WIDTHS:
        out.add(sys_part(W=w))
    for wb in WORDS:
        out.add(sys_part(word_bits=wb))
    out |= {sys_part(1024, c=16, word_bits=128, Mmax=32, R=8, C=16, W=12),
            sys_part(64, c=16, word_bits=128, Mmax=32, R=8, C=8, W=12),
            sys_part(1024, word_bits=128, R=2, C=4, W=14)}  # fmt: skip
    # vec
    for c in CS:
        out.add(vec_part(c=c))
    for lanes in LANES:
        out.add(vec_part(L=lanes))
    for w in WIDTHS:
        out.add(vec_part(W=w))
    out |= {vec_part(1024, L=2, W=12), vec_part(64, c=16, L=2, W=14), vec_part(1024, c=16, W=12)}
    # ing
    for c in CS:
        out.add(ing_part(c=c))
    for j in JS:
        out.add(ing_part(J=j))
    for wb in WORDS:
        out.add(ing_part(word_bits=wb))
    out |= {ing_part(1024, c=16, word_bits=128, J=4), ing_part(64, c=16, word_bits=128, J=4)}
    assert all(p.valid() for p in out), [p for p in out if not p.valid()]
    return sorted(out)


def all_valid() -> list[Part]:
    out = []
    for n, c, wb in itertools.product(NRX, CS, WORDS):
        for m, (r, cc), w in itertools.product(MMAX, ARRAYS, WIDTHS):
            out.append(Part("sys", n, c, wb, Mmax=m, R=r, C=cc, W=w))
        for lanes, w in itertools.product(LANES, WIDTHS):
            out.append(Part("vec", n, c, wb, L=lanes, W=w))
        for j in JS:
            out.append(Part("ing", n, c, wb, J=j))
    return [p for p in out if p.valid()]


def heldout_set(seed: int = 529) -> list[Part]:
    """Seeded draws from the valid parts, none in the fit set: 4 ``sys``, 4 ``vec``, 2 ``ing``."""
    fit = set(fit_set())
    rng = np.random.default_rng(seed)
    out: list[Part] = []
    for kind, count in (("sys", 4), ("vec", 4), ("ing", 2)):
        pool = [p for p in all_valid() if p.kind == kind and p not in fit]
        out += [pool[i] for i in sorted(rng.permutation(len(pool))[:count])]
    return sorted(out)


def brute_set() -> list[Part]:
    """AC13's designs' parts: per array, at ``Mmax`` 16, ``c`` 8 and 64-bit words, the systolic
    array and width, the vector unit's lanes and width, the DMA."""
    out: set[Part] = set()
    for n in NRX:
        for (r, cc), w in itertools.product(ARRAYS[:4], WIDTHS):
            out.add(sys_part(n, R=r, C=cc, W=w))
        for lanes, w in itertools.product(LANES, WIDTHS):
            out.add(vec_part(n, L=lanes, W=w))
        out.add(ing_part(n))
    assert all(p.valid() for p in out)
    return sorted(out)


#: AC13's decision rule, registered with the sets.  A *design* is one brute-force part of each kind at
#: an array (sharing ``W`` and the word) and a runtime point; a *decision* is (array, target, loss
#: budget, time budget, cost measure).  The model's pick is the least predicted cost among the designs
#: predicted to meet both budgets (loss from the accuracy table; time the job-level simulator's, every
#: job's cycles from the fitted models).  A design's *measured* cost is its parts' csynth resources;
#: its *measured* time the simulator's with every job's cycles from its own builds (csynth latency
#: corrected by the XSI anchors).  The pick is right when its measured time <= 1.02 x the budget and
#: its measured cost <= 1.10 x the least measured cost among the designs measured to meet both ("none
#: feasible" on both sides is right; on one side only, wrong).  >= 90 % right per cost measure.
DECISION_RULE = {
    "arrays": list(NRX),
    "targets": ["-6_14", "-6_3"],
    "loss_budgets_db": [0.1, 0.25, 0.5],
    "loss_statistic": "median",
    "time": "the long-term update of the three sectors, back to back (T_LT)",
    "time_budgets_ms": [1.0, 5.0, 10.0],
    "cost_measures": ["DSP", "LUT", "FF", "BRAM_18K", "max"],
    "runtime_axes": {"t": list(range(1, 11)), "form": ["factored", "explicit"], "r": [1]},
    "design": "one brute-force part of each kind at the array, sharing W, 64-bit words",
    "time_tolerance": 1.02,
    "cost_tolerance": 1.10,
    "pass_fraction": 0.90,
}


def write_prereg() -> dict[str, str]:
    """Write the three sets and the rule as JSON under ``hw/prereg/`` (local) and return their
    SHA-256."""
    PREREG.mkdir(parents=True, exist_ok=True)
    bodies = {role: json.dumps([p.as_dict() for p in ps], indent=0, sort_keys=True)
              for role, ps in (("fit", fit_set()), ("heldout", heldout_set()),
                               ("brute", brute_set()))}  # fmt: skip
    bodies["rule"] = json.dumps(DECISION_RULE, indent=0, sort_keys=True)
    hashes = {}
    for role, text in bodies.items():
        (PREREG / f"{role}.json").write_bytes(text.encode())
        hashes[role] = hashlib.sha256(text.encode()).hexdigest()
    return hashes


def load(role: str) -> list[Part]:
    return [Part(**d) for d in json.loads((PREREG / f"{role}.json").read_text())]


if __name__ == "__main__":
    h = write_prereg()
    print(f"rule     sha256 {h['rule']}")
    for role in ("fit", "heldout", "brute"):
        ps = load(role)
        by = {k: sum(p.kind == k for p in ps) for k in KINDS}
        print(f"{role:8s} {len(ps):4d} parts {by}  sha256 {h[role]}")
    sys.exit(0)
