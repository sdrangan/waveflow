---
title: Overriding the generated task
parent: Module Code Generation
nav_order: 5
audience: hls
applies_to: [FreeRunMod]
api: [KernelTask, kernel_task, derive_kernel_task]
summary: "When the framework cannot write a leaf's task body — an m_axi owner, or anything outside the extractor's vocabulary — the module hands over a body you wrote by overriding kernel_task() with a KernelTask. Explains what each field is, why none of them is derivable in that case (the parameter order is a fact about the C++), and that run_iter stays as the pysim golden."
---

# Overriding the generated task

A [free-running leaf](./freerunning.md) normally declares nothing: its task body is generated from
`run_iter`, and its `KernelTask` descriptor — the C++ name, the header, the argument order, the
template arguments — is **derived** from the module by the same helpers that emit the body.

Sometimes the framework cannot write the body. Then you write it, and `kernel_task()` is how you hand
it over.

## When this happens

**The body owns an `m_axi` master.** Task-body emission refuses these outright:

> `MemRStream has m_axi masters ['m_mem']; task-body emission is stream-only today.`

That is a **scope boundary, not a law of HLS** — a free-running task may carry `m_axi`, and the
framework's own `mem_r_stream_task.h` does. The emitter simply has not answered what an `m_axi` body
needs (bundle naming, depth, who owns the `offset=slave` register), so it refuses rather than emit
something unreviewed.

**The body is outside the extractor's vocabulary.** Constructing descriptors, driving a `framed_word`
channel, holding stream-of-blocks locks — none of that is in the
[fixed list of shapes](./extractor.md) the extractor recognizes.

Both cases are the same answer: a fixed, hand-written, reviewed task body, named through
`kernel_task()`.

## The override

`MemRStream` is the worked case ([`waveflow/hw/mem_stream.py`](../../../waveflow/hw/mem_stream.py)):

```python
def kernel_task(self) -> KernelTask:
    return KernelTask(
        "mem_r_stream_framed_task",       # task_fn      — the C++ function
        "mem_r_stream_framed_task.h",     # header       — where it lives
        ("s_cmd", "m_mem", "m_out"),      # signature    — endpoint attrs, IN ARGUMENT ORDER
        template_args=(int(self.mem_dwidth),),
    )
```

which the composite generator turns into:

```cpp
hls_thread_local hls::task t1(mem_r_stream_framed_task<64>, cmd, m_in, copy_data);
```

### Why none of this is derivable

Look at the third argument. `("s_cmd", "m_mem", "m_out")` is **stream, m_axi, stream** — and that
order is a fact about the C++, not about the Python. Whoever wrote the header chose it. The module's
endpoints could be declared in any order at all, and nothing about the Python implies that the memory
pointer belongs in the middle.

`signature` is the map between the two: **the endpoint attribute names, in task-argument order**. The
generator walks it, resolves each name to either a boundary port or an internal channel — decided by
what that endpoint was bound to — and emits the call. That is the seam that lets the top be derived
from the graph while the body stays hand-written.

The same applies to `task_fn` and `header`: nobody can guess that `MemRStream`'s body is called
`mem_r_stream_framed_task` rather than `mem_r_stream_task`. And `template_args` are the concrete values
to bake into the C++ template, in the order the template declares them.

### `run_iter` stays

Overriding `kernel_task` does **not** remove `run_iter`. It stays as the **pysim golden**: the model
that says what the hand-written C++ is supposed to do. It is no longer extracted — the body you named
is used instead — but it is still what the Python simulation runs, and what the RTL is checked
against.

Nothing checks the C++ against the Python except a test. That is the standing arrangement for
hand-written bodies everywhere in the framework, hooks included.

## What the base class does otherwise

The default `kernel_task()` derives all four fields, and overriding is detected by identity against
that base method — the same way [`_kind()`](../flows/concurrent_python.md) detects a `run_iter`
override. So:

| | `kernel_task()` |
|---|---|
| generated leaf | derived; declare nothing |
| hand-written leaf | **override** |
| composite | raises — a composite has no task of its own, only its children do |

A generated leaf that declared one anyway would be restating what the generator already knows, and
**nothing cross-checks the two** — a mismatched name surfaces as a csynth error about a missing
function, not a Waveflow one. That is exactly why the derivation exists.

One field carries a second job worth knowing about: `task_fn` is also the component's identity key for
[calibration](../calib/), so renaming a hand-written task renames its calibration data.

## See also

- [Free-running kernel in HLS](./freerunning.md) — the generated case this departs from.
- [Free-running composite in HLS](./freerunning_composite.md) — how the resolved call ends up in a top.
- [Streaming Memory Kernels](../memory/memstream.md) — `MemRStream` / `MemWStream`, the components
  whose bodies are hand-written this way.
