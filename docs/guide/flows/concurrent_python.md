---
title: Writing it in Python
parent: Concurrent (free-running)
nav_order: 0
audience: python
api: [FreeRunMod, run_iter, add_comp, add_if, add_state, KernelTask, synthesizable]
summary: "How to describe a free-running module in Python: a LEAF implements run_iter (one firing) and a COMPOSITE has sub-components wired by internal interfaces — body XOR children, never both. Covers what one firing means, why there is no before-the-loop, how to carry state across firings with add_state, and how a composite declares only its boundary port names."
---

# Writing it in Python

A free-running module **runs on its own**. There is no start/done handshake and nothing calls it: the
runtime re-fires it on every new job. In Python that is a [`FreeRunMod`](./modules.md), and it comes
in exactly two shapes.

## Body XOR children

A `FreeRunMod` is either a **leaf** — it implements `run_iter` — or a **composite** — it has
sub-components. Never both, never neither. The kind is decided by content, not by type: there is no
separate composite class, and a leaf is simply the 1-task degenerate case that the same generator
walks.

### A leaf: one firing

```python
@dataclass
class StateAccum(FreeRunMod):
    cpp_kernel_name: ClassVar[str | None] = "state_accum"
    cpp_namespace:   ClassVar[str | None] = "state_accum_impl"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in  = StreamIFSlave( name=f"{self.name}_s_in",  sim=self.sim, bitwidth=32)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=32)
        self.add_endpoint(self.s_in)
        self.add_endpoint(self.m_out)

        self.total = HwState(AccArray())          # storage that outlives a firing
        self.add_state(self.total)

    def run_iter(self) -> ProcessGen[None]:
        x = yield from self.s_in.get(Vec4)
        y = self.accumulate(x, self.total)        # a @synthesizable hook
        yield from self.m_out.write(y)
```

**`run_iter` is one firing, not a loop over jobs.** This is the thing to internalise. The base class
loops it forever in simulation, but that `while` is only the discrete-event stand-in for the runtime
re-firing the task — it is not emitted. So there is **no "before the loop"**: anything you want to
survive between firings cannot be a local, and cannot be an undeclared `self.X` either, because the
[extractor forbids reading mutable instance state](../comp_codegen/extractor.md).

That is what `add_state` is for. `self.total` is declared, so reading it is deliberate rather than
accidental capture, and codegen emits it as a `static` in the task body — the only place persistent
storage can live. See [`HwState`](../memory/hwstate.md).

One shape rule worth knowing early: the hook's result is **named before it is written**
(`y = self.accumulate(...)`, then `write(y)`). A call nested inside `write(...)` is not one of the
extractor's statement shapes.

### A composite: a graph

```python
def __post_init__(self) -> None:
    super().__post_init__()
    self.seq     = Sequencer(...)
    self.rstream = MemRStream(...)
    self.wstream = MemWStream(...)
    for c in (self.seq, self.rstream, self.wstream):
        self.add_comp(c)                    # insertion order == codegen task order

    cmd_if = StreamIF(name=..., sim=self.sim, clk=self.clk, bitwidth=w, framed=True)
    cmd_if.bind("master", self.seq.cmd_out)
    cmd_if.bind("slave",  self.rstream.s_cmd)
    self.add_if(cmd_if)                     # an internal channel
    ...
    self.boundary = ["s_cmd", "m_in", "m_out", "s_done"]
```

A composite has **no body at all** — its children do the work, and it is passive in simulation. What
it declares is the graph: `add_comp` for children, `add_if` for the internal channels between them.

It declares only the **names** of its boundary ports. The endpoints and their order are derived: a
child endpoint not bound to one of the internal interfaces *is* a boundary port. Only names are
needed because local names collide — two children may both call their AXI port `m_mem`. A leaf
declares no boundary at all; it is derived from `kernel_task()`'s signature.

## Handing over a hand-written body

A leaf whose body is not extractable — anything owning an `m_axi` master, for instance — names a
pre-written task instead:

```python
def kernel_task(self) -> KernelTask:
    return KernelTask("mem_r_stream_task", "mem_r_stream_task.h",
                      ("s_cmd", "m_mem", "m_out"), template_args=(64,))
```

`run_iter` then stays as the **pysim golden**: the model that says what the hand-written C++ is
supposed to do. Nothing checks the two against each other except a test.

## Choosing between leaf and composite

Reach for a composite when stages should run **concurrently** — a load / compute / store pipeline
where the loads of job *j+1* overlap the store of job *j*. Each child becomes its own `hls::task`,
and the internal interfaces become the FIFOs between them. If the work is one stage, a leaf is
simpler and lowers to exactly one task.

One trap the framework has hit twice: in a free-running pipeline **every stage needs a token per
job**. A stage that consumes input and emits nothing on some jobs will deadlock a downstream stage
that is waiting. If an opcode produces no output, forward something anyway.

## Next

- [Flow steps](./concurrent_flowsteps.md) — from the graph to a generated top and an RTL check.
- [How it is realized in HLS](../comp_codegen/freerunning.md) — the `ap_ctrl_none` top and the task
  body — and [the XSI testbench](../comp_codegen/xsi_tb.md) that drives it.
- [mem_copy example](../../examples/memcpy/) — a full composite, worked end to end.
