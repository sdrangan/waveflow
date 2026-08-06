---
title: RTL simulation
parent: Free-running memory copy
nav_order: 6
---

# RTL simulation

This is the final rung: the synthesized DUT, driven through real handshakes by the
[generated harness](./codegen_tb.md), one clock at a time. It is the number you would quote — a
cycle-accurate measurement of the actual RTL.

## Running it

Two toolchain steps stand between the Python model and an RTL run. First, C-synthesize the top (needs
Vitis HLS) — this produces the Verilog the harness drives:

```bash
python examples/mem_copy/mem_copy_build.py --through csynth
```

Then the RTL run itself is the **`-m xsi` gate** (needs Vivado `xsim` + a MinGW `g++`). It is a pytest
target rather than a build step, because it *asserts* an exact cycle count and so belongs with the
tests:

```bash
pytest tests/examples/test_xsi_bfm.py -m xsi -k mem_copy
```

Under the hood the gate runs `xsi/run.bat`: `xvlog` compiles the RTL, `xelab -dll` elaborates it into
`xsimk.dll`, `g++` builds the [BFM harness](./codegen_tb.md) main against it, and the executable steps
the clock for the fixed loop bound. The gate regenerates the Verilog file list and clears the cached
`xsim.dir` first — a stale file list plus a cached `.dll` is how an XSI run goes green while proving
nothing.

## Data in, data out

The C++ main checks nothing — it runs and dumps. Every value crossing the boundary is a **burst
bundle** (a folder of `words.bin` + `bounds.bin` + `meta.json`). Written *before* the run by
`write_mem_copy_xsi_bundles` (the same `MemCopySim.write_scenario` the pysim rung uses):

| bundle | contents |
|---|---|
| `vectors/s_cmd` | the commands, packed by `CopyCmd.serialize()` — the `AxisMaster` plays these |
| `vectors/mem_in` | the source arena; the `FlatMemory` seeds itself from it |
| `vectors/golden` | the expected arena after the copy |

Written *by* the run, in `post_sim`:

| bundle | contents |
|---|---|
| `vectors/out` | the memory arena as it ended up |
| `vectors/s_done` | the completion words, **plus `cycles.bin`** — the cycle each word arrived |

That last file is what lets timing be checked off-line: the sink records *when* each completion landed;
nothing interprets it in C++.

## Inspecting the results

The golden lives in Python. `check_mem_copy_xsi_outputs` (in `examples/mem_copy/mem_copy.py`) reads the
dumped bundles and asserts the three things that make the run correct:

1. **the copy** — every destination region equals `vectors/golden`;
2. **completion** — one `CopyResp` per job, each echoing back the `tx_id` the host set;
3. **timing** — the cycle the *last* completion landed is exactly **2908**.

That third one catches silent regressions. It is **time-to-last-completion**, not the loop bound: the
run loops a fixed 3400 cycles with a drain tail, but the *work* finishes at 2908 —
`cycles.bin[n-1]` for the last completion word. It is a direct fingerprint of the free-running
schedule, so if a change perturbs it, the assertion moves — a real behaviour change worth a human look,
not an inequality that would absorb a regression silently.

## Which rung to trust for what

The [pysim rung](./testbench.md) ran the *same graph*, and the two agree on the **architectural**
story: the design is pipelined, not sequential; the period is dominated by one direction
(`max(read, write)`), not the sum; the job count and ordering match. They do **not** agree on the
cycle count — pysim runs optimistic, because a transaction-level model does not reproduce every
source of per-cycle contention in the generated RTL.

So use each for what it is good at:

- **pysim** — correctness, overlap and structure, deadlock, job accounting. Fast enough to run on
  every edit.
- **RTL** — the number you would quote. The `-m xsi` gate asserts **2908** exactly.

*Why* the two agree once calibrated, and how to *see* it, is the next page: [Visualizing
timing](./timing.md) traces the internal channels and both `m_axi` bundles out of an RTL run,
attributes every cycle of the period, and shows why the pysim reproduces it — `mem_copy` reuses the
platform's **shipped** bus and mem-stream timing models (the general
[calibration guide](../../guide/calib/) covers how those are built). A *calibrated* pysim reproduces the
RTL period; an **uncalibrated** one (no platform) is a lower bound with the right shape, not a
prediction.
