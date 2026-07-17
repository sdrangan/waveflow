---
title: Hardware components
parent: Realization Flows
nav_order: 1
audience: python
api: [SimObj, Component, HwComponent, HostActivated, FreeRunComp, add_endpoint]
summary: "The foundation both flows build on. A HwComponent is a SimObj with typed ports and a behavior — the single source of truth for a hardware block, both the model you simulate and the source for a generated kernel. It is defined by three things: its endpoints, how it is wired, and what it does. The taxonomy then sorts the kinds and hands off to a flow: a plain HwComponent is a simulation-only model, HostActivated maps to the sequential flow, and FreeRunComp (a leaf, or a composite of sub-components) maps to the concurrent flow."
---

# Hardware components

Both flows start from the same object — a **`HwComponent`**. This page is the shared foundation: what a
component is, the three things that define it, and the taxonomy of kinds that sorts each component into
its flow.

## From `SimObj` to `HwComponent`

Everything in a Waveflow design is a [`SimObj`](../sim/) — anything the [simulation](../sim/) schedules,
with the three-phase lifecycle (`pre_sim` → `run_proc` → `post_sim`) and its own concurrent process(es).
Hosts, drivers, sinks, and testbenches are all `SimObj`s, and so is every piece of hardware.

A [`Component`](../../../waveflow/hw/component.py) is a `SimObj` with **structure**: it exposes typed
**endpoints** — the ports it talks to the outside world through — and it can contain **sub-components**
wired together by internal **interfaces**. It is the *connectable node* in the design graph
(`add_endpoint` / `add_comp` / `add_if`).

A [`HwComponent`](../../../waveflow/hw/hw_component.py) is a `Component` that represents **hardware**. On
top of a `Component`'s ports and hierarchy it adds the **synthesis surface** —
[`HwParam`](./parametrization.md) template parameters and a codegen identity
(`cpp_kernel_name`). That is what makes it the **single source of truth for a hardware block**: the
*same class* is both the model you simulate and the source for a generated C++ kernel.

## A component is defined by three things

- **Its interface endpoints** — the typed ports it talks to the outside world through (a stream input,
  a memory-mapped master, an AXI-Lite register map). You declare them on the class.
- **How it is wired** — a component does not call other components directly; its endpoints are **bound**
  to [interfaces](../interface/), which carry transactions to the endpoints of other components.
- **What it does** — its behavior is the **methods on those endpoints** (`get` an incoming transaction,
  `write` an outgoing one), driven from its lifecycle methods.

A minimal example — a **moving-average filter**, `y[n] = ½·(x[n-1] + x[n])`, streaming fixed-size blocks
of `Float32`:

```python
@dataclass
class MovingAvg(HwComponent):
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
is not on the component — it is bound externally, by connecting `x_in` / `y_out` to
[interfaces](../interface/) at the point the component is instantiated.

## The kinds — and the flow each maps to

`MovingAvg` above is a **plain `HwComponent`**: it simulates, but its `run_proc` computes with NumPy, so
it is a *simulation-only model*, not shaped for hardware generation. To make a component
**synthesizable** you subclass one of the execution-model kinds — each has a specialized process shape
that maps cleanly to a hardware pattern — *and* its compute moves into an
[`@synthesizable`](../../../waveflow/hw/synth.py) method in the array-operator idiom.

```
HwComponent              base — a plain HwComponent is a simulation-only model (MovingAvg above)
├── HostActivated        host-launched: implement on_start; runs once per trigger      -> sequential flow
└── FreeRunComp          free-running: implement run_iter (a leaf), OR add sub-components (a composite)
                                                                                        -> concurrent flow
```

There are exactly **two synthesizable kinds**, and each is the entry point to one flow:

- **Plain `HwComponent`** — a behavioral model of hardware Waveflow does *not* generate (a data
  converter, a memory, an RF channel). It never leaves simulation.
- **[`HostActivated`](./sequential.md)** — the host launches it over a register map;
  writing `ap_start` runs its `on_start` once (read inputs, compute, write outputs, return). Use it for
  invocation-style accelerators. This is the **[sequential flow](./sequential.md)**.
- **[`FreeRunComp`](./concurrent.md)** — a free-running component. A **leaf** implements
  `run_iter` (one firing; the base loops it forever) and lowers to a single `hls::task`; a **composite**
  has no body of its own — it wires sub-components, and each becomes its own `hls::task`. A leaf is just
  the 1-task case of a composite, so they are **one class** (`CompositeComp` is an alias for
  `FreeRunComp`, kept only for readable declarations). This is the **[concurrent flow](./concurrent.md)**.

> *Which* lifecycle method you implement is what distinguishes the kinds: `on_start` for a
> host-launched component, `run_iter` for a free-running leaf, and neither for a composite (its
> sub-components do the work). The kind is a property of the **class**, so codegen dispatches on it
> directly rather than inferring the execution model from the shape of the code.

## Next

Pick the kind your design is, and follow its flow:

- **[Sequential (host-activated)](./sequential.md)** — a `HostActivated` component and a sequential Vitis
  testbench, verified in C-sim and co-sim.
- **[Concurrent (free-running)](./concurrent.md)** — a `FreeRunComp` (leaf or composite), verified at RTL
  through an XSI BFM.

**Source of truth:** `waveflow/hw/component.py` (`Component`), `waveflow/hw/hw_component.py`
(`HwComponent`), `waveflow/hw/hw_hostactivated.py` (`HostActivated`), `waveflow/hw/hw_freerun.py`
(`FreeRunComp`), `waveflow/hw/hw_composite.py` (`CompositeComp` = `FreeRunComp`).
