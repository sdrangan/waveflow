---
title: Hostile stand-ins
parent: Developing module by module
grand_parent: Modules
nav_order: 3
audience: python
api: [StandInPolicy, POLICIES, add_robustness_steps, harness, memory_harness]
summary: "A polite testbench never shows that a module survives anything else. A StandInPolicy makes each of a harness's stand-ins misbehave as its protocol allows: a stream sink drops TREADY, a stream driver or credit source holds off its next word (never withdrawing one it presented), a credit sink holds its reports (starved credit), a scripted bus master takes its answers late. At RTL the stall is cycle by cycle, from a seeded hash shared with the C++ models; pysim approximates it per burst. add_robustness_steps adds the harness once per policy that touches its stand-ins, and each one's compare is the check: the RTL must still finish with exactly pysim's words. The memory stand-in is polite: the RTL AXI-MM slave models have no stall policy."
---

# Hostile stand-ins

A polite testbench -- a driver that presents a word every cycle it can, a sink always ready, credit
returned at once -- never shows that a module survives anything else. A
[`StandInPolicy`](../../../waveflow/simulation/policy.py) makes each of a [harness](units_harness.md)'s
stand-ins misbehave, each only as its protocol allows:

| stand-in | the policy's knob | what it does |
|---|---|---|
| `StreamSink` | `stall_pct` | drops TREADY on that percent of cycles |
| `StreamDriver`, `CreditSource` | `stall_pct` | holds off its next word -- never withdrawing one it presented: AXI-Stream allows nothing else |
| `CreditSink` | `stall_pct`, `credit_delay` | drops its ready on that percent of cycles; holds each credit report that many cycles after it is due: starved credit |
| `MmScript` (a memory's host) | `stall_pct`, set from the policy's `answer_pct` | drops RREADY / BREADY: takes its answers late |
| the memory stand-in | -- | polite: the RTL AXI-MM slave models (`AxiMmReadSlave`, `AxiMmWriteSlave`) have no stall policy |

At RTL the stall is cycle by cycle: whether a cycle stalls is a seeded hash of the cycle number,
`stalled(seed, cycle, pct)`, the same function as the C++ `wfbfm::policy_stalled`. pysim is the functional
reference, so its stand-ins only approximate the policy -- a delay per burst of the cycles it would have
stalled on average.

## The sweep

```python
from waveflow.build.robustness import add_robustness_steps

add_robustness_steps(dag, lambda p: harness(MarkovGen, policy=p), work_dir=..., top=..., prefix="rob_")
```

[`add_robustness_steps`](../../../waveflow/build/robustness.py) builds the harness under each policy of
the sweep (`POLICIES`) and adds it as a system of its own, `<prefix><policy>_*`, through `compare`. A
policy that touches none of the harness's stand-ins (`policy.touches(sysm.stand_ins.values())`) is
skipped. The harness scales its RTL run's bound by how much longer the policy may make it take
(`policy.bound(n_cycles)`); the calibration workload is always polite.

The check is the ordinary `compare`: the RTL must still finish with exactly pysim's words. A hang shows
as a recording cut short, a protocol slip as different words. A policy that changes nothing finds
nothing, so the sweep is harsh:

| policy | knobs |
|---|---|
| `stall` | `stall_pct=90` |
| `starved_credit` | `credit_delay=2000` |
| `late_answers` | `answer_pct=90` |

The gates are [`test_robustness_xsi.py`](../../../tests/examples/test_robustness_xsi.py): the
polynomial kernel and markov's generator, chain core and memory, each under every policy that touches
it. Where this sits in the flow: [Functional verification](../systems/verification.md#robustness-hostile-stand-ins).
