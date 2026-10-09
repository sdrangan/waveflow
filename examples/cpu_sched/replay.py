"""replay.py — the micro-scheduler's trace on gem5 against the processor model (plan step 14).

The example's scheduler ran on the *model*: each operation was priced from its counters by the A53
platform's fitted models.  Here the same operations, in the same order, run on gem5's HPI core
(``sched_replay.c``), each in its own measured region, and the two are compared:

* first the **counters** -- the C replay must see exactly the trace's ``n_tasks`` / ``n_scanned`` /
  ``n_moved``, or it measured a different program;
* then the **cycles** -- per operation (median and max relative error) and in total;
* and the **speed** -- the wall time of pricing the trace through a :class:`Processor` against the
  wall time of the gem5 run.

    python -m examples.cpu_sched.replay --out examples/cpu_sched/replay_result.json
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from waveflow.cpu.calib.gem5 import GEM5_TAG, Gem5Runner, stat_series
from waveflow.cpu.calib.kernels import Kernel
from waveflow.cpu.platform import CpuPlatform
from waveflow.cpu.processor import Processor
from waveflow.cpu.task import SwFunction
from waveflow.simulation.simulation import Simulation

HERE = Path(__file__).resolve().parent
FIXTURE = (
    Path(__file__).resolve().parents[2] / "tests/fixtures/cpu/cpu_sched_trace.jsonl"
)
CYCLES = "system.cpu_cluster.cpus.numCycles"
_CODE = {"add": "a", "delete": "d", "reprio": "r"}


def _expected(trace: list[dict[str, Any]]) -> dict[str, Any]:
    """What ``sched_replay.c`` must print for *trace*."""
    acc = 0
    handlers = (
        lambda x: (x + 1) & 0xFFFFFFFF,
        lambda x: (x * 3) & 0xFFFFFFFF,
        lambda x: x ^ 0x5A5A,
    )
    n_dispatch = 0
    for t in trace:
        if t["op"] == "delete":
            acc = handlers[t["gid"] % 3](acc)
            n_dispatch += 1
    adds = sum(t["op"] == "add" for t in trace)
    return {
        "kernel": "sched_replay",
        "n_ops": len(trace),
        "n_dispatch": n_dispatch,
        "final_len": adds - n_dispatch,
        "acc": acc,
        "n_tasks": [int(t["n_tasks"]) for t in trace],
        "n_scanned": [int(t["n_scanned"]) for t in trace],
        "n_moved": [int(t["n_moved"]) for t in trace],
    }


REPLAY = Kernel(
    name="sched_replay",
    twin=lambda trace: {},  # compared through _expected(), which needs the trace itself
    args=("trace", "mode"),
    counters=(),
    source_path=HERE / "sched_replay.c",
    symbols=("apply", "tg_insert", "tg_remove"),
)


def replayable(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*trace* without the aging calls that found nothing to promote (``gid`` -1).

    Such a call changes no list, so there is nothing to replay.  They are dropped here rather than
    taught to ``sched_replay.c``: a branch for them in ``apply()`` costs every operation a few cycles
    and moved the recorded 63,266-cycle total by 7 %.
    """
    return [t for t in trace if not (t["op"] == "reprio" and t["gid"] < 0)]


def trace_text(trace: list[dict[str, Any]]) -> str:
    """The C replay's input, one ``<op> <gid> <prio>`` line per operation (see ``replayable``)."""
    if any(t["gid"] < 0 for t in trace):
        raise ValueError(
            "a no-op aging call cannot be replayed: pass replayable(trace)"
        )
    return "".join(
        f"{_CODE[t['op']]} {t['gid']} {t.get('tg_prio', 0)}\n" for t in trace
    )


def load_trace(path: Path = FIXTURE) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@dataclass
class ReplayResult:
    n_ops: int
    n_dispatch: int
    counters_match: bool
    measured_total: float
    predicted_total: float
    total_rel_err: float
    op_median_rel_err: float
    op_max_rel_err: float
    per_family: dict[str, dict[str, float]]
    gem5_wall_s: float
    model_wall_s: float
    speedup: float
    #: The median marker cost in context (each region's following empty region).
    empty_region_cycles: float
    gem5_tag: str
    platform: str


def _model_wall_s(
    platform: CpuPlatform, trace: list[dict[str, Any]], repeats: int = 5
) -> float:
    """Wall time of pricing the trace through a Processor (best of *repeats*)."""
    funcs = {
        op: SwFunction(
            name=f"sched_ops.{op}",
            fn=lambda c: (None, c),
            cycles=platform.model(f"sched_ops.{op}"),
        )
        for op in _CODE
    }
    dispatch = SwFunction(
        name="dispatch",
        fn=lambda: (None, {"n_dispatch": 1.0}),
        cycles=platform.model("dispatch"),
    )
    best = float("inf")
    for _ in range(repeats):
        sim = Simulation()
        cpu = Processor(name="cpu", sim=sim, config=platform.cpu_config())

        def proc(cpu: Processor = cpu):  # bind this repeat's processor
            for t in trace:
                feats = {k: float(t[k]) for k in ("n_tasks", "n_scanned", "n_moved")}
                yield from cpu.execute(funcs[t["op"]], feats)
                if t["op"] == "delete":
                    yield from cpu.execute(dispatch)

        sim.env.process(proc())
        t0 = time.perf_counter()
        sim.env.run()
        best = min(best, time.perf_counter() - t0)
    return best


def replay(
    trace: list[dict[str, Any]] | None = None,
    *,
    runner: Gem5Runner | None = None,
    platform: CpuPlatform | None = None,
) -> tuple[ReplayResult, list[dict[str, Any]]]:
    """Run the replay on gem5 and compare; return the summary and the per-operation rows."""
    trace = replayable(trace if trace is not None else load_trace())
    runner = runner or Gem5Runner()
    platform = platform or CpuPlatform.load()
    why = runner.unavailable()
    if why:
        raise RuntimeError(why)
    runner.build(REPLAY)  # not timed: the comparison is simulation against simulation
    t0 = time.perf_counter()
    out, _, rundir = runner.run(
        REPLAY,
        {"trace": "/run/trace.txt"},
        extra_files={"trace.txt": trace_text(trace)},
        compact_stats=True,
        parse=False,
    )
    gem5_wall = time.perf_counter() - t0
    expected = _expected(trace)
    counters_match = out == expected

    # The last block is the exit's; the others alternate: a measured region, then an empty one.
    raw = stat_series(rundir / "m5out" / "stats.txt", CYCLES)[:-1]
    n_dispatch = expected["n_dispatch"]
    if len(raw) != 2 * (len(trace) + n_dispatch):
        raise RuntimeError(
            f"{len(raw)} regions for {len(trace)} ops + {n_dispatch} dispatches, "
            "each followed by its empty region"
        )
    # Net of the markers' cost *in that context* (the following empty region), not of the
    # standalone 94 cycles: back-to-back markers run hot and cost far less (sched_replay.c).
    net = [raw[i] - raw[i + 1] for i in range(0, len(raw), 2)]
    overhead = [raw[i + 1] for i in range(0, len(raw), 2)]

    rows: list[dict[str, Any]] = []
    it = iter(net)
    for t in trace:
        family = f"sched_ops.{t['op']}"
        feats = {k: float(t[k]) for k in ("n_tasks", "n_scanned", "n_moved")}
        rows.append(
            {
                "family": family,
                **feats,
                "measured": next(it),
                "predicted": platform.model(family).predict_feat(feats),
            }
        )
        if t["op"] == "delete":
            rows.append(
                {
                    "family": "dispatch",
                    "n_tasks": np.nan,
                    "n_scanned": np.nan,
                    "n_moved": np.nan,
                    "measured": next(it),
                    "predicted": platform.model("dispatch").predict_feat(
                        {"n_dispatch": 1.0}
                    ),
                }
            )
    meas = np.array([r["measured"] for r in rows])
    pred = np.array([r["predicted"] for r in rows])
    if (meas <= 0).any():
        raise RuntimeError(
            f"{int((meas <= 0).sum())} regions measured <= 0 cycles net of markers"
        )
    err = np.abs(pred - meas) / np.abs(meas)
    for r, e in zip(rows, err):
        r["rel_err"] = float(e)
    per_family = {}
    for fam in sorted({r["family"] for r in rows}):
        e = np.array([r["rel_err"] for r in rows if r["family"] == fam])
        m = sum(r["measured"] for r in rows if r["family"] == fam)
        p = sum(r["predicted"] for r in rows if r["family"] == fam)
        per_family[fam] = {
            "n": len(e),
            "median": float(np.median(e)),
            "max": float(e.max()),
            "measured_total": float(m),
            "predicted_total": float(p),
        }
    model_wall = _model_wall_s(platform, trace)
    res = ReplayResult(
        n_ops=len(trace),
        n_dispatch=n_dispatch,
        counters_match=counters_match,
        measured_total=float(meas.sum()),
        predicted_total=float(pred.sum()),
        total_rel_err=float(abs(pred.sum() - meas.sum()) / meas.sum()),
        op_median_rel_err=float(np.median(err)),
        op_max_rel_err=float(err.max()),
        per_family=per_family,
        gem5_wall_s=gem5_wall,
        model_wall_s=model_wall,
        speedup=gem5_wall / model_wall,
        empty_region_cycles=float(np.median(overhead)),
        gem5_tag=GEM5_TAG,
        platform=platform.name,
    )
    return res, rows


def replay_total(
    trace: list[dict[str, Any]] | None = None, *, runner: Gem5Runner | None = None
) -> dict[str, Any]:
    """The marker-free total: one region around the whole replay (``mode=total``).

    Per-operation markers perturb what they measure by tens of cycles -- the size of the operations
    themselves -- so the total is measured once more without them.  It still includes the replay
    loop's own few instructions per operation, which no model prices.
    """
    trace = replayable(trace if trace is not None else load_trace())
    runner = runner or Gem5Runner()
    out, _, rundir = runner.run(
        REPLAY,
        {"trace": "/run/trace.txt", "mode": "total"},
        extra_files={"trace.txt": trace_text(trace)},
        compact_stats=True,
        parse=False,
    )
    blocks = stat_series(rundir / "m5out" / "stats.txt", CYCLES)
    return {"counters_match": out == _expected(trace), "total_cycles": blocks[0]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Replay the scheduler trace on gem5 against the model."
    )
    ap.add_argument("--trace", type=Path, default=FIXTURE)
    ap.add_argument("--out", type=Path, help="write the summary (JSON) here")
    args = ap.parse_args(argv)
    res, _ = replay(load_trace(args.trace))
    text = json.dumps(asdict(res), indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")
    return 0 if res.counters_match else 1


if __name__ == "__main__":
    raise SystemExit(main())
