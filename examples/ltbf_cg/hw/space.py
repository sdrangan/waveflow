"""space.py -- the engine's knob space and the pre-registered build sets.

``plans/mimo_cg/ltbf_cg_dse.md`` steps 4.4-4.5.  A build is one :class:`~examples.ltbf_cg.hw.engine.
LtbfEngine` configuration.  Built knobs: the form, the sizes ``nmax`` (N_rx: 64, 256, 1024 for the
8x8, 16x16, 32x32 arrays), ``cmax`` (8 x connected UEs: 240 for 3 sectors x 10 UEs; the N_UE sweep
needs 96 to 960) and ``mmax`` (8: per-UE grouping; 80: a joint solve of 10 own UEs; regime 2's
N_rx columns run as column blocks of ``mmax``), the tile ``R x C`` and the word width ``W``.
Modeled, not built: ``units`` (engines side by side, one per sector at most) and ``L`` (complex
values per I/O beat; the built engine moves one per 64-bit beat).

Three sets, fixed here before any of them is built and hashed into the plan (§15):

* **fit** -- one knob at a time around a factored and an explicit centre, plus corners, spanning
  every size;
* **held-out** -- seeded random draws from the valid space, none in the fit set, 4 factored per
  array and one explicit per built size (AC7: >= 8);
* **brute force** -- the sub-grid the DSE's decisions are checked on (AC9): per array, both forms
  (explicit up to 256), square tiles 1-16 plus two oblong ones, five widths, both groupings.

    python -m examples.ltbf_cg.hw.space            # write the three sets (local) and print hashes
"""

from __future__ import annotations

import hashlib
import itertools
import json
import sys
from dataclasses import asdict, dataclass

import numpy as np

from examples.ltbf_cg.hw import build as hb

NMAX = (64, 256, 1024)
CMAX = (96, 240, 480, 960)
MMAX = (8, 16, 32, 80)
TILES = (1, 2, 4, 8, 16)
WIDTHS = (10, 12, 14, 16, 18, 20, 24)
#: The explicit engine keeps Q (N_rx x N_rx) on chip: built up to 256 (1024 is modeled, via DDR).
EXPLICIT_NMAX = 256
PREREG = hb.BUILD.parent / "prereg"


@dataclass(frozen=True, order=True)
class Knobs:
    explicit: bool
    nmax: int
    cmax: int
    mmax: int
    R: int
    C: int
    W: int

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def name(self) -> str:
        return hb.name(self.as_dict())

    def valid(self) -> bool:
        k = self
        if k.explicit and (k.R != k.C or k.nmax > EXPLICIT_NMAX):
            return False
        return not (k.nmax % k.R or k.cmax % k.R or k.mmax % k.C or k.nmax % k.C)


def fit_set() -> list[Knobs]:
    out: set[Knobs] = set()
    fc = Knobs(False, 256, 240, 80, 4, 4, 16)
    ec = Knobs(True, 256, 240, 80, 4, 4, 16)
    for centre in (fc, ec):
        out.add(centre)
        for t in TILES:
            out.add(Knobs(centre.explicit, 256, 240, 80, t, t, 16))
        for w in (10, 12, 14, 20, 24):
            out.add(Knobs(centre.explicit, 256, 240, 80, 4, 4, w))
        for m in (8, 16, 32):
            out.add(Knobs(centre.explicit, 256, 240, m, 4, min(4, m), 16))
        for c in (96, 480, 960):
            out.add(Knobs(centre.explicit, 256, c, 80, 4, 4, 16))
        out.add(Knobs(centre.explicit, 64, 240, 80, 4, 4, 16))
    for r, c in ((1, 4), (4, 1), (2, 8), (8, 2), (16, 4), (4, 16)):
        out.add(Knobs(False, 256, 240, 80, r, c, 16))
    for t in (1, 4, 16):
        for w in (10, 24):
            out.add(Knobs(False, 1024, 240, 80, t, t, w))
            out.add(Knobs(False, 64, 240, 8, min(t, 8), min(t, 8), w))
    out.add(Knobs(False, 1024, 960, 80, 8, 8, 16))
    out.add(Knobs(False, 1024, 240, 8, 8, 8, 16))
    out.add(Knobs(True, 64, 240, 8, 8, 8, 24))
    out.add(Knobs(True, 64, 96, 16, 1, 1, 10))
    assert all(k.valid() for k in out), [k for k in out if not k.valid()]
    return sorted(out)


def all_valid() -> list[Knobs]:
    return [
        k
        for e, n, c, m, r, cc, w in itertools.product(
            (False, True), NMAX, CMAX, MMAX, TILES, TILES, WIDTHS
        )
        if (k := Knobs(e, n, c, m, r, cc, w)).valid()
    ]


def heldout_set(seed: int = 45) -> list[Knobs]:
    """Seeded draws from the valid space, none in the fit set: 4 factored per array and one
    explicit at each size it is built at (14)."""
    fit = set(fit_set())
    pool = [k for k in all_valid() if k not in fit]
    rng = np.random.default_rng(seed)
    out: list[Knobs] = []
    for explicit, nmax, count in [(False, n, 4) for n in NMAX] + [
        (True, 64, 1),
        (True, 256, 1),
    ]:
        part = [k for k in pool if k.nmax == nmax and k.explicit == explicit]
        idx = rng.permutation(len(part))[:count]
        out += [part[i] for i in sorted(idx)]
    return sorted(out)


def brute_set() -> list[Knobs]:
    out = set()
    for nmax in NMAX:
        for explicit in (False, True):
            tiles = [(t, t) for t in TILES] + ([] if explicit else [(2, 8), (8, 2)])
            for (r, c), w, m in itertools.product(tiles, (10, 12, 16, 20, 24), (8, 80)):
                k = Knobs(explicit, nmax, 240, m, r, min(c, m), w)
                if k.valid():
                    out.add(k)
    return sorted(out)


#: AC9's decision rule, registered with the sets.  A decision is (array, scenario, loss budget,
#: time budget, cost measure) over the brute-force designs: the model's pick is the least
#: predicted cost among designs predicted to meet both budgets; it is right when its measured time
#: <= 1.02 x the budget and its measured cost <= 1.10 x the least measured cost among designs
#: measured to meet both (both "none feasible" is right; one side alone is wrong).  >= 90 % right
#: per cost measure.
DECISION_RULE = {
    "arrays": list(NMAX),
    "scenarios": [
        {"band": "100MHz", "target": "-6_3", "srs": "10dB"},
        {"band": "100MHz", "target": "-6_14", "srs": "10dB"},
    ],
    "loss_budgets_db": [0.1, 0.5, 1.0],
    "loss_statistics": ["median", "p90"],
    "time_budgets_ms": [1.0, 5.0, 10.0],
    "cost_measures": ["DSP", "LUT", "FF", "BRAM_18K", "max"],
    "runtime_axes": {
        "t": list(range(1, 11)),
        "units": [1, 3],
        "regime": [1, 2],
        "r": [1],
    },
    "time_tolerance": 1.02,
    "cost_tolerance": 1.10,
    "pass_fraction": 0.90,
}


def write_prereg() -> dict[str, str]:
    """Write the three sets and the decision rule as JSON under ``hw/prereg/`` (local) and return
    their SHA-256."""
    PREREG.mkdir(parents=True, exist_ok=True)
    hashes = {}
    bodies = {
        role: json.dumps([k.as_dict() for k in ks], indent=0, sort_keys=True)
        for role, ks in (
            ("fit", fit_set()),
            ("heldout", heldout_set()),
            ("brute", brute_set()),
        )
    }
    bodies["rule"] = json.dumps(DECISION_RULE, indent=0, sort_keys=True)
    for role, text in bodies.items():
        (PREREG / f"{role}.json").write_bytes(text.encode())
        hashes[role] = hashlib.sha256(text.encode()).hexdigest()
    return hashes


def load(role: str) -> list[Knobs]:
    return [Knobs(**d) for d in json.loads((PREREG / f"{role}.json").read_text())]


if __name__ == "__main__":
    h = write_prereg()
    print(f"rule     sha256 {h['rule']}")
    for role in ("fit", "heldout", "brute"):
        ks = load(role)
        by = {n: sum(k.nmax == n for k in ks) for n in NMAX}
        print(f"{role:8s} {len(ks):4d} builds {by}  sha256 {h[role]}")
    sys.exit(0)
