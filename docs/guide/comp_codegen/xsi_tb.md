---
title: XSI testbench in HLS
parent: Module Code Generation
nav_order: 7
audience: hls
api: [tb_top_spec, render_tb_harness, render_tb_main, render_ports_h, render_rtl_f, bfm_model]
summary: "How a testbench FreeRunMod graph is realized as a cycle-based XSI BFM: tb_top_spec walks the graph, each participant maps to a pre-written C++ BFM model via bfm_model(), and render_tb_harness plus render_tb_main emit a harness that drives the elaborated RTL in xsim. The target is sequential_xsi_tb, and it exists because Vitis cannot co-simulate an ap_ctrl_none DUT."
---

# XSI testbench in HLS

A free-running DUT has no `ap_start` / `ap_done`, so **Vitis cannot co-simulate it** — there is
nothing to wrap. Verification instead drives the elaborated RTL directly, cycle by cycle, through
**XSI** (the Xilinx Simulator Interface): `xelab -dll` builds the design into `xsimk.dll`, and a C++
harness pumps the clock and models every port.

The target is `sequential_xsi_tb`. It is generated, not hand-written.

## The testbench is a graph, not a program

This is the part that surprises people. A `sequential_vitis_tb` comes from a `SeqTB`'s `main()` — a
*body*. An XSI testbench comes from a **component graph**:

```python
@dataclass
class StateAccumTB(FreeRunMod):
    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    def __post_init__(self):
        super().__post_init__()
        self.dut    = StateAccum(...)
        self.driver = StreamDriver(sim=self.sim, bitwidth=w, in_bundle="vectors/s_in")
        self.sink   = StreamSink(sim=self.sim, bitwidth=w, out_bundle="vectors/m_out")
        for c in (self.dut, self.driver, self.sink):
            self.add_comp(c)
        # ... add_if the StreamIFs wiring driver -> dut -> sink
```

The reason is stated in `mem_copy_sim.py` and worth repeating: **a function body is code; a component
graph is data** — and only data can be introspected. `tb_top_spec` cannot read statements that have
already executed, so it learns the participants and their wiring from the graph. The same graph then
drives the pysim golden *and* generates the XSI harness — one structure, two backends.

Note `potential_targets`: a testbench is a composite `FreeRunMod`, so it would otherwise inherit
`composite_kernel`. Declaring `SEQUENTIAL_XSI_TB` is what says "this lowers to a harness, not a
kernel".

## Participants map to pre-written models

A testbench participant does not *lower* to C++ — it **maps** to a pre-written, cycle-exact model.
Each declares which via `bfm_model()`:

| Python participant | C++ model | drives |
|---|---|---|
| `StreamDriver` | `AxisMaster` | an AXI-Stream input |
| `StreamSink` | `AxisSlave` | an AXI-Stream output, timestamping each word |
| `MemoryMod` | `FlatMemory` | the arena an AXI-MM slave model serves from |

`render_tb_harness` instantiates those models on the DUT's ports (it `#include`s the
`<top>_ports.h` that `render_ports_h` emitted), and `render_tb_main` writes a main that is just
construct → run *N* cycles → close. Any per-instance configuration rides across as
[`DynParam`](../flows/parametrization.md) member assignments — `s_cmd.in_bundle = "vectors/s_cmd";` —
so adding a knob is a field on the Python class plus a public member on the C++ model, with no
generator change.

## The scenario lives in files, not in the C++

Inputs and expected outputs are written once as **burst bundles** under `vectors/`, and both backends
read them: pysim's `StreamDriver` loads the same bundle in `pre_sim` that the XSI `AxisMaster` loads,
and a `MemoryMod`'s `load_segs` / `dump_segs` seed and dump the arena the same way.

The consequence is that the generated C++ contains **no golden data and no checking**. The run
produces output bundles; Python reads them back and asserts. A testbench that restated its vectors in
C++ would be a second source of truth for what the design is supposed to do.

## Building and running

```
xvlog -f rtl_<top>.f          # the RTL from a prior csynth
xelab work.<top> -dll -s <top>    # -> xsim.dir/<top>/xsimk.dll
g++ ... <tb>.cpp xsi_loader.cpp -o <tb>.exe
./<tb>.exe
```

`render_rtl_f` generates the `.f` file listing the csynth output, so the RTL list is derived rather
than maintained. Two prerequisites bite if missed: the RTL must exist (a prior `csynth_design`), and a
cached `xsimk.dll` will be reused, so a stale elaboration can silently test yesterday's design.

## The gates are exact

XSI gate assertions are **exact cycle counts, not bounds**. A count that moves is either a real
regression or a real improvement, and both deserve a human look — an inequality would silently absorb
the first kind.

Functional gates are exact too, and the discriminator is chosen so failure is loud. `examples/state_toy`
feeds five all-ones vectors through a running total and requires `1,2,3,4,5`: a design whose
[state](../memory/hwstate.md) did not persist would emit `1,1,1,1,1` and C-synthesize identically.

## See also

- [Free-running kernel in HLS](./freerunning.md) — the DUT this harness drives.
- [Concurrent (free-running)](../flows/concurrent.md) — the flow end to end.
- [Testbench](./testbench.md) — the other testbench target, `sequential_vitis_tb`, from a `SeqTB` body.
