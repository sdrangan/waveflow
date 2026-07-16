---
title: Control-driven kernel
parent: Realization Flows
nav_order: 1
audience: python
api: [HlsCodegenStep, FunctionalVerifyStep, ValidateTimingStep]
summary: "Flow 1 — a control_driven_kernel verified by a sequential_vitis_tb through C-simulation and RTL co-simulation. The simplest path, because the kernel is a function: the testbench calls it, and Vitis generates the RTL harness for you. Three gates — bit-exact against the Python golden, pipeline II <= 1, and measured RTL cycles within tolerance of the Python prediction. Built and green; the worked instance is examples/regmap."
---

# Flow 1 — Control-driven kernel

**DUT:** a [`control_driven_kernel`](../comp_codegen/index.md) — a
[`HostActivated`](../components/hostactivated.md) component realized as one `ap_ctrl_hs` HLS IP.
**Testbench:** a [`sequential_vitis_tb`](../comp_codegen/testbench.md) — a [`SeqTB`](../components/)
whose `main()` *calls* the kernel, run under Vitis **C-simulation** (the C++ directly) and
**co-simulation** (the generated RTL behind the same call).

**Status: built.** Every kernel in the repo takes this path today — `simp_fun`, `poly`, `hist`,
`block_scale` — though not all with the same rigor (see [the gates](#the-three-gates)).

> `block_scale` is the one that does not *declare* the target: it is still a plain `HwComponent` rather
> than a `HostActivated`, so it is `ap_ctrl_hs` on **raw pins** instead of behind an `s_axilite`
> adapter, and [`check`](../comp_codegen/index.md) abstains on it. It takes this flow all the same —
> which is the point of the [contract](../comp_codegen/structure.md) being about *shape*, not
> declaration.

## Why this is the simple one

**The kernel is a function.** It takes arguments, runs once, returns. So the testbench is a
straight-line program that calls it — and in C-simulation that call is *literally* a C++ function call:
untimed, with no hardware in sight.

The leverage is in co-simulation. Because the DUT is a function, **Vitis generates the RTL harness for
you**: it wraps your C testbench so each call becomes an `ap_start` → wait-for-`ap_done` transaction
against the synthesized RTL. You write a program; you get a cycle-accurate measurement.

That is exactly what the next flow loses. A free-running kernel never returns, so there is nothing to
call and nothing for Vitis to wrap — which is why [Flow 2](./freerun_seq.md) must drive the RTL
directly with a hand-written BFM.

## The recipe

The `BuildDag` for a Flow 1 example, in dependency order
([`examples/regmap/simp_fun_build.py`](../../../examples/regmap/simp_fun_build.py)):

| Step | Does | Produces |
|---|---|---|
| `build_inputs` | write the test vector to disk | `x_in`, `a_in`, `b_in`, `data_dir` |
| `gen_kernel` | lower the component → HLS C++ | `<kernel>.hpp`, `.cpp`, hook stub |
| `gen_tb` | lower the `SeqTB` → `<kernel>_tb.cpp` | `simp_fun_tb` |
| `system_sim` | Python-only sim: host + DUT, no Vitis | `system_sim` |
| `py_sim` | run the `SeqTB` → **the golden** + the prediction | `sim_dir`, `py_timing` |
| `csim` | Vitis C-simulation of the generated TB + kernel | `csim_data_dir` |
| **`validate_csim`** | **gate 1** — C-sim outputs vs the golden | `verify_report` |
| `csynth` | C-synthesis **+ RTL co-simulation** | `report_dir` |
| **`inspect_synth`** | **gate 2** — parse the synthesis report | `loop_df` |
| `extract_cosim_timing` | the measured cycles, from the cosim report | `cosim_timing` |
| **`validate_timing`** | **gate 3** — prediction vs measurement | `timing_verdict` |
| `generate_timing_diagram` | render the side-by-side SVG | `timing_diagram_svg` |

One ordering constraint is worth noticing: **`csynth` consumes `csim_data_dir`.** Synthesis does not
run until C-simulation has passed — there is no point measuring the timing of a kernel that computes
the wrong answer.

(`--list-steps` shows two more: the `*_source` / `run_tcl` entries are file dependencies rather than
work, and `sync_docs_figures` promotes the rendered diagram into the committed docs — deliberately
manual, so a routine run never churns a committed figure.)

## The three gates

The flow's value is not that it produces C++. It is that it **refuses to agree** when something is
wrong.

These are what the flow *offers*; each example wires what it needs. `regmap` and `stream_inband` wire
all three — they are the reference shape. `shared_mem` checks its outputs inside its own cosim step and
gates on the AXI burst layout instead of on cycles; `block_scale` runs C-sim and co-sim with no
separate gate step at all. If you are building a new example, copy `regmap`.

**Gate 1 — functional (`validate_csim`).** The C-sim outputs are compared **bit-exactly** against the
`py_sim` golden: the result deserialized from `.bin` as typed values, plus the named
`regmap_status.json` fields. Same Python source, same answers. A mismatch stops the pipeline here.

**Gate 2 — design intent (`inspect_synth`).** Every reported loop must have **pipeline II ≤ 1**. If
Vitis backed off to a slower schedule, the timing below would no longer describe the design you meant —
so the step fails rather than measure the wrong thing.

**Gate 3 — cycle-approximate Python (`validate_timing`).** `abs(py_cycles − cosim_cycles)` must fall
within a per-kernel tolerance. This is the gate that makes the framework's central claim *checkable* —
that the Python model predicts real hardware. For `simp_fun`:

```json
{ "pass": true, "py_cycles": 4, "cosim_cycles": 5, "delta": 1, "tolerance": 4 }
```

The model predicted 4 cycles; the RTL took 5. Both numbers are kept, not just the pass bit — a future
cycle-model-training step would fit the model's parameters from a corpus of these verdicts.

## Four levels of one design

What makes the flow readable is that a single component is exercised four ways, each closer to
hardware:

1. **`system_sim`** — Python only: a host `SimObj` driving the DUT. No Vitis.
2. **`py_sim`** — the `SeqTB` alone, in SimPy. Produces the golden **and** the cycle prediction.
3. **`csim`** — the generated C++, compiled and run. Untimed; checks the maths.
4. **`cosim`** — the synthesized RTL, driven by Vitis's harness. Timed; checks the cycles.

Levels 1–2 need no toolchain, which is why most of the test suite runs with no Vitis installed.

## Running it

```bash
cd examples/regmap
python simp_fun_build.py --through validate_timing
```

Steps whose outputs are already fresh are skipped; `--force` re-runs them anyway. With no Vitis, stop
at `--through py_sim` — everything up to the golden is toolchain-free.

## The walkthrough

This page is the **recipe**. The **worked instance** is [Register Map](../../examples/regmap/) — the
same flow, one page per step, against real code:
[the Python model](../../examples/regmap/python.md) →
[system simulation](../../examples/regmap/pysim.md) →
[sequential execution](../../examples/regmap/seqtb.md) →
[code generation](../../examples/regmap/codegen.md) →
[C and RTL simulation](../../examples/regmap/rtlsim.md).

## See also

- [Component structure](../comp_codegen/structure.md) — the contract for when a component lowers at all.
- [Testbench](../comp_codegen/testbench.md) — how a `SeqTB`'s `main()` becomes the `int main()` this flow runs.
- [Build System](../build/) — the `BuildDag` machinery behind the table above.
- [Flow 2](./freerun_seq.md) — the same sequential testbench, against a DUT Vitis cannot wrap.
