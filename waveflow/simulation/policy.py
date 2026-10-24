"""policy.py — hostile testbench policies for a harness's stand-ins (``plans/unit_harness.md`` Stage 5).

A harness's stand-ins are polite by default: a driver presents a word every cycle it can, a sink is
always ready, credit comes back at once, a master takes its answers the cycle they arrive.  A design
that is only ever run that way has not been shown to survive anything else.  A
:class:`StandInPolicy` makes them hostile, each in the way its protocol allows:

* ``stall_pct`` -- a stream sink drops TREADY, a stream driver holds off its next word (never
  withdrawing one it presented: AXI-Stream allows nothing else), a credit source likewise;
* ``answer_pct`` -- a scripted AXI-MM master drops RREADY / BREADY: it takes its answers late;
* ``credit_delay`` -- a credit sink holds each credit report that many cycles after it is due (starved
  credit).

At RTL the stall is cycle by cycle: :func:`stalled` -- a seeded hash of the cycle number, the same
function as the C++ ``wfbfm::policy_stalled``.  pysim is the functional reference, so its stand-ins only
approximate the policy (a delay per burst of the cycles the policy would have stalled on average); what
a hostile run checks is that the RTL still finishes, with the same words.
"""
from __future__ import annotations

from dataclasses import dataclass


def stalled(seed: int, cycle: int, pct: int) -> bool:
    """Is *cycle* one a stand-in with this policy stalls on?  *pct* percent of cycles; ``pct <= 0``:
    never.  The C++ ``wfbfm::policy_stalled``, bit for bit."""
    if pct <= 0:
        return False
    m = 0xFFFFFFFF
    x = ((seed * 2654435761) & m) ^ ((cycle * 2246822519) & m)
    x ^= x >> 15
    x = (x * 2654435761) & m
    x ^= x >> 13
    x = (x * 3266489917) & m
    x ^= x >> 16
    return x % 100 < pct


def stall_cycles(nwords: int, pct: int) -> int:
    """The cycles a *pct* stall adds, on average, to *nwords* beats: pysim's per-burst approximation."""
    if pct <= 0:
        return 0
    return -(-int(nwords) * int(pct) // (100 - int(pct)))


@dataclass(frozen=True)
class StandInPolicy:
    """How a harness's stand-ins misbehave (see the module docstring); ``seed`` picks the stall
    pattern."""

    name: str
    stall_pct: int = 0
    answer_pct: int = 0
    credit_delay: int = 0
    seed: int = 1

    def __post_init__(self) -> None:
        for f in ("stall_pct", "answer_pct"):
            if not 0 <= getattr(self, f) < 100:
                raise ValueError(f"policy {self.name!r}: {f} must be in [0, 100)")
        if self.credit_delay < 0:
            raise ValueError(f"policy {self.name!r}: credit_delay must be >= 0")

    def apply(self, stand_in) -> None:
        """Set this policy's knobs on *stand_in* -- each one it has (a ``DynParam`` it carries).  A
        stand-in whose stalls are answers (``policy_role = "answers"``: a bus master) takes
        ``answer_pct``; any other, ``stall_pct``."""
        answers = getattr(type(stand_in), "policy_role", "") == "answers"
        for knob, value in (("stall_pct", self.answer_pct if answers else self.stall_pct),
                            ("stall_seed", self.seed), ("credit_delay", self.credit_delay)):
            if hasattr(stand_in, knob):
                setattr(stand_in, knob, int(value))

    def touches(self, stand_ins) -> bool:
        """Does this policy change anything about *stand_ins*?  A sweep skips a harness it does not."""
        for s in stand_ins:
            answers = getattr(type(s), "policy_role", "") == "answers"
            pct = self.answer_pct if answers else self.stall_pct
            if (hasattr(s, "stall_pct") and pct) or (hasattr(s, "credit_delay") and self.credit_delay):
                return True
        return False

    def slowdown(self) -> float:
        """How much longer a run may take under this policy: what a harness scales its bound by."""
        return 100.0 / (100 - max(self.stall_pct, self.answer_pct))

    def bound(self, n_cycles: int) -> int:
        """The RTL run's bound under this policy, for a polite bound of *n_cycles*: room for every
        stall and for a credit report held on each of up to 20 window refills."""
        return int(n_cycles * self.slowdown() * 1.5) + 20 * self.credit_delay + 1000


#: The polite testbench: every existing gate's.
POLITE = StandInPolicy("polite")
#: The sweep every harness runs (Stage 5).  Hostile enough to bite: at 50% a sink never back-pressured
#: markov's generator (a word every four cycles), and credit held 300 cycles never emptied its window
#: (measured at RTL, 2026-10-15) -- a policy that changes nothing finds nothing.
POLICIES = (
    StandInPolicy("stall", stall_pct=90, seed=7),
    StandInPolicy("starved_credit", credit_delay=2000, seed=3),
    StandInPolicy("late_answers", answer_pct=90, seed=11),
)

__all__ = ["POLICIES", "POLITE", "StandInPolicy", "stall_cycles", "stalled"]
