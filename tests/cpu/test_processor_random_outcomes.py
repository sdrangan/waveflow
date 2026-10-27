"""The processor model's scheduling, pinned on random workloads with preemption and interrupts.

``plans/sim_speed_cpu_linalg.md`` made the ``Processor`` faster (step 10) without changing what it
simulates; the other outcome pins (``test_sim_speed_outcomes.py``) are all run-to-completion and never
interrupt.  These 200 seeded workloads cover what they do not: one to three cores, preemptive and
run-to-completion scheduling, interrupt handlers, several clocks and switch costs, zero and fractional
cycle counts, callers that submit from freshly spawned processes at the same instant.  Each is reduced
to a digest of every :class:`~waveflow.cpu.task.TaskRecord` (``tests/cpu/_outcomes.py``) and of the
order and time of every body call and every result, and compared with the digests recorded by
**``main``'s processor** (before the optimizations), so a change in the order of events fails here.

The generator is the independent review's differential fuzz (step 17), which found the optimized and
the original processor identical on 60,000 such workloads.  Re-record -- only for a change *meant* to
change the outcome -- with the processor to record from:
``python -m tests.cpu.test_processor_random_outcomes --record <path/to/processor.py>``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import random
import sys
from pathlib import Path
from types import ModuleType

from tests.cpu import _outcomes as O
from waveflow.cpu import processor as current
from waveflow.cpu.config import CpuConfig
from waveflow.cpu.task import SwFunction
from waveflow.simulation.simulation import Simulation

SECTION = "random"
N_WORKLOADS = 200


def workload(seed: int) -> tuple[dict, list]:
    """A random configuration and its software threads: ``(start, [call, ...])`` each."""
    rng = random.Random(seed)
    # The draws happen in this order; a literal evaluates its entries left to right, so reordering
    # them would change every workload and the recorded digests with it.
    cfg = {
        "n_cores": rng.choice([1, 1, 2, 3]),
        "f_clk_hz": rng.choice([1.0, 1.0, 3.0, 1e9 / 7]),
        "preemptive": rng.random() < 0.6,
        "switch_cycles": rng.choice([0, 0, 1, 2, 3.5, 4]),
        "irq_entry_cycles": rng.choice([0, 1, 2]),
    }
    threads = []
    for t in range(rng.randint(1, 6)):
        calls = [
            {
                "delay": rng.choice([0, 0, 0, 1, 2, 0.5, 3, 7]),
                "irq": rng.random() < 0.2,
                "prio": rng.choice([0, 1, 2, 5, 5, 9]),
                "cyc": rng.choice([0, 0, 1, 2, 3, 4.5, 10, 13, 0.25]),
                # submitted from a fresh process, not waited for
                "spawn": rng.random() < 0.25,
                "name": f"t{t}k{k}",
            }
            for k in range(rng.randint(1, 8))
        ]
        threads.append((rng.choice([0, 0, 1, 2]), calls))
    return cfg, threads


def outcome(mod: ModuleType, seed: int) -> tuple[str, int, int]:
    """Run workload *seed* on *mod*'s ``Processor``: its digest, preemptions and interrupts."""
    cfg, threads = workload(seed)
    sim = Simulation()
    cpu = mod.Processor(name="cpu", sim=sim, config=CpuConfig(**cfg))
    env = sim.env
    log: list[str] = []

    def call(c: dict):
        def body():
            log.append(f"body {c['name']} {float(env.now).hex()}")
            return c["name"], {"c": float(c["cyc"])}

        f = SwFunction(name=c["name"], fn=body, cycles=float(c["cyc"]),
                       energy_pj=2.0 * c["cyc"] + 1)  # fmt: skip
        if c["irq"]:
            r = yield from cpu.interrupt(f)
        else:
            r = yield from cpu.execute(f, prio=c["prio"])
        log.append(f"done {c['name']} {float(env.now).hex()} {r}")

    def thread(start, calls):
        if start:
            yield env.timeout(start)
        for c in calls:
            if c["delay"]:
                yield env.timeout(c["delay"])
            if c["spawn"]:
                env.process(call(c))
            else:
                yield from call(c)

    for start, calls in threads:
        env.process(thread(start, calls))
    env.run()
    keys = [O.record_key(r) for r in cpu.records] + log + [float(env.now).hex()]
    digest = hashlib.sha256("\n".join(keys).encode()).hexdigest()[:24]
    return (
        digest,
        sum(r.n_preempted for r in cpu.records),
        sum(r.is_irq for r in cpu.records),
    )


def test_random_workloads_schedule_as_the_original_processor_did():
    want = O.load(SECTION)
    got = [outcome(current, s)[0] for s in range(N_WORKLOADS)]
    moved = [
        s for s, (a, b) in enumerate(zip(got, want["digests"], strict=True)) if a != b
    ]
    assert (
        not moved
    ), f"{len(moved)} of {N_WORKLOADS} workloads moved, the first is seed {moved[0]}"


def test_the_workloads_exercise_preemption_and_interrupts():
    want = O.load(SECTION)
    assert want["preemptions"] > 100 and want["interrupts"] > 100, want


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("recorded_processor", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # its dataclasses resolve their annotations through it
    spec.loader.exec_module(mod)
    return mod


def record(path: Path) -> None:
    mod = _load(path)
    runs = [outcome(mod, s) for s in range(N_WORKLOADS)]
    O.save(SECTION, {"processor": str(path.name), "recorded_from": sys.argv[3] if len(sys.argv) > 3
                     else "", "digests": [r[0] for r in runs],
                     "preemptions": sum(r[1] for r in runs), "interrupts": sum(r[2] for r in runs)})  # fmt: skip


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "--record":
        sys.exit("usage: python -m tests.cpu.test_processor_random_outcomes --record "
                 "<path/to/processor.py> [<what it is>]")  # fmt: skip
    record(Path(sys.argv[2]))
    print(f"recorded section '{SECTION}' of {O.FIXTURE}")
