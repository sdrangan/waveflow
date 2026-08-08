---
title: Vector multiply resource modeling
parent: Examples
nav_order: 5.5
has_children: true
summary: "Measuring and modelling what a design costs. A free-running vector multiplier swept across its two parameters, with the measurements handed to a VitisResourceModel that predicts LUT, FF, DSP and BRAM anywhere in the space. The arithmetic is trivial on purpose — the subject is how little you have to say to get a trustworthy model: describe what the design contains, and the library picks the features, prices the device geometry and fits whatever is left."
---
# Vector Multiply — measuring and modelling what a design costs

This example builds on [`memcpy`](../memcpy/). That one introduced the **free-running** kernel — a
block that is never started and never returns, paced only by back-pressure. `vecmult` is the smallest
design in the tree that takes such a kernel all the way to a *number*: how many LUTs, flip-flops,
DSPs and block RAMs it occupies, and how that changes as you turn its knobs.

That is the subject here. The arithmetic is deliberately trivial — `z = x * y`, element-wise — because
the interesting content is not the compute. It is how little you have to say to get a resource model
you can trust: you **describe what the design contains** — this many multipliers, this many memory
banks, this many lanes — and the library chooses the features, prices the device geometry and fits
whatever is left over.

`fir_block` also models its resources, but it does so while simultaneously teaching cross-firing
state, fixed point, a four-module composite and RTL verification. This example does one thing.

## The design

One firing carries a command, both operands, and a response:

```text
s_in:   [ cmd(tx_id, n) | x_0 .. x_{n-1} | y_0 .. y_{n-1} ]
z_out:  [ z_0 .. z_{n-1} | resp(tx_id) ]
```

Two parameters, and the difference between them is load-bearing:

| | | |
|---|---|---|
| `dwid` | stream word width | sets the **lane count** `LW = dwid / 16` |
| `vlen` | **compile-time** bound on the buffer | sets the BRAM cost |
| `n` | **runtime** length in the command | costs nothing in area — but changes the *shape* of the logic |

Because `x` and `y` share one port they arrive sequentially, so the kernel buffers `x` while `y`
streams past. That buffer has to be read `LW` samples per cycle to sustain II=1, which forces a
cyclic `ARRAY_PARTITION` — and *that* is what turns a throughput requirement into a memory cost.

{: .note }
> It is worth knowing what does **not** force the buffer, because the plausible reason is false. Two
> *separate* input streams would need no buffer: distinct FIFOs are independent ports and their reads
> schedule in the same beat. Measured, that design runs at II=1 with **zero** BRAM. A shared port is
> what makes storage unavoidable — the buffer is a consequence of the *interface*, not the
> arithmetic.

## Learning Objectives

In going through this example, you will learn to:

1. **Build a standalone free-running module** — one `FreeRunMod`, one stream in, one out, with an
   in-band command and a response that echoes a transaction id.
2. **Hand off a kernel body the extractor cannot write** — declare a hand-written `hls::task` through
   `kernel_task()`, and keep the Python `run_iter` as the golden.
3. **Prove the twin** — replay the pysim job through the C++ in Vitis C-simulation, so "Python golden,
   C++ twin" is a checked claim rather than an intention.
4. **Write a parameter sweep with `sweep_cli`** — declare the points as a `ParamGrid`, the run as a
   `SweepRunner` and one `Stage`, and get a program with `--dry-run`, `--resume` and a flag per axis;
   then collect an attributed utilization report at each of the 16 points.
5. **Describe a design's structure and install a model on the module** — `resource_structure` and
   `get_rm` — reading each declared number straight off the kernel body, and getting DSP and BRAM
   exactly right before any synthesis has run.
6. **Sweep for the measurements the declaration cannot give you**, and let `VitisResourceModel`
   choose the features and fit the coefficients for LUT and FF.
7. **Compose an estimate over a hierarchy** — `add_rm`, `compose` — and read the confidence it
   reports, which is the *weakest* link rather than the best one.

## The build

```bash
python -m examples.vecmult.vecmult_build --list-steps
#   vecmult_source -> pysim -> codegen_dut -> csim -> csynth -> resources

python -m examples.vecmult.vecmult_build --through pysim        # no toolchain, seconds
python -m examples.vecmult.vecmult_build --through resources    # needs Vitis, ~40 s
python -m examples.vecmult.vecmult_sweep                        # the 16-point grid, ~15 min
```

`csynth` consumes the `csim` verdict, so a design whose C++ disagrees with its Python golden cannot
reach synthesis and cannot contribute a resource measurement.

{: .warning }
> There is **no RTL rung** here. The resource counters are settled at C-synthesis, so measuring them
> needs no RTL simulation — but that also means this example verifies *function* (csim) and *cost*
> (csynth) without verifying *cycle behaviour*. For a design that closes that too, see
> [`fir_block`](../firblock/) and its XSI gate.

## In this section

- [The module](./vecmult.md) — the standalone `FreeRunMod`, its ports, and the command/response
  protocol.
- [The kernel](./kernel.md) — the hand-written task: why it buffers, why it partitions, and the
  ragged final beat.
- [Testbench](./testbench.md) — the pysim golden and the csim twin check, driven from one set of
  vectors.
- [DUT codegen](./codegen_dut.md) — how one leaf lowers to an `ap_ctrl_none` top, what
  `template_args` reaches, and the two kinds of file in `include/`.
- [Testbench codegen](./codegen_tb.md) — what is generated for the csim rung and what deliberately
  is not.
- [The resource model](./vitis_resmod.md) — what `VitisResourceModel` does for you, the six terms it
  asks for and where each number comes from, and what it already predicts with nothing measured.
- [The sweep](./sweep.md) — how the sweep script is written, 16 design points through the DAG, and
  the committed corpus.
- [How well it fits](./resmodfit.md) — the model checked against points held out of its own fit, plus
  an appendix on why it is built the way it is.

## See also

- [Resource Analysis Tools](../../guide/resource/) — where the measurements come from.
- [Sweeping a design](../../guide/build/sweep.md) — the sweep API this example is the worked instance
  of.
- [Resource models](../../guide/resource_model/) — the concepts this example is the worked instance of.
- [Block FIR](../firblock/) — the advanced resource-modelling design: composite, stateful, RTL-gated.
