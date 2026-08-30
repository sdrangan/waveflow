---
title: Python simulation
parent: VMAC with an AXI-MM command queue
nav_order: 5
has_children: false
---

# Python simulation

The SimPy simulation wires the host and the accelerator to one shared memory and
runs the whole command-queue protocol as a discrete-event simulation. It is the
runnable Stage-2 model in
[`examples/vmac/vmac_queue_sim.py`](../../../examples/vmac/vmac_queue_sim.py), and it
is where numerical parity against the golden is established before any C++ exists.

## Wiring

Two masters, one memory, one interconnect:

```
   VmacHost ─┐                          ┌─ ring (head/tail + command slots)
 (SimObj)    ├─ AXIMMCrossBarIF ─ MemoryMod ─ A region
   VmacAccel ┘                          └─ B | Y regions
```

The [`VmacHost`](../../../examples/vmac/vmac_host.py) and the `VmacAccel` are both
masters on an `AXIMMCrossBarIF`; a single `MemoryMod` is the slave holding the
command **ring** (its `head`/`tail` pointers and slots) alongside the `A`, `B`, and
`Y` data regions. Both sides reach the ring through an
[`AXIMMQueue`](../../guide/interface/derived/mmqueue.md) bound to that memory — the host as
producer (`write`), the accelerator as consumer (`get`). This mirrors the minimal
[`examples/interface/aximm_queue_demo.py`](../../../examples/interface/aximm_queue_demo.py)
demo, scaled up to a real datapath.

## The free-running consumer

The accelerator does not wait to be told to start. Its `run_proc` loops forever,
dequeuing and executing until the host enqueues the `end` sentinel:

```python
while True:
    cmd = yield from self.cmd_queue.get(self.Cmd)   # block until a command is in the ring
    if cmd.op == OpCode.end:
        return                                       # sentinel — drain and stop
    yield from self.vmac_compute(cmd, self.m_mem)    # timed reads → execute → timed writeback
```

Each `get` reads the ring pointers and one slot; when the ring is empty it
[polls](../../guide/timing_model/poll.md) until the host advances `tail`. The host, for
its part, writes `A` and `B`, enqueues `anorm` and `abcorr` (both `inner_prod +
reduce`) then `end`, barriers on the ring draining, reads both `Y` regions back, and
computes `rho = abcorr / anorm` itself (the [host/host-side division](./vmac.md#the-motivating-computation)).

## The timing model at a glance

The run logs a **source-agnostic** timeline — per-command memory transactions
(`rw` / `addr` / `nwords` / `tstart` / `tend`), latencies, and ring occupancy — to
`timeline/sim_timeline.json`. The Stage-2 loosely-timed (LT) model treats each
operand transfer as a single timed **block** holding a capacity-1 bus resource for
its whole duration. That is enough to get **occupancy** and the **`ab_eq` read-word
halving** right, but not absolute latency — closing that gap with a Vitis cosim
calibration is the whole subject of the [timing](./timing.md) page, which is why the
timeline is emitted in the same schema as the cosim timeline.

## Parity — the self-check

The simulation's headline assertion is **bit-exact numerical parity**: the values
VMAC writes to `Y` equal the one [`execute` golden](./pymodel.md), byte-for-byte, and
the host's reconstructed `rho` matches the numpy reference. Alongside that, the run
confirms the structural invariant the LT model *does* capture: the `anorm` command
(where `B` aliases `A`, so `ab_eq` holds) issues **half** the read words of `abcorr`
— 16 vs. 32 at 4×4. (The complementary "equal *latency* despite the freed bus"
invariant is the calibrated-model result; see [timing](./timing.md).)

Run it:

```bash
cd examples/vmac
python vmac_queue_sim.py         # run the sim, re-emit timeline/sim_timeline.json + sim_sweep.json
```

With parity established in pure Python, the [next step](./codegen.md) extracts the
same `run_proc` into a synthesizable top.
