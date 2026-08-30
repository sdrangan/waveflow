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

### Which model serves which port {#bfm-duals}

A testbench port is never free: it must present the **dual** of the DUT port it faces — the opposite
role on the same protocol. That pairing is one table, `BFM_DUALS` in
[`composite_gen.py`](../../../waveflow/build/composite_gen.py), and every caller goes through it. So
"which duals exist?" has one lookup, and the **holes are rows in the same table** rather than a
caveat in prose:

| DUT boundary port | protocol | the TB must present | model |
|---|---|---|---|
| `axis_in` | AXI4-Stream | master | `AxisMaster` * |
| `axis_out` | AXI4-Stream | slave | `AxisSlave` * |
| `maxi_read` | AXI4-MM | read slave | `AxiMmReadSlave` |
| `maxi_write` | AXI4-MM | write slave | `AxiMmWriteSlave` |
| `mm_slave` | AXI4-MM | master | **none** — in this flow the kernel is always the master |
| `axilite_slave` | AXI4-Lite | master | **none** — so a `HostActivated` DUT cannot be driven at RTL |

\* On AXI-Stream the role fixes the direction but not the class, so the **participant** names it: a
source, a sink, and a peer that never backpressures are three classes in one role. On `m_axi` there
is nothing to choose — a memory does not get to decide whether it is read or written; the DUT's port
kind decides, and the participant supplies only the arena.

The AXI4-Lite hole is the load-bearing one: it is why every design verified this way is free-running.
Filling it is future work (`plans/design_cut.md` §S7).

### Models may bind each other {#channels}

The table above answers "what must the testbench present against a DUT **port**?" — which presumes
there is a port. Not every edge has one. An interface whose endpoints *both* lie outside the cut has
no DUT port between them and therefore no dual to look up, but it is not thereby absent: its peers
are still nodes, and something has to move values between them.

That something is a **channel**, and it lives in its own header:

| primitive | in | is |
|---|---|---|
| `BlockChannel<T>` | `waveflow/build/xsi/xsi_channel.h` | a depth-bounded queue between **two models**, with drop / starve counters |
| `RateTick` | same | the fractional-credit accumulator for an edge running on its own clock |

A channel is a separate header and a separate registry rather than a row in `BFM_DUALS`, and the
reason is structural: `BFM_DUALS` is keyed by the DUT's boundary port kind, and a model↔model edge
has no such kind — that is the definition of one.

**The rule that makes it work: write in `update()`, read in the next `sample()`.** A direct call
between two models would make the transfer's timing depend on the order the harness happens to visit
its participants in — a generator-ordering detail deciding a functional result. So a channel
*stages*: `push()` sets the item aside and the channel's own `sample()` commits it, and the channel
is declared before both peers so that commit runs first in every sweep. An item pushed anywhere in
cycle *c* becomes readable at the start of cycle *c+1*, whatever order the peers appear in. Each hop
therefore costs exactly one cycle that pysim does not have.

`xsi_channel.h` deliberately depends on nothing but the standard library and the lifecycle base
(`xsi_simobj.h`, split out of `xsi_bfm.h` for exactly this) — an edge model binds models, never
pins, so it needs no Vivado headers and is compiled *and run* under a plain `g++`
(`tests/build/test_xsi_channel.py`). Authoring one is
[Behavioral edges](../custom_hooks/behavioral.md).

### ...and one model may bind both {#spanning}

A model is not restricted to one side. A **converter** binds RTL pins on its fabric side *and* a
channel on its RF side, in one object — which is what a converter is, rather than a boundary model
glued to a separate channel peer. The Python module declares one `BfmModel` per data path, and each
port resolves by its own kind:

```cpp
RfdcAdcMaster s_in(sim.dut(), edge_toy_ports::s_in, adc_rf, /* fmt */, /* words_per_cycle */);
//                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^  ^^^^^^
//                 the boundary port                the behavioral edge
```

See [a module may declare more than one model](../comp_codegen/xsi_tb.md#per-port) for the
declaration and what it refuses.

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

Writing a *new* model is a separate page: [Writing a BFM model](../custom_hooks/bfm_model.md) covers
when one is warranted (usually it is not), why `sample` and `update` must stay split, the `DynParam`
config contract, and the conformance gate a new model owes.

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
`mem_copy` finishes at 2908 inside a 3400-cycle loop. Compare completion cycles, never the total.

## See also

- [XSI Build Rung](./xsi.md) — terminology and the full compile/elaborate/run flow.
- [The mem_copy testbench](../../examples/memcpy/testbench.md) — the generated path end to end.
- [Stream drivers and sinks](../sim/stream_tb.md) — the pysim participants and their BFM twins.
- [Writing a BFM model](../custom_hooks/bfm_model.md) — authoring a sixth model, if you really need one.
