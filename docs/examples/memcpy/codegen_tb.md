---
title: Testbench codegen
parent: Memory Copy
nav_order: 5
---

# Testbench codegen — the graph becomes an XSI harness

The [DUT codegen](./codegen_dut.md) lowered the *design* graph to an RTL top. The **same idea** lowers
the *testbench* graph — the `MemCopyTB` from the [Testbench](./testbench.md) page — to a cycle-accurate
harness that drives that RTL top. One graph, two backends: run it in Python, or walk it to C++.

## What XSI and a "harness" are

The synthesized DUT is Verilog RTL. **XSI** (Xilinx Simulator Interface) loads that RTL into a
simulation shared library (`xsim.dir/mem_copy/xsimk.dll`) that C++ can step one clock at a time and
poke/peek at its ports. XSI gives you the *DUT*; it gives you nothing to drive it with.

A **harness** is that missing half: cycle-accurate C++ models — a **BFM** (bus-functional model) per
DUT port — that drive real AXI-Stream and AXI-MM handshakes into the RTL, plus the clock loop that
steps everything. Because the DUT is free-running (`ap_ctrl_none`), there is no start/done to wait on —
so Vitis C/RTL cosim cannot drive it, and this generated XSI harness is how it is verified at all.

## Each participant maps to its BFM twin

Verifying the generated RTL means each testbench participant maps to its **BFM twin** — a C++ model
that drives real handshakes through XSI. The mapping is the participant's own declaration
(`bfm_model()`), not a table someone maintains:

| pysim participant | XSI model | drives |
|---|---|---|
| `StreamDriver` | `AxisMaster` | the `s_cmd` AXI-Stream |
| `StreamSink` | `AxisSlave` | the `s_done` AXI-Stream |
| `MemComponent` | `FlatMemory` + `AxiMmReadSlave` / `AxiMmWriteSlave` | the two `m_axi` bundles |

Note the memory expands to **three** C++ objects: one arena plus a slave model per bundle. In pysim a
crossbar is one interface; at RTL there is no crossbar, so each bundle needs its own slave and both
serve the same `FlatMemory`. The generator works that out from the graph.

The BFM library itself (`waveflow/build/xsi/xsi_bfm.h`) is **framework** — the models know nothing
about `mem_copy`. Each obeys the same `sample` / `update` / `drive` phase split as pysim's
`pre_sim`/`run_proc`/`post_sim`, so a beat decided from values sampled *before* the rising edge is
applied *after* it. [The BFM models](../../guide/build/bfm.md) cover this in full.

## Walking the graph to the harness

`tb_top_spec` walks `MemCopyTB` — the participants, their `bfm_model()` twins, the port bindings — and
`render_tb_harness` emits the C++. **Every line is generated:**

- `xsi/mem_copy_tb_harness.h` — the models, their wiring to the RTL ports (via
  `xsi/mem_copy_ports.h`, the DUT's port map from [DUT codegen](./codegen_dut.md)), the lifecycle
  phases, and the fixed-cycle run loop;
- `xsi/mem_copy_bfm_tb.cpp` — the entire `main`, which is just construct-run-close:

```cpp
int main() {
    mem_copy_tb::Harness h("mem_copy_bfm.wdb");
    h.run(3400);           // a fixed cycle bound, comfortably past the ~2908 completion
    h.close();
    return 0;
}
```

There is **no golden in the C++** — it runs and it dumps. Every value crossing the boundary is a burst
bundle: `vectors/{s_cmd,mem_in,golden}` written before the run, `vectors/{out,s_done}` written by it.
Correctness is checked back in Python at the [RTL rung](./rtlsim.md), from those dumped bundles.

## Building it

`codegen_tb` emits the harness, the main, and the scenario constants — after `codegen_dut`, since the
harness includes the DUT's port map:

```bash
python examples/mem_copy/mem_copy_build.py --through codegen_tb
```

```
codegen_dut: ...                                          # runs first (dependency)
codegen_tb:
    xsi\mem_copy_tb_harness.h
    xsi\mem_copy_bfm_tb.cpp
    RUNNING...
generated TB xsi/mem_copy_vectors.h + xsi/mem_copy_tb_harness.h + xsi/mem_copy_bfm_tb.cpp
    PASSED
```

The only hand-written half of the whole RTL testbench is **two Python functions**:
`write_mem_copy_xsi_bundles` (the scenario — a one-line wrapper over `MemCopySim.write_scenario`) and
`check_mem_copy_xsi_outputs` (the golden). Everything else — harness, `main`, ports, models — is
generated or framework. That is the same shape the [sequential flow](../../guide/flows/sequential.md)
has: Python writes the inputs, the kernel and testbench are generated, the toolchain runs them, Python
checks the outputs.

## Next

[RTL simulation](./rtlsim.md) — synthesize the top, run the harness through XSI, and compare the cycle
count to pysim.
