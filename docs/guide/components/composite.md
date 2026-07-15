---
title: Composite components
parent: Hardware Components
nav_order: 5
audience: python
applies_to: [CompositeComp]
api: [CompositeComp, add_comp, add_if, StreamIF]
summary: "CompositeComp — a hardware block built from smaller ones. It has no body of its own: its sub-components do the work, running concurrently. You register children with add_comp, wire internal dataflow edges with add_if, and expose the children's ports as the composite's own boundary. Walked through a two-stage (2·x)² pipeline of FreeRunComp leaves; the deep concurrent semantics live in the Concurrency section."
---

# Composite components

## What it is

A [`CompositeComp`](../../../waveflow/hw/hw_composite.py) builds a **larger block out of smaller ones**.
It has **no body of its own** — no `run_proc`, no `run_iter`; its **sub-components do the work**, each
running as its own concurrent process. You build one in `__post_init__` with two methods:

- **`add_comp(child)`** — register a sub-component (a `HwComponent`; usually a [free-running
  leaf](./freerun.md)).
- **`add_if(iface)`** — wire an internal **edge** between two children by binding one child's *master*
  endpoint and another's *slave* endpoint to a shared [interface](../interface/).

Its C++ is the **composite top** — one `hls::task` per child plus one channel per internal edge, derived
from the graph (not from an extracted body).

## Example — `(2·x)²` in two stages

Two [free-running](./freerun.md) leaves in a line — a doubler feeding a squarer. `x` and `y` are the
composite's boundary ports; `z` is the internal edge:

```mermaid
flowchart LR
    xin([x])
    subgraph ScaledSquare
        direction LR
        double["double: z = 2·x"]
        square["square: y = z²"]
        double -->|z| square
    end
    yout([y])
    xin -->|x| double
    square -->|y| yout
```

The squarer is the `Square` from [free-running components](./freerun.md), reused unchanged. Its
partner is a doubler with the same shape — one `Vec` in, one `Vec` out per firing, with the compute in
a `@synthesizable` pure function using the array operators
([`examples/toy/toy.py`](../../../examples/toy/toy.py)):

```python
@dataclass
class Double(FreeRunComp):
    """z = x + x, element-wise over one Vec per firing."""

    # NOTE: not "double" — that is a C++ keyword, so it could never be a kernel function name.
    cpp_kernel_name: ClassVar[str | None] = "vec_double"

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.x_in = StreamIFSlave(name=f"{self.name}_x_in", sim=self.sim, bitwidth=32)
        self.z_out = StreamIFMaster(name=f"{self.name}_z_out", sim=self.sim, bitwidth=32)
        self.add_endpoint(self.x_in)
        self.add_endpoint(self.z_out)

    def run_iter(self) -> ProcessGen[None]:
        x = yield from self.x_in.get(Vec)
        yield from self.z_out.write(self.dbl(x))

    @synthesizable
    def dbl(self, x: Vec) -> Vec:
        return x + x                           # z = 2·x
```

The composite just wires them — it declares **no behavior of its own**:

```python
@dataclass
class ScaledSquare(CompositeComp):
    """Composite: y = (2·x)², computed by two concurrent free-running sub-components."""

    cpp_kernel_name: ClassVar[str | None] = "scaled_square"

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()

        # 1. sub-components — each runs its own run_iter loop concurrently
        self.double = Double(name=f"{self.name}_double", sim=self.sim, clk=self.clk)
        self.square = Square(name=f"{self.name}_square", sim=self.sim, clk=self.clk)
        self.add_comp(self.double)
        self.add_comp(self.square)

        # 2. internal edge — double.z_out (master) -> square.x_in (slave).
        #    StreamIF REQUIRES clk: it models transfer latency as nwords/clk.freq, and
        #    QueuedTransferIF.__post_init__ raises "clock must be provided" without it.
        self.z_if = StreamIF(name=f"{self.name}_z_if", sim=self.sim, clk=self.clk, bitwidth=32)
        self.z_if.bind("master", self.double.z_out)
        self.z_if.bind("slave", self.square.x_in)
        self.add_if(self.z_if)

        # 3. boundary — the composite's ports ARE its children's endpoints
        self.x_in = self.double.x_in      # composite input  = doubler's input
        self.y_out = self.square.y_out    # composite output = squarer's output
```

Three things to notice:

- **No parent body.** `ScaledSquare` has no `run_proc`/`run_iter` — `double` and `square` each run as an
  independent process, and defining `run_iter` on a composite is rejected at class-definition time.
- **The edge is a real channel** with backpressure: if `square` falls behind, `double` blocks on
  `write`, and the two stages **overlap**.
- **Boundary ports alias, they don't copy.** `self.x_in = self.double.x_in` makes the composite's input
  *the very same endpoint* as the child's — the hierarchy adds no runtime hop.

> **Every `StreamIF` needs a `clk`.** It models a transfer as `nwords / clk.freq`, so a clock-less
> interface is rejected at construction (`ValueError: clock must be provided for StreamIF`). The
> composite threads its own `clk` down to both children and to the edge, so one clock describes the
> whole block.

> **A stream is a FIFO of words, whatever the payload.** The `Vec` on the `z` edge crosses as four
> sequential beats — it is still a plain FIFO. What needs a stream-of-blocks interface (`SOBIF`) is a
> **random-access consumer**: a stage that must hold a whole block resident and index into it. That,
> along with longer pipelines and multi-input stages, is the [Concurrency](../concurrency/) section.

## What this example claims

It is **real, executed code**: [`tests/examples/test_toy.py`](../../../tests/examples/test_toy.py)
runs `ScaledSquare` in the pysim, checks `y = (2x)²`, and asserts that the composite schedules both
children and that the doubled value really crosses the `z` edge.

As on the [free-running](./freerun.md#what-this-example-claims) page, that is a claim about the
**pysim model**, not about synthesis: the composite top described above (one `hls::task` per child,
one channel per edge) is generated from a graph the toy does not declare — `composite_top_spec` reads
the `ordered_subcomps` / `internal_edges` / `boundary` descriptors that the real composites
(`MemCopy`, `InterleaverCanon` in `examples/interleaver/`) carry. This toy is the *shape* of a
composite and its concurrent behaviour, not a generated block.

## Going deeper

This page is the *shape* of a composite. The deep concurrent semantics — overlap, longer load-compute-store
pipelines, stages with several inputs — are the [Concurrency](../concurrency/) section; how the composite
top is generated and verified is the [realization flows](../flows/).
