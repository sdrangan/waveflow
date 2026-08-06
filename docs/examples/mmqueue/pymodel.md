---
title: The Python model
parent: VMAC with an AXI-MM command queue
nav_order: 4
has_children: false
---

# The Python model — one-golden anatomy

This is the reference page of the example. VMAC's `VmacAccel`
([`examples/vmac/vmac.py`](../../../examples/vmac/vmac.py)) is the canonical
**one-golden accelerator anatomy**: a single author-written numerical golden that
*both* the SimPy simulation and the synthesized kernel are checked against, wrapped
in a synthesizable shell that owns memory and timing. The next tile copies this
shape.

## One author-written golden

The numerical truth is one method:

```python
def execute(self, cmd: VmacCmd, a, b=None, alpha=None) -> DataArray:
    ...
```

`execute` is **pure math** — arrays in, result array out. It takes the operands
already resident as numpy/`DataArray` structures (`a`, optional `b`, optional
per-row `alpha`), applies the element-wise op, optionally reduces down the rows, and
requantizes once to the output format (the [arithmetic](./arithmetic.md) page). It
knows nothing about memory, addressing, the queue, or timing. Because it is the
single source of numerical truth, the C++ kernel is bit-exact with it *by
construction*, and the SimPy run is checked against it directly.

> **Why one golden, not two.** An earlier design carried *two* goldens — `execute`
> (pure math) and an `execute_mem` that operated on a flat memory image. They were
> redundant and could drift. The example collapsed to a single `execute` plus a
> generic memory-image harness ([`vmac_golden_mem.py`](../../../examples/vmac/vmac_golden_mem.py)'s
> `apply_golden`, which deserializes operands out of a memory image, calls `execute`,
> and serializes the result back). That is the same parity discipline the
> [shared-memory histogram](../shared_mem/) example uses — one golden, a generic
> deserialize/serialize wrapper for the memory view — and it is what the
> [RTL conformance](./rtlsim.md) now compares against.

## The synthesizable shell

Around the golden sits the synthesizable method:

```python
@synthesizable(impl_file="vmac_compute_impl.tpp")
def vmac_compute(self, cmd: VmacCmd, mem) -> ProcessGen[DataArray]:
    ...                       # timed reads → self.execute(...) → timed writeback
```

`vmac_compute` **owns memory access and timing**: it reads the operand regions over
`mem`, calls `self.execute(...)` for the math, and writes `Y` back — recording each
transaction for the timing model. The `mem` argument is the same in both worlds: an
[`MMIFMaster`](../../guide/interface/aximm.md) in the SimPy simulation, an `m_axi`
pointer in the C++ kernel. Its Python body is the **simulation** model only — the
extractor does *not* lower it; codegen emits a call to the hand-written
[`vmac_compute_impl.tpp`](./hook.md) instead. So the one method is bit-exact-checked
in Python and synthesized from the `.tpp`.

## Region — element coordinates

The shell reads and writes through a **`Region`** view over the master:
`mem.region(...)` gives a `Region` whose `read_slice` / `write_slice` take **element
indices**, not byte or word offsets. This is the SimPy twin of the C++
`read_array_slice` / `read_array_lane` calls (see
[kernel transfer reference](../../guide/custom_hooks/reference.md)): the model and the kernel
address operands the same width-agnostic way — `M[i, j]` at element `addr +
i·row_stride + j` — so a `data_bw` or `mem_dwidth` change never rewrites the
addressing. Element coordinates are also synth-natural: the kernel divides by the
packing factor internally, the author never does. The `Region` slice calls keep
timing **off the data path** — they return the values and record the transfer
separately — which is what lets the timing model be calibrated independently (see
[timing](./timing.md)).

## Sim vs. hardware: what is extracted

`VmacAccel` cleanly separates what becomes hardware from what is simulation
bookkeeping. The free-running consumer is `run_proc`:

```python
while True:
    cmd = yield from self.cmd_queue.get(self.Cmd)  # → AXIMMQueueGetStmt (synthesizable)
    self._record_dequeue(cmd)                      # @sim_only — dropped by the extractor
    if cmd.op == OpCode.end:                        # sentinel → ends the loop
        return
    yield from self.vmac_compute(cmd, self.m_mem)  # → the kernel hook
    self._record_command(cmd)                       # @sim_only — latency bookkeeping
```

- The **extractable control flow** — the dequeue (`cmd_queue.get`, which lowers to
  the [ring-dequeue hook](../../guide/custom_hooks/queue.md)), the `end` test, and the
  `vmac_compute` call — is what the [generated top](./codegen.md) is built from.
- The **`@sim_only`** helpers (`_record_dequeue`, `_record_command`) are timing/latency
  bookkeeping; the extractor silently drops them, so they never reach hardware.
- **`pre_sim`** does one-time Python setup (caching element geometry, setting the
  ring's `poll_interval`); it is not synthesized.

That split — synthesizable control flow with `@sim_only` instrumentation woven
through it, and a `@synthesizable` datapath delegating to a pure golden — is the
anatomy. The [Python simulation](./pysim.md) page runs it; the
[code generation](./codegen.md) page extracts it.
