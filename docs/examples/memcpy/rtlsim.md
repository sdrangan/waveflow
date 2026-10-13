---
title: RTL simulation
parent: Free-running memory copy
nav_order: 6
summary: "The final rung: the system flow runs the synthesized kernel inside its system top, driven by the generated harness one clock at a time, then compares what the testbench recorded at RTL with what it recorded in pysim, word for word. The golden runs on the RTL's recordings, and the cycle the last completion landed (2908) is the number you would quote."
---

# RTL simulation

This is the final rung. The synthesized kernel runs inside the [system top](./system.md), driven
through real handshakes by the generated harness, one clock at a time. The result is the number you
would quote: a cycle-accurate measurement of the actual RTL.

## Running it

One command runs the whole system flow. It needs Vitis HLS for `csynth`, and Vivado `xsim` with a
C++ compiler for the run:

```bash
python -m examples.mem_copy.mem_copy_system_build
```

The steps run in dependency order:

| step | what it does |
|---|---|
| `scenario` | writes the scenario once (`write_inputs` and `run.json`) |
| `csynth` | C-synthesizes the kernel from [DUT codegen](./codegen_dut.md)'s `mem_copy.tcl`, unless its RTL is current |
| `pysim` | runs the system in pysim from a copy of the scenario, and gathers what it recorded |
| `system_rtl` | renders the system top and its file list |
| `system_xsi` | builds the harness, compiles and elaborates the RTL, runs it from another copy of the scenario, and gathers what it recorded |
| `compare` | compares the two sets of recordings, word for word |

```
scenario:
    xsi_work\mem_copy_system\scenario
    PASSED (0.0 s)
csynth:
    mem_copy_proj\solution1\syn\verilog
    UP-TO-DATE
pysim:
    xsi_work\mem_copy_system\pysim_traces
[copy] jobs=16 done_bursts=16 all_ok=True
    PASSED (0.1 s)
system_rtl:
    xsi_work\mem_copy_system\rtl.json
    PASSED (0.0 s)
system_xsi:
    xsi_work\mem_copy_system\traces
    PASSED (5.8 s)
compare:
    xsi_work\mem_copy_system\compare.json
    PASSED (0.0 s)
```

`--synth check` makes `csynth` fail rather than build when the kernel's RTL is missing or stale.
That is what the test gate uses, because a gate must never grade RTL it did not produce. Each step is
cached: an unchanged design reruns only the simulation, and a cached elaboration is reused only if
its content stamp matches the RTL just written.

## What the run records

Everything that crosses the boundary is a **burst bundle** (a folder of `words.bin`, `bounds.bin` and
`meta.json`). The inputs are the scenario's: `s_cmd`, `mem_in`, and `golden` for the check. The
outputs are what the testbench's models recorded in `post_sim`. The flow gathers them from each run
into `pysim_traces/` and `traces/`:

| recording | recorded by | contents |
|---|---|---|
| `out` | the memory | the arena as it ended up |
| `s_done` | the completion sink | the `CopyResp` words, **plus `cycles.bin`** at RTL: the cycle each one landed |

`compare` checks that every recording holds the same words at RTL as in pysim. A mismatch fails the
run and names the recording, the participant that records it, and the first word that differs.

## The golden, on the RTL's recordings

`check_xsi` in `mem_copy_system_build.py` runs `check_mem_copy_xsi_outputs` on the RTL run's
recordings. It asserts the three things that make the run correct:

1. **the copy**: every destination region equals `golden`;
2. **completion**: one `CopyResp` per job, each echoing back the `tx_id` the command set;
3. **timing**: the last completion landed at cycle exactly **2908**.

The third catches silent regressions. It is time-to-last-completion, not the loop bound: the run
steps a fixed 3400 cycles, but the work finishes at 2908, the last entry of `s_done/cycles.bin`. It
is a direct fingerprint of the free-running schedule. If a change moves it, that is a real behaviour
change worth a human look, which an inequality would have absorbed.

The gate is a pytest target, because it asserts:

```bash
pytest -m xsi tests/examples/test_mem_copy_system_xsi.py
```

It runs the same DAG through `compare`, then asserts the comparison came back clean and the golden
holds at 2908.

## Which rung to trust for what

The pysim rung and the RTL rung agree **word for word** on what the testbench recorded, which is what
`compare` checks. They agree on the **architectural** story: the design is pipelined, not
sequential; the period is dominated by one direction (`max(read, write)`), not the sum; the job count
and ordering match. They do **not** agree on the cycle count. pysim's 16 jobs end at about 2361
cycles; the RTL's at 2908. A transaction-level model does not reproduce every source of per-cycle
contention in the generated RTL.

So use each for what it is good at:

- **pysim**: correctness, overlap and structure, deadlock, job accounting. Fast enough to run on
  every edit.
- **RTL**: the number you would quote. The gate asserts **2908** exactly.

*Why* the two agree once calibrated, and how to *see* it, is the next page. [Visualizing
timing](./timing.md) traces the internal channels and both `m_axi` bundles out of an RTL run,
attributes every cycle of the period, and shows why the pysim reproduces it: `mem_copy` reuses the
platform's **shipped** bus and mem-stream timing models (the general
[calibration guide](../../guide/calib/) covers how those are built). A *calibrated* pysim reproduces
the RTL period; an **uncalibrated** one (no platform) is a lower bound with the right shape, not a
prediction.
