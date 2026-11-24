"""os_filterbank's pysim speed, one tree against another (``plans/osfb_pysim_fidelity.md``, Step 6).

Two modes, one file, so the same script measures every tree:

* ``python speed.py run <kind> <mode> <reps>`` -- one process, on whichever tree is first on
  ``PYTHONPATH``: ``reps`` runs of the bank on scenario ``kind`` (``test`` or ``long``), each on a fresh
  harness and timed around ``sysm.run()`` alone, with nothing instrumented; then one more run, untimed, that
  counts SimPy's events (``env.step``) and the bursts and words the links move
  (``QueuedTransferIF._transfer``).  ``mode`` ``untimed`` is the bank as built; ``timed`` puts every stream
  block the tree can time on the packaged platform (``platform_dir`` where the block has the field: the
  arbiter at Stage 7, the arbiter and the three demuxes since cause F).  Prints one JSON line.
* ``python speed.py compare <name>=<tree> <name>=<tree> [--rounds 5] [--reps 5]`` -- alternates fresh
  processes, one ``run`` each, tree by tree within every round, scenario and mode; prints each
  configuration's best run, and the events, bursts and words (deterministic, so the same every round).

A tree is a checkout's root, for example one extracted from git (``git archive <commit> | tar -x -C <dir>``);
the script itself is passed by path, so it need not exist in the trees it measures.  Run it on an idle
machine.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import time
from pathlib import Path

KINDS = ("test", "long")
MODES = ("untimed", "timed")
BLOCKS = ("arb", "dmx_ans", "dmx_st", "dmx_crd")


def run(kind: str, mode: str, reps: int) -> dict:
    """The bank on *kind*, *reps* timed runs and one counted run, in this process."""
    import examples.os_filterbank.os_filterbank as fb
    import waveflow
    from examples.os_filterbank.os_filterbank_build import N_CYCLES
    from waveflow.hw import interface
    from waveflow.hw.harness import harness

    def build():
        sysm = harness(fb.OsFilterBank, workload=kind, n_cycles=N_CYCLES[kind])
        dut = sysm.dut()
        timed = []
        if mode == "timed":
            from waveflow.calib.platform import packaged_platforms_dir
            from waveflow.streams.timing import PLATFORM

            for name in BLOCKS:
                blk = getattr(dut, name)
                if "platform_dir" in {f.name for f in dataclasses.fields(blk)}:
                    blk.platform_dir = packaged_platforms_dir() / PLATFORM
                    timed.append(name)
        return sysm, dut, timed

    walls = []
    for _ in range(reps):
        sysm, dut, timed = build()
        t0 = time.perf_counter()
        sysm.run()
        walls.append(time.perf_counter() - t0)
        if mode == "timed" and not all(getattr(dut, n).timing_model is not None for n in timed):
            raise RuntimeError(f"a platform model did not attach: {timed}")

    # The counted run: every event the environment processes, every burst a link moves.
    n = {"events": 0, "bursts": 0, "words": 0}
    orig = interface.QueuedTransferIF._transfer

    def counted_transfer(self, ep, words, *a, **k):
        n["bursts"] += 1
        n["words"] += len(words)
        return (yield from orig(self, ep, words, *a, **k))

    interface.QueuedTransferIF._transfer = counted_transfer
    sysm, dut, timed = build()
    env = sysm.sim.env
    step = env.step

    def counted_step():
        n["events"] += 1
        return step()

    env.step = counted_step
    sysm.run()
    interface.QueuedTransferIF._transfer = orig
    per = dut.clk.period
    last = [round(sysm.stand_ins[f"m_status_{c}"].stream_ep.last_stamp.delivered / per) - 1 for c in range(fb.C)]
    return {"waveflow": waveflow.__file__, "kind": kind, "mode": mode, "timed_blocks": timed,
            "best_s": min(walls), "walls_s": walls, **n, "end_cycle": round(env.now / per, 3),
            "last_status_beat": last}


def compare(trees: dict[str, Path], rounds: int, reps: int) -> list[dict]:
    """Fresh processes, alternating the trees within every round, scenario and mode."""
    rows = []
    for rnd in range(1, rounds + 1):
        for kind in KINDS:
            for mode in MODES:
                for name, root in trees.items():
                    env = {**os.environ, "PYTHONPATH": str(root)}
                    out = subprocess.run([sys.executable, "-W", "ignore", str(Path(__file__).resolve()), "run",
                                          kind, mode, str(reps)], env=env, cwd=root, check=True,
                                         capture_output=True, text=True).stdout
                    row = {"round": rnd, "tree": name, **json.loads(out.strip().splitlines()[-1])}
                    if not row["waveflow"].startswith(str(root)):
                        raise RuntimeError(f"{name}: imported {row['waveflow']}, not {root}")
                    rows.append(row)
    return rows


def summary(rows: list[dict]) -> None:
    names = list(dict.fromkeys(r["tree"] for r in rows))
    print("| scenario | mode | " + " | ".join(f"{n}: best ms, events" for n in names) + " | wall | events |")
    print("|---|---|" + "---|" * len(names) + "---|---|")
    for kind in KINDS:
        for mode in MODES:
            cells, best, ev = [], [], []
            for name in names:
                rs = [r for r in rows if r["tree"] == name and r["kind"] == kind and r["mode"] == mode]
                if len({(r["events"], r["bursts"], r["words"]) for r in rs}) != 1:
                    raise RuntimeError(f"{name} {kind} {mode}: the counts differ between processes")
                best.append(min(r["best_s"] for r in rs))
                ev.append(rs[0]["events"])
                cells.append(f"{1e3 * best[-1]:.1f}, {ev[-1]:,} ({rs[0]['bursts']:,} bursts, "
                             f"{rs[0]['words']:,} words; end {rs[0]['end_cycle']:,.0f}; "
                             f"last {rs[0]['last_status_beat']}; timed {rs[0]['timed_blocks']})")
            print(f"| `{kind}` | {mode} | " + " | ".join(cells)
                  + f" | x{best[-1] / best[0]:.3f} | x{ev[-1] / ev[0]:.3f} |")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("kind", choices=KINDS)
    r.add_argument("mode", choices=MODES)
    r.add_argument("reps", type=int)
    c = sub.add_parser("compare")
    c.add_argument("trees", nargs="+", help="name=root, in the order each round runs them")
    c.add_argument("--rounds", type=int, default=5)
    c.add_argument("--reps", type=int, default=5)
    c.add_argument("--out", type=Path, help="also write every process's row here (JSON lines)")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        print(json.dumps(run(a.kind, a.mode, a.reps)))
        return
    trees = {}
    for t in a.trees:
        name, root = t.split("=", 1)
        trees[name] = Path(root).resolve()
    rows = compare(trees, a.rounds, a.reps)
    if a.out:
        a.out.write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
    summary(rows)


if __name__ == "__main__":
    main()
