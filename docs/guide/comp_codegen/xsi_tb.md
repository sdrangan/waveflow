---
title: XSI testbench in HLS
parent: Module Code Generation
nav_order: 8
audience: hls
api: [tb_top_spec, render_tb_harness, render_tb_main, render_ports_h, render_rtl_f, bfm_model, resolve_bfm_model, declares_hook]
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
Each declares which via [`bfm_model()`](../custom_hooks/bfm_model.md), a **documented hook** on
`HwModule` and the exact peer of `kernel_task()`: one hands over a pre-written artifact for a module
*inside* the cut, the other for a module *outside* it.

(It was a duck-typed convention until recently — the walk found participants with
`hasattr(c, "bfm_model")`. That probe no longer works, and could not: now the hook exists on the base
class it answers `True` for every module including the DUT. Declaration is detected by identity
against the base, via `declares_hook()`.)

| Python participant | C++ model | drives |
|---|---|---|
| `StreamDriver` | `AxisMaster` | an AXI-Stream input |
| `StreamSink` | `AxisSlave` | an AXI-Stream output, timestamping each word |
| `MemoryMod` | `FlatMemory` | the arena an AXI-MM slave model serves from |

`render_tb_harness` instantiates those models on the DUT's ports (it `#include`s the
`<top>_ports.h` that `render_ports_h` emitted), and `render_tb_main` writes a main that is just
construct → run *N* cycles → close. Any per-instance configuration rides across as
[`DynParam`](../flows/parametrization.md#dynparam) member assignments — `s_cmd.in_bundle = "vectors/s_cmd";` —
so adding a knob is a field on the Python class plus a public member on the C++ model, with no
generator change. (Watch the falsy-value trap when you do —
[Writing a BFM model](../custom_hooks/bfm_model.md#config-contract).)

### A module may declare more than one model {#per-port}

`bfm_model()` returns one `BfmModel` or a **sequence** of them. One is the common case and is what
every table row above is. Several is what a module needs when its C++ realization is more than one
object — and two facts turned out not to be per-*module*:

- **The class is per data path.** For AXI-Stream the role fixes the direction but the participant
  names the class, so a single declaration cannot give a converter's receive port one model and its
  transmit port another.
- **The constructor shape is per port.** Each port is resolved *by its own kind*: an endpoint facing
  a DUT boundary port contributes `sim.dut(), <ns>::<port>`; an endpoint on a
  [behavioral edge](../interface/behavioral.md) contributes that edge's channel variable.
- **A `ports` entry may be a tuple — a port group** — which resolves to a **single** constructor
  argument, `sim.dut(), {<ns>::<a>, <ns>::<b>}`. That is what a model spanning a *variable* number of
  like ports needs: an `n_ch` converter presents one AXIS port per channel and one model owns them
  all, because the RF edge behind them carries every channel in one block. Every member of a group
  must resolve the same way, and a group of **one** renders as the bare port name — so a design whose
  shape did not change generates the C++ it always did.

```python
def bfm_model(self):
    return (BfmModel("RfdcAdcMaster", ports=(("rx_stream_0", "rx_stream_1"), "rx_rf"), extra_args=(...)),
            BfmModel("RfdcDacSlave",  ports=(("tx_stream_0", "tx_stream_1"), "tx_rf"), extra_args=(...)))
```

Each of those is **one object spanning the cut** — RTL pins on the fabric side, a channel on the RF
side — not a boundary model glued to a separate channel peer. Which is why the second walk skips a
side the boundary walk already claimed: emitting a peer as well would put two objects on one edge,
and they would disagree about what crossed it.

Port order **is** constructor order, and the ports resolve in that order, so the declaration has to
match the C++ signature. Nothing checks that pairing at generate time; what does check it is a test
that reads the signature back out of the header
(`tests/build/test_bfm_per_port.py::TestEmittedCtorMatchesTheHeader`).

A named port that is wired to *neither* a DUT boundary port nor a behavioral edge is refused: there
is no argument the generator could write for it, and guessing one binds the wrong object.

Ordinary participants are unaffected — a single `BfmModel` whose ports are all boundary ports
resolves exactly as it always did.

## `tb_top_spec` has two walks {#two-walks}

The walk above iterates **`dut.boundary`** — one model per RTL port — and that spine is what makes
"did we cover every port?" structural rather than a review question. It is also blind by
construction: an edge with no DUT port on either end emits nothing, and was not rejected so much as
*invisible*.

So there is a second walk, over the TB's own interfaces. The partition is on the **interface**, so
nothing can be claimed twice or missed:

| the interface has | is | walk 1 or 2 | emits |
|---|---|---|---|
| at least one endpoint on the DUT boundary | a boundary edge | **1** | one BFM per DUT port |
| neither endpoint on the DUT boundary | a [behavioral edge](../interface/behavioral.md) | **2** | one channel + its two peer models |
| an endpoint inside the DUT that is not a boundary port | a graph error | — | a refusal |
| an endpoint on no testbench child at all | a graph error | — | a refusal |

The last two rows are the point of the change as much as the second walk is: those cases used to be
**silent no-ops**, and the temptation they created was to collapse an edge's far peer into a file
read by the neighbouring model. That is precisely the invariant violation this whole flow exists to
prevent — *the pysim graph and the XSI graph must have the same nodes*. An edge that reaches nowhere
useful is now an error that names what it reached.

Two consequences worth knowing before you hit them:

- **A channel is declared before both of its peers** — in the member list, in the constructor's
  initializer list, and in the participant registration. All three, because declaration order *is*
  construction order and construction order is what puts the channel's `sample()` first. See
  [Behavioral edges](../interface/behavioral.md#why-a-queue-and-not-a-direct-call).
- **A module cannot yet sit on both a boundary port and a behavioral edge.** `bfm_model()` names one
  C++ class for the whole module, and the two bindings have different constructor shapes
  (`(dut, prefix, …)` versus `(channel, …)`). It is refused with that sentence rather than emitted
  wrongly; resolving it needs per-port resolution in `BfmModel`.

A graph with no behavioral edge emits exactly what it did before — byte for byte, including the
`#include "xsi_channel.h"` that only appears when a channel does.

## Two questions, two targets

| target | scope | asks |
|---|---|---|
| `sequential_xsi_tb` | per **graph** | does this whole testbench lower to a harness? |
| `xsi_bfm_model` | per **module** | could *this* module be realized as a model beside a top? |

They are not the same question, and a design can answer the second yes for every participant and
still fail the first — the graph adds questions a module cannot answer alone (is there exactly one
DUT? is every DUT port driven?). The per-module one takes an optional `crossing=` naming the endpoints
that cross the cut; with none given it judges against **every** registered endpoint, which is the
strictest and the only cut-independent answer.

### What `check` can and cannot tell you

The two are not the same kind of verdict, and the difference is load-bearing:

- `composite_kernel` is **derived**. Gate 4 runs the real extractor and converts its raise into a
  verdict, so it answers with rules nobody restated — there is no second copy to drift.
- `xsi_bfm_model` is **resolved**. Four lookups: the hook is declared, the named C++ class exists in
  `xsi_bfm.h`, its `ports` cover every crossing endpoint, and each endpoint has a dual in the
  [protocol × role table](../build/bfm.md#bfm-duals).

That is the complete list. `(True, None)` means **resolvable**, not correct: nothing compares your
Python module's behavior to the C++ model's, and nothing static can. Closing that gap is a
[byte-identical vector gate](../custom_hooks/bfm_model.md#conformance) and nothing
else.

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
- [Writing a BFM model](../custom_hooks/bfm_model.md) — the authoring page for the `bfm_model()` hook.
