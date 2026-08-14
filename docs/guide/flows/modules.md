---
title: Hardware modules
parent: Hardware modules and Flows
nav_order: 1
audience: python
api: [SimObj, HwModule, HostActivated, FreeRunMod, add_endpoint, kernel_task, bfm_model, declares_hook]
summary: "The foundation both flows build on. A HwModule is a SimObj with typed ports and a behavior — the single source of truth for a hardware block, both the model you simulate and the source for a generated kernel. It is defined by three things: its endpoints, how it is wired, and what it does. The taxonomy then sorts the kinds and hands off to a flow: a plain HwModule is a simulation-only model, HostActivated maps to the sequential flow, and FreeRunMod (a leaf, or a composite of sub-components) maps to the concurrent flow. Kind is only one of three axes: kind is how the body is invoked (a class fact), hooks are how it is realized (kernel_task / bfm_model), and the cut decides which hook applies (a build choice)."
---

# Hardware modules

Both flows start from the same object — a **`HwModule`**. This page is the shared foundation: what a
module is, the three things that define it, and the taxonomy of kinds that sorts each module into
its flow.

## From `SimObj` to `HwModule`

Everything in a Waveflow design is a [`SimObj`](../sim/) — anything the [simulation](../sim/) schedules,
with the three-phase lifecycle (`pre_sim` → `run_proc` → `post_sim`) and its own concurrent process(es).
Hosts, drivers, sinks, and testbenches are all `SimObj`s, and so is every piece of hardware.

A [`HwModule`](../../../waveflow/hw/hw_module.py) is a `SimObj` with **structure** that represents
**hardware**. On top of a `SimObj` it adds two things:

- **Connectable structure** — typed **endpoints** (the ports it talks to the outside world through) and
  optional **sub-components** wired together by internal **interfaces**. It is the *connectable node* in
  the design graph (`add_endpoint` / `add_comp` / `add_if`).
- **A synthesis surface** — [`HwParam`](./parametrization.md) template parameters and a codegen identity
  (`cpp_kernel_name`). That is what makes it the **single source of truth for a hardware block**: the
  *same class* is both the model you simulate and the source for a generated C++ kernel.

(A `HwModule` can be a single leaf or, following SystemC, the hierarchical top that contains many
sub-modules — the same class serves both, which is why "module" rather than "component" is the honest
name.)

## A module is defined by three things

- **Its interface endpoints** — the typed ports it talks to the outside world through (a stream input,
  a memory-mapped master, an AXI-Lite register map). You declare them on the class.
- **How it is wired** — a module does not call other modules directly; its endpoints are **bound**
  to [interfaces](../interface/), which carry transactions to the endpoints of other modules.
- **What it does** — its behavior is the **methods on those endpoints** (`get` an incoming transaction,
  `write` an outgoing one), driven from its lifecycle methods.

A minimal example — a **moving-average filter**, `y[n] = ½·(x[n-1] + x[n])`, streaming fixed-size blocks
of `Float32`:

```python
@dataclass
class MovingAvg(HwModule):
    cpp_kernel_name: ClassVar[str | None] = "moving_avg"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.x_in  = StreamIFSlave( name=f"{self.name}_x_in",  sim=self.sim, bitwidth=32)
        self.y_out = StreamIFMaster(name=f"{self.name}_y_out", sim=self.sim, bitwidth=32)
        self.add_endpoint(self.x_in)                       # <- endpoints
        self.add_endpoint(self.y_out)

    def run_proc(self) -> ProcessGen[None]:                # <- behavior
        xprev = 0.0
        while True:
            x = yield from self.x_in.get(Samples)          # one block in
            xv = x.val
            y = 0.5 * (np.concatenate(([xprev], xv[:-1])) + xv)
            xprev = xv[-1]
            yield from self.y_out.write(Samples(y))        # one block out
```

Two endpoints declared in `__post_init__`; the behavior a loop over them in `run_proc`. The **wiring**
is not on the module — it is bound externally, by connecting `x_in` / `y_out` to
[interfaces](../interface/) at the point the module is instantiated.

## The kinds — and the flow each maps to

`MovingAvg` above is a **plain `HwModule`**: it simulates, but its `run_proc` computes with NumPy, so
it is a *simulation-only model*, not shaped for hardware generation. To make a module
**synthesizable** you subclass one of the execution-model kinds — each has a specialized process shape
that maps cleanly to a hardware pattern — *and* its compute moves into an
[`@synthesizable`](../../../waveflow/hw/synth.py) method in the array-operator idiom.

```
HwModule              base — a plain HwModule is a simulation-only model (MovingAvg above)
├── HostActivated        host-launched: implement on_start; runs once per trigger      -> sequential flow
└── FreeRunMod          free-running: implement run_iter (a leaf), OR add sub-components (a composite)
                                                                                        -> concurrent flow
```

There are exactly **two synthesizable kinds**, and each is the entry point to one flow:

- **Plain `HwModule`** — a behavioral model of hardware Waveflow does *not* generate (a data
  converter, a memory, an RF channel). It never leaves simulation.
- **[`HostActivated`](./sequential.md)** — the host launches it over a register map;
  writing `ap_start` runs its `on_start` once (read inputs, compute, write outputs, return). Use it for
  invocation-style accelerators. This is the **[sequential flow](./sequential.md)**.
- **[`FreeRunMod`](./concurrent.md)** — a free-running module, in one of two shapes. A
  **standalone** one implements `run_iter` (one firing; the base loops it forever) and lowers to a
  single `hls::task`; a **composite** has no body of its own — it wires sub-components, and each becomes
  its own `hls::task`. A standalone module is just the 1-task case of a composite, so they are
  literally **one class** — there is no separate composite type; the top level of a design or a
  testbench is a `FreeRunMod` too. This is the **[concurrent flow](./concurrent.md)**.

> *Which* lifecycle method you implement is what distinguishes the kinds: `on_start` for a
> host-launched module, `run_iter` for a standalone free-running one, and neither for a composite
> (its sub-components do the work). The kind is decided by **content** — a `run_iter` body vs
> sub-components — so codegen dispatches on it directly rather than inferring the execution model from
> the shape of the code.

## Kind is not the only axis

The kinds above answer one question: **how is this module's body invoked?** That is a class fact —
host-launched or free-running — and no build changes it.

There are two more axes, and keeping them apart is what lets one module serve more than one design.

| axis | question | decided by |
|---|---|---|
| **kind** | how is the body *invoked*? | the class (`HostActivated` / `FreeRunMod`) |
| **hooks** | is there a *pre-written* artifact for it? | the module, by declaring one |
| **cut** | *which* artifact applies? | the **build** |

### Hooks: the three pre-written realizations

A module's artifact is either derived from its Python or handed over ready-made. Handing it over is a
**hook**, and there are three — all optional:

| hook | declares | realized as |
|---|---|---|
| [`kernel_task()`](../custom_hooks/writing.md) | "my `hls::task` body is *X*" | a task **inside** the generated top |
| [`rtl_module()`](../comp_codegen/rtl_module.md) | "my **Verilog** is *Z*" | an RTL module **beside** the generated top |
| [`bfm_model()`](../custom_hooks/bfm_model.md) | "my cycle model is *Y*" | an `XsiSimObj` **beside** the top |
| *none* | nothing — there is no pre-written artifact | **nothing is emitted**: a pysim-only node |

A module may declare any, all, or none. **None is not an error** — it is a plain `HwModule`
that never leaves simulation, and that is a *finding* from `check`, not something the class states
about itself. (Overriding is the declaration: the base `bfm_model()` raises, and `declares_hook()`
detects the override by identity, exactly as `_kind()` detects a `run_iter` override.)

The third row is easy to read past, so it is worth naming a real one. The canonical pysim-only nodes
are the [RF environment](../rf/) participants — `RfDataSource` and `RfDataSink`, and today the
`Rfdc` converter with them:

```pycon
>>> check(RfDataSource, "xsi_bfm_model")
(False, 'RfDataSource declares no bfm_model() hook, so it has no pre-written cycle model ...')
>>> potential_targets(RfDataSource)
frozenset()
```

They exist in the Python graph and nowhere else, and asking `check` is how you find that out. Note
this is a statement about *today's build*, not about the class: a module acquires a hook when
somebody writes one, and nothing else about it changes.

Note what a hook is *not*: it is not a kind. `MemRStream` hands over an entirely hand-written
`hls::task` body and lives **inside** a synthesized kernel; `StreamDriver` hands over an entirely
hand-written cycle model and lives **outside** one. Extracted-vs-pre-written
([`comp_codegen`](../comp_codegen/) vs [`custom_hooks`](../custom_hooks/)) is a real axis, and it is
not this one.

### The cut is a build choice, not a class fact {#the-cut}

Consider a design of three modules. In one synthesis the DUT is `mod1` alone and `mod2`/`mod3` are
testbench models; in another all three are inside the DUT. **Nothing about `mod2` changed** — only
where the boundary was drawn.

![The same three modules under two cuts. In cut A the boundary encloses mod1 alone, so mod2 and mod3 are realized outside it through bfm_model(); in cut B the boundary encloses all three, so each is realized inside it through kernel_task(). The modules themselves are identical in both panels.](./figures/design_cut.svg)

So no module declares which side it is on, and the framework does not either:

- The **boundary is derived**. A child endpoint not bound to one of the composite's internal
  interfaces *is* a boundary port. Only the external port *names* are declared, and only because
  local names collide (two children both call their memory port `m_mem`).
- The **cut is an argument**. `tb_top_spec(tb, dut=...)` names which child is synthesized; discovery
  is the default, not the mechanism.
- **The cut decides which hook is consulted** — inside the top, `kernel_task()`; beside it in the
  same design, [`rtl_module()`](../comp_codegen/rtl_module.md); outside the design, `bfm_model()`.

This is why "is this a DUT or a testbench participant?" is not a property you will find on any class.
Asking it of a *(module, cut)* pair is what `check(mod, target)` is for. See
[Concurrent flow — the DUT/TB boundary](./concurrent.md#dut-tb-boundary) for what re-cutting
costs in practice, which as of today is more than it should.

## Next

Pick the kind your design is, and follow its flow:

- **[Sequential (host-activated)](./sequential.md)** — a `HostActivated` module and a sequential Vitis
  testbench, verified in C-sim and co-sim.
- **[Concurrent (free-running)](./concurrent.md)** — a `FreeRunMod` (standalone or composite), verified
  at RTL through an XSI BFM.

**Source of truth:** `waveflow/hw/hw_module.py` (`HwModule`), `waveflow/hw/hw_hostactivated.py`
(`HostActivated`), `waveflow/hw/hw_freerun.py` (`FreeRunMod` — standalone or composite).
