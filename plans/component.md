 # New Component Design

 ## Background 

 Up to now, we have been using Waveflow to design individual Vitis kernels connected to simple testbenches.  We attempted to extend this model to include Vitis kernels with the DATAFLOW architecture, but the DATAFLOW architecture turned out to be extremely limited.  In general, we need a method to express multiple concurrent processes with in a general architecture.
 Additionally, we need a path to be able to generate code for various outputs:

 - Synthesizable Vitis kernels that generate Vitis IP blocks
 - Vitis testbenches that describe sequential programs that exercises the Vitis kernel methods.  These can be used in C-Sim and RTL co-sim
 - Vivado IPI that is a collection of Vitis kernels plus custom Vivado blocks (like AXI interconnect)
 - SystemC modules that incorporate one of more Vitis kernels or Vivado IPI

 Whatever the code generation target, theere should be a consistent way to simulate these different objects in python.

This document describes some notes on how to achieve this.

## Hierarchical HwComponent

### Structure

To meet the goals, the first major change is to enable HwComponents with sub-components. One possible example syntax for a simple load-compute-store module would be like:

```python

class Load(HwComponent):
    ...

class Interleaver(HwComponent):

    def __post_init__(self):
        # Model each stage as a sub-component
        self.load = LoadStage(...)
        self.compute = ...
        ...

        
        self.add_comp(self.load)
        self.add_comp(self.compute)
        ...

        # Create the interfaces from load->compute and compute->store
        self.lc_stream = StreamIF(...)
        self.cs_stream = StreamIF(...)
        self.add_if( self.lc_stream )
        self.add_if( self.cs_stream )

        # Bind the components
        self.lc_stream.bind('master', self.load)
        self.lc_stream.bind('slave', self.compute)
        ...
```

Here, we introduce two new methods:

- `add_comp`:  Adds a sub-component.  This could add the component to a list or dictionary, say `self.sub_comp`
- `add_if`:  Adds an internal interface.  Note that this method is **not** for external interface endpoints.  Rather it is for interfaces between the sub-components. 

Note these are analogous to the `add_endpoint` method now.
So the model is extended so that `HwComponent` classes own
methods to have sub-components and interfaces between them.

**Note**:  We need to figure out how to name sub-components.  The same sub-component in two different instances of the higher object, will ahve the same name.  We should keep this in mind -- HwComponent names are not globally unique.   It may not be a problem.  For example, all components are registered in a global list of simulation objects, (`Simulation._sim_objs`).   Right now, this is a list, not a named dictionary, so probably OK.

### Top-level interface endoints

To connect a top-level interface endpoint to a sub-component, the 
sub-component should simply receive the top-level interface enpoint as an argument.   Suppose, for example, the top-level has a stream

```python

class Interleaver(HwComponent):
    def __post_init__(self):

        # Create a top level interface endpoint to receive 
        # command packets
        self.s_in  = StreamIFSlave(...)
        self.add_endpoint(self.s_in)

        # Pass the interface endpoint to the load stage,
        # so that it can receive the packets
        self.load = Load(self.s_in, ...)

```

In this simple example, the load can pend on the command:

```python
class Load(HwComponent):

    def run_proc(self) -> ProcessGen[None]:
        while True:
            cmd_hdr: Cmd = yield from self.s_in.get(Cmd)
            ...
```

## Active and passive sub-components

Sub-components come in two flavors, and the distinction drives codegen:

- **Active** components (`Load`, `Compute`, …) own a `run_proc`.  That process becomes an HLS thread
  (composite kernel), an `SC_THREAD` (SystemC), or a Vitis function body — and the same body is what
  pysim runs.  An active component generates a *process*.
- **Passive** components (memory, FIFOs) have no independent process; they *respond* to transactions
  on their interfaces.  A passive component generates *storage / structure* (BRAM, DRAM, a behavioral
  slave), not a thread.

Keeping this explicit avoids conflating "a stage" with "a memory" in the codegen rules below.

## Memory Sub-Components

Memory is the canonical **passive** sub-component, and an important one.  Like every `HwComponent` it
plays a **dual role**:

- in **pysim** it is a behavioral model — a backing array plus the transactional read/write semantics
  on its interfaces;
- in **codegen** it lowers to the right *physical* memory for the target: on-chip **BRAM**
  (`ARRAY_PARTITION`) when it lives inside a kernel, **external DRAM** reached over `m_axi` when it is
  a top-level memory, or a **behavioral AXI-MM slave** in a SystemC/RTL testbench.

We support two `Memory` types:

- **Raw memory components** — expose direct AXI/MM interfaces (read/write by element or slice), and
  model BRAM or external DRAM.  Parameters: size, element type, partitioning.  These should land on
  the single element-coordinate memory interface (`read_array_slice` / `write_array_slice`, `Region`)
  rather than inventing a third access style — align with the memory-unification direction.

- **Stream-wrapped memory components** — wrap a raw memory with one or more command streams (each
  stream carries a transaction, e.g. "write these `n` words to addr `a`"), plus **arbitration** across
  the streams.  This is the form that matters for `hls::task` synthesis: the DTLP pattern funnels all
  memory access through a dedicated read-task / write-task that *owns* the `m_axi` bus, and the arbiter
  is where a structural `n_masters` parameter lives (the poster child for class-level parameterized
  ports — see Note 2 below).


## Python Simulation

Python simulation of hierarchical `HwComponent` classes is straightforward.  Since each `HwComponent` is a `SimObj`, all sub-objects will be registered in the global `Simulation` `_sim_objs` list.  Hence, in the life cycle (pre_sim, run_proc, and post-sim), all sub-components will automatically be called.  

**Note**:  I don't think a child `HwComponent` should assume that its parent `pre_sim` has already ran, or vice versa.  So, we may need to think about order carefully if that is needed. 


## Code generation

We next discuss how we generate various Vitis and Vivado code from `HwComponent` classes.

### What decides the codegen output: capability, target, and role

A key question is: given a `HwComponent` class, what determines whether it becomes a
synthesizable Vitis kernel, a composite IP, a SystemC module, etc.?  The decision is **not** a
single flag on the class.  Instead it splits into three separate concepts.

- **Capability** — *what a component CAN be.*  Intrinsic to the class, and **computed**, not
  declared.  The `vitis_synthesizable()` method (below) is exactly this: it inspects the endpoints
  and sub-components and returns whether the class can be a synthesizable kernel.  We generalize it
  to a small set of capability queries — `can_synthesize()`, `can_testbench()`, `can_systemc()`.  A
  component often has several capabilities at once.

- **Target** — *what output we are asking for in a given build.*  Decided by the **`BuildStep`**, not
  the class.  The same class yields different outputs through different steps: `HlsCodegenStep(Load)`
  produces a standalone Vitis IP; `SCCodegenStep(Load)` produces a SystemC module; a testbench step
  produces a C-Sim testbench.  So the same Python class can result in multiple codegen outputs, and
  the choice rests with the step that consumes it.

- **Role** — *what a sub-component becomes inside a given parent build.*  A child's role is
  **derived**, not declared.  The parent's chosen target plus the child's capability determine it.
  If `Interleaver` is built as a composite kernel, `Load` is inlined as a sub-function / HLS thread;
  if built as a system, `Load` is a separate IP wired in IPI.  The same `Load` class takes a
  different role depending on how the parent composes it.

The important consequence: a parent **assigns** its sub-components' roles rather than **reads** them.
A sub-component never pre-declares a target; the parent derives each child's role from its own target
and the child's capability set.  This resolves the concern that a hierarchical component "must know
the targets of its sub-components" — it does, but by assignment, not by lookup.

These interlock in two directions:

- **Bottom-up (capability constrains the parent).**  A parent can only *choose* the composite-kernel
  target if every child `can_synthesize()` and the memory-exclusivity rules hold.  If one child
  cannot (e.g., it is a Vivado IP, or two masters need arbitration on a shared memory), the composite
  option is removed and the parent must be a system.  This is exactly the `vitis_synthesizable()`
  recursion below — the upward constraint.

- **Top-down (target assigns roles).**  Once the BuildStep picks the top's target from its *allowed*
  set, that choice propagates down: composite → inline children; system → separate-IP children.  This
  is the downward assignment.

A useful analogy is a compiler.  A function's *inlinability* is intrinsic (capability); whether it is
*actually inlined* is the caller/optimizer's per-compilation decision (role); and you invoke the
compiler for a specific output such as an object file vs. a shared library (target).  Nobody records
"I am inlined" on the function.

Concretely, for the `Load` stage (capability = synthesizable):

| how it is built                        | Load's role                        |
| -------------------------------------- | ---------------------------------- |
| `HlsCodegenStep(Load)` standalone      | its own Vitis IP                   |
| parent Interleaver → composite kernel  | inlined sub-function / HLS thread  |
| parent Interleaver → system            | separate IP, wired in IPI / SystemC |

Because the target lives on the `BuildStep` and the role is derived, **no `preferred_target` flag is
needed on the class.**  Capabilities (computed) plus the build context — which step, composing which
parent — are fully authoritative.

**Caveat to fold in (from the `hls::task` de-risk).**  The composite-kernel role wraps each
sub-component in an HLS thread (`hls::task`).  We verified on Vitis 2025.1 that `hls::task` + `m_axi`
**csynths fine but cannot be Vitis C/RTL co-simulated** (`COSIM 212-345`; `ap_ctrl_none` cosim only
supports combinational / II=1 / pure-stream).  So a composite kernel that touches memory is real
silicon but is only *verifiable* on the SystemC / Vivado RTL-sim rung, not Vitis cosim.  A composite
kernel whose internal interfaces are pure streams (no `m_axi`) cosims cleanly.  See
`plans/dataflow_mod.md`.

### Interface Endpoints

Vitis is strict about which interface-endpoint methods are supportable, so each endpoint carries
properties that feed the capability queries and the memory-exclusivity rules:

- `vitis_synthesizable`: the endpoint can be represented in Vitis code at all.
- `vitis_mem_read`: binds to a memory through **read-only** methods.
- `vitis_mem_write`: binds to a memory through **write-only** methods.
- `vitis_stream`: realized via a stream (AXI4-Stream or `hls::stream`).

By default these are `False` — fail-closed, so an un-annotated endpoint blocks synthesizability, which
is the safe default.  The read-only / write-only split is not just a label: we construct the AXI-MM
endpoint so it *only exposes* the matching methods (a read endpoint has no `write_slice`).  That makes
the split a **capability restriction** — a mis-use fails in pysim rather than silently diverging from
Vitis — and it is exactly what enforces the "no stage both reads and writes a shared memory port" rule
that keeps a load/store dataflow correct (see `plans/dataflow_mod.md`).  The generated C++ also gets a
`const` pointer for a read endpoint, so a stray write is a compile error on every path.

### Interface lowering

An `Interface` bound between two endpoints (`add_if` + `bind`) is *one* logical connection that
renders differently per target — the same graph edge, three ways:

| interface kind      | pysim               | composite kernel (intra-IP)      | system (inter-IP)          |
| ------------------- | ------------------- | -------------------------------- | -------------------------- |
| stream              | SimPy queue         | `hls::stream` FIFO               | AXI4-Stream                |
| memory (read/write) | backing array + txn | BRAM / `m_axi` to the mem        | `m_axi` → interconnect/mem |
| **block (`SOBIF`)** | ping-pong buffer    | `hls::stream_of_blocks<T[N],2>`  | (compose two IPs; N/A now) |

So the lowering is determined by two things: the interface **kind** (stream vs memory) and the
parent's **role** (composite → on-chip FIFO/BRAM; system → AXI between IPs).  This should reuse the
existing `Interface` class — the internal `add_if` is the same master↔slave transactional connection
already used to wire top-level components in a `Simulation`, just scoped inside a parent.  It is not a
new mechanism; it is the existing one made hierarchical.

### `SOBIF` — the block interface (resident random-access double-buffer)

`SOBIF` is a distinct interface **kind**, not a flag on `StreamIF`, because the semantics genuinely
differ: a stream is sequential element/word FIFO dequeue; a `SOBIF` hands over a whole **block**
(`elem_type = DataArray[T,N]`) with **acquire/release** (`write_lock`/`read_lock`) semantics and a
**random-access** consumer.  It still subclasses `QueuedTransferIF` (reuse the master/slave connect +
SimPy plumbing); the new parts are only block granularity, the lock handover, and the random-access
API.  It lowers to `hls::stream_of_blocks<T[N],2>` (the depth-2 ping-pong).  The producer is the
`write_lock` side (e.g. `Fill`); the consumer is the `read_lock` side (e.g. `Gather`).  **Fill and
Gather MUST be separate components** — the ping-pong overlap requires it (fill block j+1 while gather
reads block j), and the DTLP rule forbids folding either into an `m_axi` owner.

**Throughput is a property the `SOBIF` consumer advertises** (feeds the LT timing model), and gather vs
scatter are NOT symmetric:

- **random-READ consumer (gather `Y[i]=X[P[i]]`)** → `n / min(LW, 2)`.  A BRAM has two physical ports;
  the ping-pong gives the reader *both* (the writer is on the other buffer), so 2 arbitrary reads/cycle
  are FREE (verified in sob3 RTL: dual-port memcore, no replication).  Cap is 2; `LW>2` needs block
  replication.
- **random-WRITE consumer (scatter `Y[P[i]]=X[i]`)** → `n`.  Two arbitrary writes can't be *proven*
  conflict-free (WAW), so Vitis serializes them regardless of ports — unless the index stream carries a
  **permutation guarantee**, which licenses `#pragma HLS DEPENDENCE ... false` → `n/2`.  Reads are
  order-independent; writes are not.  This asymmetry is why the input-side resident block rides the
  free dual-port down but the output-side one does not.

For MEM_DW=64 the interleaver is read-bound at `n` with `LW=2` (measured 295 cyc/job) — the sweet
spot; `LW>2` only helps at MEM_DW≥128.

### The concrete memory-endpoint components

The "stream-wrapped memory" pattern above is realized by two pre-written, reusable `HwComponent`s whose
kernel body is FIXED (the validated sandbox `a2s`/`s2a`), parameterized only by `MEM_DW`:

- **`MemRStream`** — `m_mem: MMIFMaster @port_read` + `s_cmd: StreamIFSlave[MRCmd]` + `m_out:
  StreamIFMaster[word_t]`.  Sole `m_axi` read owner; processes an `MRCmd{byte_addr, n_words}` queue in
  order and bursts words out.  Transparent to what the words *are* — "P then X" is just two queued
  commands; the receiver splits by count.
- **`MemWStream`** — the mirror (`@port_write`, `s_in` word stream → pure-write burst).

A pure-stream **`Sequencer`** (touches no memory) issues the ordered `MRCmd`/`MWCmd` from the app
command and forwards `n_words` to the compute tiles and to a tiny count-driven **`Demux`** (splits
`m_out` into `p_words`/`x_words`).  Because the split counts are the same `n_words` the Sequencer
enqueued, there is a single source of truth and no need for `TLAST`.  **Multi-master arbitration**
(the `n_masters` arbiter) is deferred until 2+ modules share one memory; **`TLAST`/AXI4-Stream framing**
is the later robustness upgrade.  Implementation sequence in `plans/mem_stream_impl.md`.

## Code Generation for Synthesizable Vitis Kernels

A `HwComponent` class is a **synthesizble Vitis kernel** if Waveflow can generat C++ code that Vitis can in turn run through Csynth and hence export as Vitis IP.  To determine if a particular `HwComponent` class is a synthesizable Vitis kernel, each `HwComponent` class 
will expose a class method `vitis_sythesizable()`.  
The method will oeprate as follows:
A  `HwComponent` class's method `vitis_sythesizable()` will return `Ture` if *all* the following conditions are met:

- All interface endpoints are `vitis_synthesizable=True`.
- If there are any sub-components, their `vitis_synthesizable()` ,method returns `True`, and all their interface endpoints must be one of `vitis_stream`, `vitis_mem_read` or `vitis_mem_write`.
- No two sub-components can have a `vitis_mem_write` that is bound to the same Memory or mapped to the same top level memory interface endpoint
- No two sub-components can have a `vitis_mem_read` that is bound to the same Memory or mapped to the same top level memory interface endpoint.

Note that the special case of a `HwComponent` with no sub-components is already working today.  When there are sub-components meeting the above conditions, the code generation should work out.  Basically, each sub-component will generate
a C++ call for the function.  The parent function's call with then wrap that function in an HLS thread.

Note 1:  Vitis' Dataflow architecture will **not** be supported in this initial version as it is too restrictive and fragile.

Note 2 (the default-instance fragility, and the fix):  Today `HlsCodegenStep` reads member-specific
features (the endpoint list) by constructing a throwaway default instance and reflecting on it
(`comp_class(name="_codegen", sim=Simulation())` → `extract_kernel`).  Convenient, but it rests on an
assumption stated in the code itself — *"the default instance is representative"* — which **breaks for
structurally-parameterized components.**  An arbiter whose port count depends on a constructor
parameter (`n_masters`) has a *different endpoint set* per configuration, so a single default instance
does not represent it.

Two kinds of parameter, handled differently:
- **value/width** (stream width, unroll factor): same ports, different types/values → templated
  `.tpp` + `param_supports` variants (already supported).
- **structural** (`n_masters`, lane count): changes the *endpoint set / signature*.  Vitis cannot
  template a variable port count into one kernel (each `m_axi`/`axis` port is a distinct argument), so
  a structural parameter must become a **concrete kernel per value**, and endpoint discovery must run
  on *that* concrete instance, not the default.

The end-state fix mirrors `DataSchema`: make ports **class-level descriptors** (like `IntField`),
with a param-sized **port array** for the structural case (`masters = PortArray(AxiMasterMM,
size=Param('n_masters'))`, analogous to `DataArray`).  Then the structure is introspectable off the
class and only the parameter *values* resolve per concrete instance — retiring the "default instance
is representative" assumption.  This rides along with the symbolic-`Param` unification (the
parameterization plan), not as a separate refactor, and keeps an imperative escape hatch
(`add_endpoint` in an override) for the rare non-declarative component.  In a hierarchy it is mostly
automatic: a child's structural params are *derived from the parent's wiring* (an arbiter's
`n_masters` = the number of masters the parent connects), so introspecting the built parent tree
yields correctly-parameterized children for free.

### Code Generation for Vitis C-Sim Testbenches

This follows the existing `HwTestbench` model: the testbench's `run_proc` is emitted as a
**sequential** C++ `main()` that sets up inputs, calls the DUT, and checks results.  Csim is untimed —
there is no SystemC scheduler — so the TB cannot be concurrent; sequential is the only option (and the
`hls::task` blocking-stream sync is the one concurrency it can drive, for a task-based DUT).

The same generated TB drives **two** checks: **csim** (fast functional) and **Vitis C/RTL cosim**
(single-kernel RTL equivalence), reusing one output — as long as the DUT is cosim-able (`ap_ctrl_hs`,
not free-running-`m_axi`).

The clean structuring is to make the **DUT a sub-component** of the testbench component: the TB owns
the DUT plus the stimulus/checker as its own `run_proc`, and the internal TB↔DUT interfaces lower to
direct function calls / `hls::stream` in the generated `main()`.  This keeps a single hierarchical
model for "kernel + its unit test."

Scope limit worth stating: because csim is sequential, this tier verifies a **single synthesizable
top** (which may itself be an internal DATAFLOW/task composite).  Two *separate* concurrent blocks
interacting cannot be exercised in sequential csim — that is inherently the SystemC/xsim tier below.


### Code Generation for SystemC Threads

`SCCodegenStep` takes a `HwComponent` class and emits an **`sc_module`** (yes — `sc_module` is the
right SystemC unit; a process inside it is an `SC_THREAD`).  This is the tier that verifies what Vitis
cosim cannot: free-running-`m_axi` kernels and genuine multi-block systems, run in **xsim** (not csim —
`SC_THREAD` concurrency needs the SystemC scheduler).

Mapping:
- an **active** component's `run_proc` → an `SC_THREAD` `run()` method (the same behavior body pysim
  runs, re-rendered);
- **synthesizable** sub-components → their exported **Vitis IP RTL**, instantiated as modules inside
  the SC top and wired per the `Interface` graph (so the multi-block topology is described in the SC
  module itself — no Vivado IPI needed *just to simulate*, see the IPI section);
- **endpoints** → SC ports at the boundary: streams to `sc_fifo` / AXI-Stream signals, and a
  **memory** endpoint to an **AXI-MM slave model** (Xilinx AXI VIP, or a ~100-line behavioral slave)
  that answers the kernel's `m_axi` master.  This AXI-MM slave is the one-time harness that amortizes
  across examples.

Data types: `ap_int` / `ap_fixed` are ordinary C++ templates, usable directly in the SC TB's golden
and pack/unpack logic (same types as the kernel → bit-exact by construction, reusing the generated
`DataSchema` serializers).  Only the RTL *port* signals are `sc_bv` / `sc_uint`; convert `ap_* ↔
sc_bv` at that boundary.

Setup (Windows): `xsc` (the xsim SystemC compiler) and the SystemC library ship with Vivado
(`Vivado/bin/xsc`, `Vivado/data/systemc`), so nothing extra installs.  The `xsc → xelab → xsim` flow is
less battle-tested on Windows than Linux and should be smoke-tested before we build on it.

Single-golden guarantee: the sequential csim TB and the SystemC TB both `#include` the same generated
golden/serializer header, and both trace to the pysim golden — so the tiers cannot disagree by
construction.

## Code Generation for Vivado IPI

`IpiCodegenStep` renders the **same `Interface` graph** the SystemC step uses, but for the *build*
path instead of the *sim* path: it emits Vivado IP-Integrator **TCL** that assembles an implementable
block design.  Where SystemC wires the modules for simulation, IPI wires them for synthesis +
implementation (the bitstream) — the same topology rendered for two tools.

From the graph, the TCL:
- instantiates each **synthesizable** sub-component as its exported **Vitis IP** (`HlsCodegenStep` →
  `export_design`);
- instantiates the **Xilinx IP** the system needs — AXI **SmartConnect** / interconnect where multiple
  masters share a memory, a **BRAM / DDR / HBM controller** for the backing memory, platform IP (clock
  wizard, processor-system-reset), plus RFSoC IP (RFDC) when relevant;
- **wires** the connections per the `Interface` bindings (stream → AXI-Stream; memory → AXI-MM through
  the interconnect);
- builds the **address map** from the memory endpoints (each master's view of each memory);
- exposes the top-level endpoints as external ports of the block design.

Why IPI and not a hand-written structural RTL top: once real (often encrypted) Xilinx IP is involved,
IPI handles IP configuration, address maps, and clocking that would be miserable to instantiate by
hand.  IPI is required for the **build**; it is *not* required merely to simulate — the SystemC step
can wire the exported IPs directly for a faster, IPI-free multi-block sim.

A `BuildStep` then drives Vivado to (a) generate the block design + wrapper for synth/impl, and
(b) optionally RTL-simulate the whole IPI system in xsim with a SystemC TB driving only the top-level
ports — the highest-fidelity rung, since it includes the real interconnect, memory controller, and
RFDC.

Open: address-map generation across multiple masters, clock-domain handling, and the exact TCL
templating are still to be worked out.

## The verification ladder

The four codegen outputs are not alternatives — they are **rungs**, cheapest first, and you descend
only when the rung above cannot reach the design:

| rung | output | runs in | reaches |
| --- | --- | --- | --- |
| 0 | pysim | Python | behavior + LT timing (the golden) |
| 1 | sequential C++ TB | Vitis **csim** | algorithm, single top (fast) |
| 2 | *same* C++ TB | Vitis **cosim** | single-kernel RTL == C (`ap_ctrl_hs` only) |
| 3 | SystemC TB | **xsim** | free-running-`m_axi`, multi-block RTL (SC-wired, no IPI) |
| 4 | IPI + SystemC TB | Vivado **xsim** | full system: real interconnect / memory / RFDC |
| — | IPI bitstream | Vivado impl | hardware |

One pysim golden sits behind all of them; each rung checks against it.  "Run csim first" is really
"climb from the cheap end; drop a rung only when the one above can't express the design."
