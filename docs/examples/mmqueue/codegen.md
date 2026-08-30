---
title: Code generation
parent: VMAC with an AXI-MM command queue
nav_order: 6
has_children: false
---

# Code generation — the synthesizable top

VMAC's synthesizable top is **auto-extracted** from the same `run_proc` the
[Python simulation](./pysim.md) runs. There is no hand-written top in the build path:
the framework lowers the extracted `run_proc` IR to a free-running C++ kernel, and a
Vitis cosim checks it bit-exact against the one golden.

## The generated path is the real one

[`vmac_topgen.py`](../../../examples/vmac/vmac_topgen.py) generates the top from the
extracted `run_proc` IR through the framework `kernel_to_cpp` path
(`kernel_signature` + `kernel_body_to_cpp`). The result is a **free-running,
`m_axi`-only** kernel that:

1. dequeues a `VmacCmd` from the in-memory ring — the synthesizable
   [`AXIMMQueue.get`](../../guide/interface/derived/mmqueue.md) lowered to the
   [`queue_get` ring-dequeue hook](../../guide/custom_hooks/queue.md);
2. stops if the command is `OpCode.end`;
3. otherwise runs `vmac_compute` (the [hand-written datapath hook](./hook.md)) and
   loops.

It is wired into the build as the `topgen_cosim` step, which builds a ring image in
memory (head/tail/capacity + one command slot + an `END` slot via the producer-side
`VmacCmd.serialize`), lays the `A`/`B` operands after the ring, invokes the generated
top, and checks the `Y` region **bit-exact** against
[`vmac_golden_mem.apply_golden`](../../../examples/vmac/vmac_golden_mem.py) (which
runs the one `execute` golden — no second golden):

```bash
cd examples/vmac
python vmac_build.py --through topgen_cosim   # the auto-extracted top, bit-exact cosim (Vitis)
```

So the generated top and the synthesizable queue dequeue are **done and validated**,
not aspirational.

## Why the top has only `m_axi` + `ap_ctrl`

The generated top exposes exactly two interfaces: the `m_axi` `gmem` master and the
`ap_ctrl` block-control handshake. There is **no `s_axilite`** command port. The
command never crosses a register boundary — it is read from the ring in memory by the
dequeue hook and deserialized inside the kernel.

This is deliberate (decision D6) and it **sidesteps a csynth pitfall**. Passing the
nested `VmacCmd` struct in over `s_axilite` (or by value into the synthesizable path)
mis-decomposes through HLS's struct/array optimization at C-synthesis — loop bounds
can fold to zero and the kernel gets dead-code-eliminated. That is exactly the
gotcha the [Custom Hooks: Complex guide](../../guide/custom_hooks/complex.md#1-pass-scalars-to-a-_core-function--not-a-struct-by-value)
documents, and it is why the **old hand-rolled top had to unpack the command into
scalar registers** by hand. Reading the command from memory removes the boundary
entirely: the deserialized fields are already plain scalars by the time they reach
`vmac_compute_core`.

## How the pieces fit

The generated top is the *glue*; the datapath is hand-written:

- The **structure** (the kernel signature, the `m_axi` port, the dequeue/compute
  loop) is auto-generated — the general mechanism is
  [Module Code Generation](../../guide/comp_codegen/).
- The **dequeue** is the [`queue_get` hook](../../guide/custom_hooks/queue.md), called
  where `run_proc` had `cmd_queue.get`.
- The **compute** is the [`vmac_compute_impl.tpp` hook](./hook.md), called where
  `run_proc` had `vmac_compute`.

The generated top calls into both hooks; codegen emits the calls, you wrote the
bodies.

## Transitional note

The build still contains hand-rolled `render_top` / `render_cosim_tb` f-strings (the
ones that unpack the command into `s_axilite` scalars). They are **not** the current
top — they remain only because the **calibration pipeline**
([`vmac_cosim_sweep.py`](../../../examples/vmac/vmac_cosim_sweep.py) /
[`vmac_cosim_stage3.py`](../../../examples/vmac/vmac_cosim_stage3.py) →
`calibration/vmac_calibration.json`, the queue-sim baseline behind the
[timing study](./timing.md)) still shares them. Migrating the calibration sweep onto
the generated top and re-running the Vitis sweep is a follow-up; the generated path
above is what produces the kernel.
