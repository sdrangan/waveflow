---
title: BFM Testbenches
parent: Build System
nav_order: 7
---

# BFM testbenches

At the XSI rung, a C++ testbench drives the *generated RTL* cycle by cycle — the execution path for
free-running `ap_ctrl_none` task networks, which Vitis cosim refuses to run.

What that testbench is made of has changed. The bus models are **framework code**, and for a
testbench declared as a component graph the assembly is **generated** too. What you write is the
scenario and the golden — in Python, on either side of the run.

## What you do not write

Every AXI and AXI-Stream model lives in `waveflow/build/xsi/xsi_bfm.h` and is used as-is:

| model | drives |
|---|---|
| `AxisMaster` | an AXI-Stream input — plays a burst bundle onto `TVALID`/`TDATA` |
| `AxisSlave` | an AXI-Stream output — consumes on `TREADY`/`TVALID`, tagging each word with its arrival cycle |
| `AxiMmReadSlave` | the `m_axi` read channels (AR/R), returning words beat-by-beat with `RLAST` |
| `AxiMmWriteSlave` | the `m_axi` write channels (AW/W/B), applying `WSTRB` masks and answering with `BVALID` |
| `FlatMemory` | the word arena behind one or more `m_axi` bundles |

Burst bookkeeping, beat counters, `RLAST`/`WLAST` handling, byte-address-to-word conversion and the
handshake accounting are all inside those classes. A testbench composes them; it does not reimplement
them, and it contains no per-cycle bus code.

## One lifecycle, five phases

Every model derives from `XsiSimObj`, the C++ mirror of Python's `SimObj`. All five phases default to
no-ops, so a model implements only what it needs:

| phase | when | typical use |
|---|---|---|
| `pre_sim()` | before reset | seed memory, load command vectors from a bundle |
| `sample()` | clock **low** | read kernel outputs, latch beats (`VALID && READY`) |
| `update()` | after the rising edge | apply this cycle's beats, advance FSMs |
| `drive()` | end of cycle | present held values for the next cycle |
| `post_sim()` | after the run | dump results to bundles, collect metrics |

The cycle loop just applies those phases in order across the participants. Sampling in the clock-low
phase is what keeps handshake accounting consistent and avoids off-by-one timing errors — but that
discipline now lives in the loop and the models, not in code you maintain per testbench.

## Two ways to assemble one

### Generated — from the testbench graph

When the testbench is declared as a component graph (a composite `FreeRunMod` holding the DUT and its
participants), walking it produces the harness *and* the `main`. `mem_copy` is the worked example:
each participant declares its own BFM twin via `bfm_model()`, so the mapping is derived rather than
maintained, and the entire hand-written C++ surface is:

```cpp
int main() {
    mem_copy_tb::Harness h("mem_copy_bfm.wdb");
    h.run(3400);
    h.close();
    return 0;
}
```

Both that file and `xsi/mem_copy_tb_harness.h` are build outputs. Nothing is checked in C++ — the run
dumps its results and Python compares them. See
[the mem_copy testbench](../../examples/memcpy/testbench.md) for the whole path, and
[stream drivers and sinks](../sim/stream_tb.md) for the participants themselves.

### Hand-assembled — a `main` that composes the models

The three interleaver tops (`mem_r_stream`, `mem_w_stream`, `interleaver_canon`) still assemble their
own `main`. This is the right shape when the run needs a completion rule or a golden comparison the
generated harness does not express yet — `interleaver_canon`, for instance, counts one job per *two*
`s_done` beats, because its token is a 2-word `InterleaverCmd`.

Even then there is no handshake code. You construct the models, point them at bundles, and run the
phases:

```cpp
FlatMemory      mem(MEM_NW, BPW);
AxisMaster      s_cmd (sim.dut(), ports::s_cmd, {});
AxisSlave       s_done(sim.dut(), ports::s_done);
AxiMmReadSlave  gmem0 (sim.dut(), ports::m_in,  mem);
AxiMmWriteSlave gmem1 (sim.dut(), ports::m_out, mem);

mem.load_segs   = { { (size_t)0, 0, "vectors/mem_in" } };   // seeded in pre_sim
mem.dump_segs   = { { (size_t)0, (size_t)MEM_NW, "vectors/out" } };   // written in post_sim
s_cmd.in_bundle = "vectors/cmd";

std::vector<XsiSimObj*> parts = { &mem, &s_cmd, &s_done, &gmem0, &gmem1 };
for (auto* p : parts) p->pre_sim();
sim.reset([&]{ for (auto* p : parts) p->drive(); });

for (;;) {
    // ... termination checks ...
    sim.clock_low();
    for (auto* p : parts) p->sample();
    sim.clock_high();
    for (auto* p : parts) p->update();
    for (auto* p : parts) p->drive();
}
for (auto* p : parts) p->post_sim();
```

The `_ports.h` header naming those port structs is generated from the same `TopSpec` as the top's
own pragmas, so the testbench and the DUT cannot disagree about port names.

Reference implementation:
[`examples/interleaver/xsi/interleaver_canon_bfm_tb.cpp`](https://github.com/sdrangan/waveflow/tree/main/examples/interleaver/xsi/interleaver_canon_bfm_tb.cpp).

## Data crosses as bundles, not literals

No scenario data is written in C++. Inputs — the command stream, the memory arena, the golden — are
**burst bundles** (a folder of `words.bin` + `bounds.bin` + `meta.json`) written by a Python generator
before the run; outputs are bundles written back during `post_sim`. The pattern is therefore stated
once, in Python, and the C++ only plays and records it.

This is what makes the concurrent flow structurally identical to the sequential one: Python writes the
inputs, the kernel and testbench are generated, the toolchain runs them, and Python checks the outputs.

## Completion and throughput framing

`AxisSlave` records the arrival cycle of every word, so completion timing is available off-line
(`cycles.bin` in the output bundle). From per-job completion cycles you get **fill latency** (the first
completion) and the **steady-state period** (the delta between successive ones), and thus the
pipeline's throughput.

One trap worth stating plainly: the meaningful number is **time-to-last-completion**, not the loop
count. A run loops a fixed number of cycles and then drains, so the loop bound overstates the work —
`mem_copy` finishes at 2835 inside a 3400-cycle loop. Compare completion cycles, never the total.

## See also

- [XSI Build Rung](./xsi.md) — terminology and the full compile/elaborate/run flow.
- [The mem_copy testbench](../../examples/memcpy/testbench.md) — the generated path end to end.
- [Stream drivers and sinks](../sim/stream_tb.md) — the pysim participants and their BFM twins.
