"""service.py -- the DSE service: the one process that can charge, cache, and answer.

The skill's ``dselib`` and the scripted policies are clients of it; the ledger lives here, outside
the sandbox, so nothing a client does can change what it was charged.  One service instance serves
one run: one design, one arm, one objective, one budget.

The same service serves a user (``python -m examples.dse_cg.service --live``): the Waveflow arm, the
live backend, no objective (the user's prompt has it; ``submit`` then checks the workloads the
report names), and a budget only if the user sets one.

What it does on each call (plan ``plans/dse_cg.md``, *The service and the ledger*):

* **checks the arm** -- ``pysim`` is refused in the traditional arm, here and not only in the client;
* **checks the point** -- an illegal design comes back as a row (``status: "illegal: n > 16"``);
* **charges the recorded cost** of each evaluation, from the 2025.1 lab-machine timing events stored
  with the replay table: per build for ``synth``, elaboration + simulation for a design's first
  ``rtlsim``, simulation alone after that, the measured run time for ``golden`` and ``pysim``;
* **synthesizes part by part** -- a design point's parts are the nodes of ``design.architecture()``;
  ``design.identities(params)`` names what each would synthesize, and only identities the ledger
  does not hold are charged and built (``design.synth(node, params)`` on the live backend); the
  point's row is the sum of its parts, with each part's numbers;
* **estimates kernel by kernel** -- ``hls_estimate`` C-synthesizes only the HLS kernels whose
  identity (``design.hls_identities(params)``) the ledger does not hold;
* **caches by identity** -- a part, a kernel, or a simulation already done costs 0;
* **schedules jobs on k worker slots** on a simulated tool clock, so both tool-hours and time-to-
  decision are known; ``wait`` advances the clock, the agent's thinking does not;
* **answers from a backend** -- :class:`ReplayBackend` (the pre-registered ground-truth grid, G1 for
  V1) for every scored run; :class:`LiveBackend` for a user, and for measuring G1 itself;
* **enforces the budget and the submit rule** (synthesized and RTL-simulated on the objective's
  workloads).

* **writes artifacts** -- small derived files (utilization per instance, per-block activity) into
  ``results/<eval id>/`` in the client's working directory, and their paths into the row.

Every call, its charge and its result are appended to the ledger file, which ``score.py`` reads and
``dselib.results()`` returns.  The ledger lives as long as its file: one run in the experiment (each
run starts empty, or it would inherit earlier runs' measurements for free); as long as the project
for a user (a synthesis done yesterday is still in it today).

Skeleton: interfaces only (plan Phase 3 on the live backend; replay is step 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ARMS = ("traditional", "waveflow")


@dataclass
class LedgerEntry:
    """One call: what was asked, what it cost, when it ran on the tool clock, what came back."""

    seq: int
    fn: str
    args: dict
    cost_s: float
    cached: bool
    t_start_s: float           # tool clock
    t_end_s: float
    result: Any


@dataclass
class Ledger:
    """The run's account: appends every call, totals the spend, refuses past the budget.

    ``parts`` holds one synthesis row per part identity, so two design points that share a part
    share its row; ``entries`` holds the calls, each pointing at the part rows it used.
    """

    budget_s: float | None
    path: Path
    entries: list[LedgerEntry] = field(default_factory=list)
    parts: dict[str, dict] = field(default_factory=dict)

    def charge(self, fn: str, args: dict, cost_s: float, cached: bool, result: Any,
               t_start_s: float, t_end_s: float) -> LedgerEntry:
        raise NotImplementedError("Phase 3")

    def summary(self) -> dict:
        """The body of ``dselib.budget()``."""
        raise NotImplementedError("Phase 3")

    def rows(self, fn: str | list[str] | None = None) -> list[dict]:
        """The body of ``dselib.results()``: the returned rows, filtered by function and
        flattened (one column per parameter, dotted columns for nested values)."""
        raise NotImplementedError("Phase 3")


class WorkerPool:
    """k slots on a simulated tool clock.  ``submit`` queues a job with a duration; ``wait``
    returns the clock time it finishes, given everything queued before it."""

    def __init__(self, k: int):
        self.k = k

    def submit(self, duration_s: float) -> str:
        raise NotImplementedError("Phase 3")

    def finish_time(self, job: str) -> float:
        raise NotImplementedError("Phase 3")


class ReplayBackend:
    """Answers ``golden``, ``pysim``, ``synth`` and ``rtlsim`` from the ground-truth table.

    The table is G1 (plan step 4): for every grid point, the measured resources and cycles, the
    golden accuracy, the pysim predictions, and the recorded cost of each evaluation, all at 2025.1.
    A point outside the table is a row with ``status: "not_in_space"`` (the agents' space is the
    table, so this means a bug in the brief or the client).
    """

    def __init__(self, table_dir: Path):
        self.table_dir = table_dir

    def lookup(self, fn: str, params: dict, workload: dict | None) -> tuple[dict, float]:
        """``(result row, recorded cost_s)`` for one evaluation."""
        raise NotImplementedError("step 5")


class LiveBackend:
    """Answers by running the tools: ``design.golden`` and ``design.pysim`` in process, ``synth``
    through ``system_synth`` and ``rtlsim`` through the incremental XSI flow, on this machine.  The
    cost is the time the tools took.

    Measuring G1 (plan step 4) is this backend driven over the pre-registered grid, with every
    row and cost written to the replay table.
    """

    def __init__(self, design: Any, build_dir: Path):
        self.design, self.build_dir = design, build_dir

    def lookup(self, fn: str, params: dict, workload: dict | None) -> tuple[dict, float]:
        """``(result row, measured cost_s)`` for one evaluation."""
        raise NotImplementedError("Phase 3")


class Service:
    """One run's service.  Each public method is the server side of the ``dselib`` function of the
    same name, and returns what that function returns."""

    def __init__(self, design: Any, arm: str, objective: dict | None, budget_s: float | None,
                 k_workers: int, backend: ReplayBackend | LiveBackend, ledger_path: Path):
        if arm not in ARMS:
            raise ValueError(f"arm must be one of {ARMS}")
        self.design, self.arm, self.objective = design, arm, objective
        self.ledger = Ledger(budget_s, ledger_path)
        self.pool = WorkerPool(k_workers)
        self.backend = backend

    def space(self) -> dict:
        raise NotImplementedError("Phase 3")

    def golden(self, params: dict, workload: Any) -> dict:
        raise NotImplementedError("Phase 3")

    def pysim(self, params: dict, workload: Any) -> dict:
        raise NotImplementedError("Phase 3: refuse unless arm == 'waveflow'")

    def hls_estimate(self, points: list[dict]) -> str:
        raise NotImplementedError("Phase 3: one job per kernel identity in design.hls_identities(params) "
                                  "the ledger does not hold; both arms")

    def synth(self, points: list[dict]) -> str:
        raise NotImplementedError("Phase 3: one job per part identity in design.identities(params) "
                                  "the ledger does not hold; the row sums the parts")

    def rtlsim(self, points: list[dict], workload: Any) -> str:
        raise NotImplementedError("Phase 3: synth first if needed; first workload pays elaboration")

    def wait(self, job: Any) -> list[dict]:
        raise NotImplementedError("Phase 3")

    def results(self, fn: str | list[str] | None = None) -> list[dict]:
        return self.ledger.rows(fn)

    def budget(self) -> dict:
        return self.ledger.summary()

    def submit(self, params: dict, report: dict) -> dict:
        raise NotImplementedError("Phase 3: refuse unless synthesized and rtlsim'd on the objective's "
                                  "workloads; record the report; close the run")


def serve(service: Service, address: str) -> None:
    """Expose ``service`` to ``dselib`` at ``address`` (the ``DSE_SERVICE`` the client reads).
    A local HTTP or socket server; the choice is open."""
    raise NotImplementedError("Phase 3")
