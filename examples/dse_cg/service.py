"""service.py -- the DSE service: the one process that can charge, cache, and answer.

The skill's ``dselib`` and the scripted policies are clients of it; the ledger lives here, outside
the sandbox, so nothing a client does can change what it was charged.  One service instance serves
one run: one design, one arm, one objective, one budget.

The same service serves a user (``python -m examples.dse_cg.service --live``): the Waveflow arm, the
live backend, no objective (the user's prompt has it; ``submit`` then checks the workloads the
report names), and a budget only if the user sets one.

What it does on each call (plan ``plans/dse_cg.md``, *The service and the ledger*):

* **checks the arm** -- ``pysim`` is refused in the traditional arm, here and not only in the client;
* **checks the point** -- an illegal design comes back as a row (``status: "illegal: (R, C) in
  [...]"``);
* **charges the recorded cost** of each evaluation, from the 2025.1 lab-machine timing events stored
  with the replay table: per part for ``synth``; for ``rtlsim``, elaboration once per RTL build and
  simulation per run (``design.sim_identity``: a design parameter that rebuilds nothing, or a new
  workload, is a new run on the same build); the measured run time for ``golden`` and ``pysim``;
* **synthesizes part by part** -- a design point's parts are the nodes of ``design.architecture()``;
  ``design.identities(params)`` names what each would synthesize, and only identities the ledger
  does not hold are charged and built (``design.synth(node, params)`` on the live backend); the
  point's row is the sum of its parts, with each part's numbers;
* **estimates kernel by kernel** -- ``hls_estimate`` C-synthesizes only the HLS kernels whose
  identity (``design.hls_identities(params)``) the ledger does not hold;
* **caches by identity** -- a part, a kernel, or a simulation already done costs 0;
* **schedules jobs on k worker slots** on a simulated tool clock, so both tool-hours and time-to-
  decision are known; ``wait`` advances the clock, the agent's thinking does not;
* **answers from a backend** -- :class:`ReplayBackend` (the ground truth G1: every legal point of
  the design's space, measured before any scored run) for every scored run; :class:`LiveBackend` for
  a user, and for measuring G1 itself;
* **enforces the budget and the submit rule** (synthesized and RTL-simulated on the objective's
  workloads).

* **writes artifacts** -- small derived files (utilization per instance, per-block activity) into
  ``results/<eval id>/`` in the client's working directory, and their paths into the row.

Every call, its charge and its result are appended to the ledger file, which ``score.py`` reads and
``dselib.results()`` returns.  The ledger lives as long as its file: one run in the experiment (each
run starts empty, or it would inherit earlier runs' measurements for free); as long as the project
for a user (a synthesis done yesterday is still in it today).

The live backend and its command line (:func:`main`) are written (plan Phase 3); :class:`ReplayBackend` is
plan step 5.
"""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ARMS = ("traditional", "waveflow")


class Refused(Exception):
    """A call that cannot be made at all: a wrong arm, an exhausted budget, a closed run."""


def flatten(row: dict, prefix: str = "") -> dict:
    """*row* for a data frame: its ``params`` one column each, nested values dotted columns."""
    out: dict = {}
    for k, v in row.items():
        if k == "params" and not prefix and isinstance(v, dict):
            out.update(v)
        elif isinstance(v, dict):
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


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
    share its row; ``kernels`` one csynth row per kernel identity; ``builds`` and ``runs`` the RTL
    builds elaborated and the RTL runs simulated; ``evals`` every other evaluation by its key.
    """

    budget_s: float | None
    path: Path | None
    entries: list[LedgerEntry] = field(default_factory=list)
    parts: dict[str, dict] = field(default_factory=dict)
    kernels: dict[str, dict] = field(default_factory=dict)
    builds: dict[str, dict] = field(default_factory=dict)
    runs: dict[str, dict] = field(default_factory=dict)
    evals: dict[str, dict] = field(default_factory=dict)

    @property
    def spent_s(self) -> float:
        return sum(e.cost_s for e in self.entries)

    def check(self, fn: str) -> None:
        """Refuse a call once the budget is spent."""
        if self.budget_s is not None and self.spent_s >= self.budget_s:
            raise Refused(f"{fn}: the budget of {self.budget_s:.0f} s is spent")

    def charge(self, fn: str, args: dict, cost_s: float, cached: bool, result: Any,
               t_start_s: float, t_end_s: float) -> LedgerEntry:
        e = LedgerEntry(len(self.entries), fn, args, float(cost_s), cached, float(t_start_s),
                        float(t_end_s), result)
        self.entries.append(e)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(e), default=str) + "\n")
        return e

    def summary(self, clock_s: float = 0.0, pool: WorkerPool | None = None) -> dict:
        """The body of ``dselib.budget()``."""
        by: dict[str, float] = {}
        for e in self.entries:
            by[e.fn] = by.get(e.fn, 0.0) + e.cost_s
        spent = self.spent_s
        return {"budget_s": self.budget_s, "spent_s": spent,
                "remaining_s": None if self.budget_s is None else self.budget_s - spent,
                "by_function": by, "tool_clock_s": clock_s,
                "jobs_running": pool.running(clock_s) if pool else 0,
                "jobs_queued": pool.queued(clock_s) if pool else 0}

    def rows(self, fn: str | list[str] | None = None) -> list[dict]:
        """The body of ``dselib.results()``: the returned rows, filtered by function and
        flattened (one column per parameter, dotted columns for nested values)."""
        fns = None if fn is None else ({fn} if isinstance(fn, str) else set(fn))
        out = []
        for e in self.entries:
            if fns is not None and e.fn not in fns:
                continue
            for row in e.result if isinstance(e.result, list) else [e.result]:
                if isinstance(row, dict) and "status" in row:
                    out.append({"fn": e.fn, **flatten(row)})
        return out


class WorkerPool:
    """k slots on a simulated tool clock.  ``submit`` queues a job with a duration at a clock
    time; it starts when a slot is free.  ``finish_time`` is when it ends."""

    def __init__(self, k: int):
        self.k = int(k)
        self.free = [0.0] * self.k
        self.jobs: dict[str, tuple[float, float]] = {}
        self._ids = itertools.count(1)

    def submit(self, duration_s: float, now_s: float) -> str:
        i = min(range(self.k), key=lambda j: self.free[j])
        start = max(now_s, self.free[i])
        end = start + float(duration_s)
        self.free[i] = end
        jid = f"j{next(self._ids):04d}"
        self.jobs[jid] = (start, end)
        return jid

    def finish_time(self, job: str) -> float:
        return self.jobs[job][1]

    def running(self, now_s: float) -> int:
        return sum(1 for s, e in self.jobs.values() if s <= now_s < e)

    def queued(self, now_s: float) -> int:
        return sum(1 for s, _e in self.jobs.values() if s > now_s)


class ReplayBackend:
    """Answers ``golden``, ``pysim``, ``synth`` and ``rtlsim`` from the ground-truth table.

    The table is G1 (plan step 4): for every grid point, the measured resources and cycles, the
    golden accuracy, the pysim predictions, and the recorded cost of each evaluation, all at 2025.1.
    A point outside the table is a row with ``status: "not_in_space"`` (the agents' space is the
    table, so this means a bug in the brief or the client).
    """

    def __init__(self, table_dir: Path):
        self.table_dir = table_dir

    def call(self, what: str, *args) -> tuple[dict, float]:
        """``(result row, recorded cost_s)`` for one evaluation."""
        raise NotImplementedError("step 5")


class LiveBackend:
    """Answers by running the tools on this machine through the design's adaptor: ``golden`` and
    ``pysim`` in process, ``hls_estimate`` (csynth), ``synth`` (a part out of context) and ``rtlsim``
    (the system flow through XSI).  The cost is the time the tools took: each row's own
    ``seconds`` (csynth's ``synth_seconds``, Vivado's, the run's) and, for ``rtlsim``, its build and
    simulation phases.  Measuring G1 (plan step 4) is this backend driven over the grid.
    """

    def __init__(self, design: Any, build_dir: Path | None = None):
        self.design, self.build_dir = design, build_dir

    def call(self, what: str, *args) -> tuple[dict, float]:
        """``(row, cost)``.  A failure of the tools (a build that breaks, a run that times out) is a
        row, ``status: "failed: ..."``, charged the time it took, not an exception: the agent sees
        it as it would see any other result."""
        t0 = time.perf_counter()
        try:
            return self._call(what, *args)
        except Exception as e:  # noqa: BLE001 -- every tool failure is a result
            return ({"status": f"failed: {type(e).__name__}: {str(e)[-800:]}"},
                    time.perf_counter() - t0)

    def _call(self, what: str, *args) -> tuple[dict, float]:
        d = self.design
        t0 = time.perf_counter()
        if what == "golden":
            row = d.golden(*args)
            return row, float(row.get("seconds") or time.perf_counter() - t0)
        if what == "pysim":
            row = d.pysim(*args)
            return row, float(row.get("seconds") or time.perf_counter() - t0)
        if what == "hls_estimate":
            row = d.hls_estimate(*args)
            return row, float(row.get("seconds") or 0.0)
        if what == "synth":
            row = d.synth(*args)
            return row, float(row.get("seconds") or 0.0)
        if what == "rtlsim":
            row = d.rtlsim(*args)
            return row, float(row.get("build_s", 0.0)) + float(row.get("simulate_s", 0.0))
        raise ValueError(f"no evaluation {what!r}")


class Service:
    """One run's service.  Each public method is the server side of the ``dselib`` function of the
    same name, and returns what that function returns."""

    def __init__(self, design: Any, arm: str, objective: dict | None, budget_s: float | None,
                 k_workers: int, backend: ReplayBackend | LiveBackend, ledger_path: Path | None):
        if arm not in ARMS:
            raise ValueError(f"arm must be one of {ARMS}")
        self.design, self.arm, self.objective = design, arm, objective
        self.ledger = Ledger(budget_s, ledger_path)
        self.pool = WorkerPool(k_workers)
        self.backend = backend
        self.clock_s = 0.0
        self.pending: dict[str, list[dict]] = {}
        self.closed = False

    # -- the calls ------------------------------------------------------------------------------

    def _open(self, fn: str) -> None:
        if self.closed:
            raise Refused(f"{fn}: the run is closed (submitted)")
        self.ledger.check(fn)

    def _point(self, params: dict) -> tuple[dict | None, str | None]:
        """``(filled params, None)``, or ``(None, the broken rule)``; an unknown name raises."""
        p = self.design.fill(params)
        rule = self.design.legal(p)
        return (None, rule) if rule else (p, None)

    def space(self) -> dict:
        sp = self.design.space()
        rb = self.design.rebuilds()
        for p in sp["parameters"]:
            if p["kind"] == "design":
                p["rebuilds"] = rb.get(p["name"], [])
        return sp

    def _now(self, fn: str, args: dict, key: str, what: str, call_args: tuple, p: dict) -> dict:
        """An evaluation answered at once (``golden``, ``pysim``): cached by *key*, else run and
        charged, the tool clock moved by its cost."""
        if key in self.ledger.evals:
            row = {**self.ledger.evals[key], "cost_s": 0.0, "cached": True}
            self.ledger.charge(fn, args, 0.0, True, row, self.clock_s, self.clock_s)
            return row
        row, cost = self.backend.call(what, *call_args)
        row = {**row, "params": p, "cost_s": cost, "cached": False}
        if row.get("status") == "ok":
            self.ledger.evals[key] = row
        t0 = self.clock_s
        self.clock_s += cost
        self.ledger.charge(fn, args, cost, False, row, t0, self.clock_s)
        return row

    def golden(self, params: dict, workload: Any = None) -> dict:
        self._open("golden")
        p, rule = self._point(params)
        args = {"params": params, "workload": workload}
        if rule:
            row = {"params": params, "status": f"illegal: {rule}", "cost_s": 0.0, "cached": False}
            self.ledger.charge("golden", args, 0.0, False, row, self.clock_s, self.clock_s)
            return row
        name, w = self.design.workload("accuracy" if workload is None else workload)
        key = "golden|" + json.dumps([p, name, w], sort_keys=True)
        return self._now("golden", args, key, "golden", (p, {"name": name, **w}), p)

    def pysim(self, params: dict, workload: Any = None) -> dict:
        if self.arm != "waveflow":
            raise Refused("pysim: not available in the traditional arm")
        self._open("pysim")
        p, rule = self._point(params)
        args = {"params": params, "workload": workload}
        if rule:
            row = {"params": params, "status": f"illegal: {rule}", "cost_s": 0.0, "cached": False}
            self.ledger.charge("pysim", args, 0.0, False, row, self.clock_s, self.clock_s)
            return row
        name, w = self.design.workload(workload)
        key = "pysim|" + json.dumps([p, name, w], sort_keys=True)
        return self._now("pysim", args, key, "pysim", (p, {"name": name, **w}), p)

    def _job(self, fn: str, args: dict, rows: list[dict], cost: float) -> str:
        """Queue one job of *cost* tool-seconds that answers *rows*; charge it."""
        jid = self.pool.submit(cost, self.clock_s)
        start, end = self.pool.jobs[jid]
        self.pending[jid] = rows
        self.ledger.charge(fn, args, cost, cost == 0.0, rows, start, end)
        return jid

    def _kernel(self, kernel: str, ident: str, p: dict) -> tuple[dict, float]:
        """A kernel's csynth row: from the ledger (cost 0), else run and recorded."""
        if ident in self.ledger.kernels:
            return {**self.ledger.kernels[ident], "cached": True}, 0.0
        row, cost = self.backend.call("hls_estimate", kernel, p)
        if row.get("status") == "ok":
            self.ledger.kernels[ident] = {**row, "cost_s": cost}
        return {**row, "cached": False}, cost

    def hls_estimate(self, points: list[dict]) -> str:
        self._open("hls_estimate")
        rows, total = [], 0.0
        for params in points:
            p, rule = self._point(params)
            if rule:
                rows.append({"params": params, "status": f"illegal: {rule}", "cost_s": 0.0})
                continue
            kernels, cost = {}, 0.0
            for kernel, ident in self.design.hls_identities(p).items():
                kernels[kernel], c = self._kernel(kernel, ident, p)
                cost += c
            ok = all(k.get("status") == "ok" for k in kernels.values())
            rows.append({"params": p, "status": "ok" if ok else "failed", "kernels": kernels,
                         "cost_s": cost, "cached": cost == 0.0})
            total += cost
        return self._job("hls_estimate", {"points": points}, rows, total)

    def _synth_point(self, p: dict) -> tuple[dict, float]:
        """A point's synthesis: each part not in the ledger synthesized (its kernel's csynth first,
        when that is new too) and charged; the row sums the parts."""
        ids = self.design.identities(p)
        hls = self.design.hls_identities(p)
        parts, cost = {}, 0.0
        for node, ident in ids.items():
            if ident in self.ledger.parts:
                parts[node] = {**self.ledger.parts[ident], "cached": True}
                continue
            for kernel in getattr(self.design, "node_kernels", lambda n: ())(node):
                _row, c = self._kernel(kernel, hls[kernel], p)
                cost += c
            row, c = self.backend.call("synth", node, p)
            cost += c
            if row.get("status") == "ok":
                self.ledger.parts[ident] = row
            parts[node] = {**row, "cached": False}
        counters = ("lut", "ff", "dsp", "bram", "uram")
        total = {k: sum(int(v.get(k, 0) or 0) for v in parts.values()) for k in counters}
        extra = getattr(self.design, "unbuilt_resources", lambda _p: {})(p)
        for k, v in extra.items():
            total[k] = total.get(k, 0) + int(v)
        status = "ok" if all(v.get("status") == "ok" for v in parts.values()) else "failed"
        return {"params": p, "status": status, **total, "parts": parts, "cost_s": cost,
                "cached": cost == 0.0}, cost

    def synth(self, points: list[dict]) -> str:
        self._open("synth")
        rows, total = [], 0.0
        for params in points:
            p, rule = self._point(params)
            if rule:
                rows.append({"params": params, "status": f"illegal: {rule}", "cost_s": 0.0})
                continue
            row, cost = self._synth_point(p)
            rows.append(row)
            total += cost
        return self._job("synth", {"points": points}, rows, total)

    def rtlsim(self, points: list[dict], workload: Any = None) -> str:
        self._open("rtlsim")
        rows, total = [], 0.0
        for params in points:
            p, rule = self._point(params)
            if rule:
                rows.append({"params": params, "status": f"illegal: {rule}", "cost_s": 0.0})
                continue
            srow, cost = self._synth_point(p)         # synthesized first, as the rules say
            name, w = self.design.workload(workload)
            sid = self.design.sim_identity(p, {"name": name, **w})
            if srow["status"] != "ok":
                rows.append({"params": p, "status": "failed: synthesis", "parts": srow["parts"],
                             "cost_s": cost, "cached": False})
                total += cost
                continue
            if sid["run"] in self.ledger.runs:
                row = {**self.ledger.runs[sid["run"]], "cached": True}
            else:
                row, c = self.backend.call("rtlsim", p, {"name": name, **w})
                if row.get("status") in ("ok", "mismatch"):     # a mismatch is a result too
                    sim = float(row.get("simulate_s", 0.0))
                    build = 0.0 if sid["build"] in self.ledger.builds else float(row.get("build_s", 0.0))
                    self.ledger.builds.setdefault(sid["build"], {"build_s": row.get("build_s")})
                    self.ledger.runs[sid["run"]] = row
                    cost += build + sim
                else:
                    cost += c
                row = {**row, "cached": False}
            rows.append({**row, "params": p, "workload": {"name": name, **w}, "cost_s": cost})
            total += cost
        return self._job("rtlsim", {"points": points, "workload": workload}, rows, total)

    def wait(self, job: Any) -> list[dict]:
        jobs = [job] if isinstance(job, str) else list(job)
        out: list[dict] = []
        for j in jobs:
            self.clock_s = max(self.clock_s, self.pool.finish_time(j))
            out += self.pending[j]
        return out

    def results(self, fn: str | list[str] | None = None) -> list[dict]:
        return self.ledger.rows(fn)

    def budget(self) -> dict:
        return self.ledger.summary(self.clock_s, self.pool)

    def submit(self, params: dict, report: dict) -> dict:
        """Accept the design only if this run synthesized it and RTL-simulated it on the objective's
        workloads (or, with no objective, those the report names); record the report; close."""
        self._open("submit")
        p, rule = self._point(params)
        if rule:
            return {"accepted": False, "reason": f"illegal: {rule}"}
        missing = [n for n, i in self.design.identities(p).items() if i not in self.ledger.parts]
        if missing:
            return {"accepted": False, "reason": f"not synthesized in this run: parts {missing}"}
        wls = [c.get("workload") for c in (self.objective or {}).get("subject_to", [])]
        wls += [e.get("workload") for e in report.get("evidence", []) if not self.objective]
        for w in [w for w in wls if w is not None and self._timing(w)]:
            name, wf = self.design.workload(w)
            if self.design.sim_identity(p, {"name": name, **wf})["run"] not in self.ledger.runs:
                return {"accepted": False, "reason": f"not RTL-simulated on {name} {wf}"}
        self.ledger.charge("submit", {"params": params, "report": report}, 0.0, False,
                           {"params": p, "status": "accepted", "report": report}, self.clock_s,
                           self.clock_s)
        self.closed = True
        return {"accepted": True}

    def _timing(self, w: Any) -> bool:
        """A workload whose metrics come from simulation (``rtlsim``), not ``golden``."""
        name = w if isinstance(w, str) else w.get("name")
        return any(x["name"] == name and any(m.startswith("t_") for m in x.get("valid_for", []))
                   for x in self.design.space()["workloads"])


def serve(service: Service, address: str) -> None:
    """Expose *service* to ``dselib`` at *address* (``host:port``, what ``DSE_SERVICE`` holds): a
    local HTTP server, one ``POST /call`` of ``{"fn", "kwargs"}`` a call, answered with
    ``{"ok": result}`` or ``{"refused": reason}``.  Blocks."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    host, port = address.rsplit(":", 1)
    allowed = {"space", "golden", "pysim", "hls_estimate", "synth", "rtlsim", "wait", "results",
               "budget", "submit"}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                if body["fn"] not in allowed:
                    raise Refused(f"no dselib function {body['fn']!r}")
                out = {"ok": getattr(service, body["fn"])(**body.get("kwargs", {}))}
            except (Refused, KeyError, ValueError) as e:
                out = {"refused": str(e)}
            data = json.dumps(out, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a) -> None:
            pass

    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()


def main(argv: list[str] | None = None) -> None:
    """``python -m examples.dse_cg.service --live``: the service for a user, on this machine's tools.

    The LTBF design (``ltbf_cg_mem.LtbfCgMemAdaptor``), the Waveflow arm unless ``--arm`` says
    otherwise, no objective (the prompt has it), a budget only with ``--budget-h``.  Each session
    starts a new account, appended to ``--ledger``; the tools' own caches (csynth, the parts' netlists,
    the RTL builds) persist across sessions, so a part synthesized yesterday runs no tool today (its
    recorded time is still what the new account charges).  Prints the address to export as
    ``DSE_SERVICE``, then serves until interrupted."""
    import argparse

    ap = argparse.ArgumentParser(prog="python -m examples.dse_cg.service", description=main.__doc__)
    ap.add_argument("--live", action="store_true", help="run the tools on this machine")
    ap.add_argument("--arm", choices=ARMS, default="waveflow")
    ap.add_argument("--address", default="127.0.0.1:8765", help="host:port to serve on")
    ap.add_argument("--workers", type=int, default=2, help="tool slots (the lab machine's two heavy jobs)")
    ap.add_argument("--budget-h", type=float, default=None, help="a budget of tool time, in hours")
    ap.add_argument("--ledger", type=Path, default=Path("dse_ledger.jsonl"))
    args = ap.parse_args(argv)
    if not args.live:
        ap.error("only --live is written: the replay backend is plans/dse_cg.md step 5")
    from examples.dse_cg.ltbf_cg_mem import LtbfCgMemAdaptor

    design = LtbfCgMemAdaptor()
    budget = None if args.budget_h is None else args.budget_h * 3600.0
    service = Service(design, args.arm, None, budget, args.workers, LiveBackend(design), args.ledger)
    print(f"DSE service: {design.name}, the {args.arm} arm, live; ledger {args.ledger}\n"
          f"export DSE_SERVICE={args.address}", flush=True)
    serve(service, args.address)


if __name__ == "__main__":
    main()
