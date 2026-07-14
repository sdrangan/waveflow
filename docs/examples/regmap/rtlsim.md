---
title: C and RTL Simulation
parent: Register Map (simple function)
nav_order: 5
has_children: false
---
# C and RTL Simulation

This page picks up where [Code Generation](./codegen.md) leaves off. The Vitis HLS kernel and testbench C++ exist in `gen/`, the hand-written compute body lives next to them in `simp_fun_compute_impl.cpp`, and the input test vector is on disk from the Python simulation. From here, the build DAG drives Vitis through:

1. **C-simulation** of the generated testbench against the generated kernel, verified bit-exact against the Python golden.
2. **C-synthesis** to RTL, with a resource and pipeline-II report.
3. **RTL co-simulation** to get a measured cycle count from actual simulated hardware.
4. **Cycle-validation** comparing the cosim measurement against the Python sim's prediction, with a configurable tolerance.
5. **Timing-diagram** rendering, so both runs can be inspected visually side-by-side.

The payoff at the end is a single JSON verdict that confirms — quantitatively — that the Python simulation predicted the hardware's cycle count within the tolerance. That is the cycle-approximate-Python claim made concrete for this kernel.

## Running the full flow

```bash
cd examples/regmap
python simp_fun_build.py --through generate_timing_diagram
```

This executes every step in the DAG from `build_inputs` through `generate_timing_diagram`, skipping any whose outputs are already fresh. Requires a working Vitis HLS installation; if Vitis isn't found, the `csim` step fails fast with a clear error before any code generation.

## Stages after Python simulation

| Step                        | What it does                                                                 | Key produces                                    |
| --------------------------- | ---------------------------------------------------------------------------- | ----------------------------------------------- |
| `csim`                    | Runs Vitis HLS C-simulation of the generated TB against the generated kernel | `csim_data_dir` (kernel outputs in `data/`) |
| `validate_csim`           | Compares C-sim outputs against the Python sim's `sim_dir` artifacts        | `verify_csim.json`, `results/vitis/`        |
| `csynth`                  | Runs C-synthesis + RTL co-simulation                                         | `report_dir` (Vitis solution directory)       |
| `inspect_synth`           | Parses the C-synth XML report for resource use + per-loop pipeline II        | `results/loop_df.csv`                         |
| `extract_cosim_timing`    | Parses the cosim report for the measured per-transaction cycle count         | `cosim_timing.json`                           |
| `validate_timing`         | Compares `py_timing` against `cosim_timing` with a cycle tolerance       | `timing_verdict.json`                         |
| `generate_timing_diagram` | Renders the side-by-side SVG and a JSON companion describing the events      | `timing_diagram.svg`, `timing_diagram.json` |

Each step is described below in the order it runs.

## C-sim functional verification

`CSimStep` invokes Vitis HLS's C-simulator on the generated `.cpp` files. The kernel runs as a normal C++ program; its testbench (also auto-generated) reads the input `.bin` files written by `BuildInputsStep`, writes the registers via the slave model, drives `ap_start`, polls `ap_done`, and writes the kernel's output back to `y_data.bin` plus a `regmap_status.json` mirror of the final regmap state.

`FunctionalVerifyStep` then compares those C-sim outputs to the Python golden produced by `py_sim`:

```python
# examples/regmap/simp_fun_build.py
dag.add(FunctionalVerifyStep(
    name="validate_csim",
    golden_dir_artifact="sim_dir",        # from py_sim
    actual_dir_artifact="csim_data_dir",  # from csim
    schemas=[
        {"filename": "y_data.bin", "golden_filename": "y.bin", "schema": Int32},
    ],
    jsons=[
        {"filename": "regmap_status.json", "compare_fields": ["ap_done", "y"]},
    ],
    output_dir="results/vitis",
    output_artifact="vitis_dir",
    report_path="results/verify_csim.json",
))
```

The schema entry confirms the kernel produced the same `y` value as the Python model (deserialised from `.bin` so the comparison is over typed integers, not raw bytes). The json entry confirms `ap_done` and `y` agree between the two `regmap_status.json` files. If either mismatches, the step raises and the pipeline stops here — no point measuring timing on a kernel that returns wrong data.

A passing `validate_csim` is the functional half of the cycle-approximate-Python claim: same source, same outputs at the bit level. Everything that follows is about whether the *timing* of producing those outputs also agrees.

## C-synthesis and RTL co-simulation

`CSynthStep` runs the same `run.tcl` as `CSimStep` but with `WAVEFLOW_SIMP_FUN_COSIM=1` set in the environment — that flag tells the TCL script to perform C-synthesis followed by `cosim_design`. Output lands in `waveflow_simp_fun_proj/solution1/` (a standard Vitis HLS solution directory) with the C-synth XML report, the synthesised RTL, and the cosim report.

`InspectSynthStep` parses the C-synth XML for two things worth gating on:

```python
# examples/regmap/simp_fun_build.py — InspectSynthStep.run
parser = CsynthParser(sol_path=str(report_dir))
parser.get_loop_pipeline_info()
parser.get_resources()
if not parser.loop_df.empty:
    non_unit_ii = parser.loop_df[
        parser.loop_df["PipelineII"].apply(
            lambda v: isinstance(v, (int, np.integer)) and v > 1
        )
    ]
    if not non_unit_ii.empty:
        raise RuntimeError("Vitis synthesis produced loops with PipelineII > 1.")
parser.loop_df.to_csv(loop_df_path, index=False)
```

The hard assertion is that every reported loop has pipeline II ≤ 1 — i.e., the synthesizer accepted one-element-per-cycle throughput. If Vitis backed off (II > 1) the step fails: timing-validation results would no longer reflect the design intent. The full `loop_df` is also written to `results/loop_df.csv` for inspection.

## Cosim timing extraction

The cosim run's report is consumed by `ExtractCosimTimingStep`:

```python
# examples/regmap/simp_fun_build.py
dag.add(ExtractCosimTimingStep(
    name="extract_cosim_timing",
    top="simp_fun",
    report_dir_artifact="report_dir",
))
```

This step reads Vitis's cosim report (typically `<solution>/sim/report/simp_fun_cosim.rpt`), extracts the per-transaction cycle latency, and writes it as a structured `cosim_timing.json` with the same shape as the `py_timing.json` produced by `ExtractPyTimingStep` on the Python side. The matching shape is what makes the next step's comparison trivial.

`ExtractCosimTimingStep` is a generic build step from `waveflow.build.cosim_steps` — not poly-specific or simp_fun-specific. Any kernel run through this pipeline gets the same JSON shape on the cosim side.

## Validating cycle-approximate Python

`ValidateTimingStep` is where the loop closes:

```python
# examples/regmap/simp_fun_build.py
dag.add(ValidateTimingStep(
    name="validate_timing",
    py_timing_artifact="py_timing",
    cosim_timing_artifact="cosim_timing",
    tolerance_cycles=4,
))
```

It reads both timing JSONs, computes `abs(py_cycles - cosim_cycles)`, asserts the delta is within `tolerance_cycles`, and emits `timing_verdict.json` recording both numbers and the verdict. **Both numbers are kept in the verdict, not just the pass/fail bit** — this is the load-bearing artifact for a future cycle-model-training workflow that would fit the Python model's `latency_cycles` / `poll_interval_cycles` parameters from a corpus of these measurements.

The simp_fun tolerance is set to `4` cycles, which is tight for this kernel — the simple affine path has very little jitter between the Python model and the real RTL. If `validate_timing` ever starts failing on this kernel, it's a signal that either:

- the Python model's `latency_cycles` parameter drifted out of sync with what Vitis is actually scheduling, OR
- a change to the synthesis configuration introduced unexpected slack (e.g., pipelining choices changed).

Either is worth investigating before bumping the tolerance.

### Reading the verdict

`timing_verdict.json` has the shape:

```json
{
  "pass": true,
  "py_cycles": 5,
  "cosim_cycles": 6,
  "delta": 1,
  "tolerance": 4
}
```

The `pass` bit is what CI gates on; the four numeric fields are what a future model-tuning step would consume. If `pass` is `false`, the step also raises `RuntimeError` with the same fields in the message so the failure is visible without opening the JSON.

## Timing diagram

`GenerateTimingDiagramStep` consumes all three timing artifacts (`py_timing`, `cosim_timing`, `timing_verdict`) and renders a side-by-side SVG plus a JSON companion describing the event annotations:

```python
# examples/regmap/simp_fun_build.py
@dataclass(kw_only=True)
class GenerateTimingDiagramStep(BuildStep):
    description = "Generate the committed timing-diagram artifacts from timing JSON."
    consumes = ["py_timing", "cosim_timing", "timing_verdict", "timing_diagram_source"]
    produces = {
        "timing_diagram_svg":  Path("results/timing_diagram.svg"),
        "timing_diagram_json": Path("results/timing_diagram.json"),
    }
```

The SVG is generated by a normal DAG step, not by a separate ad-hoc script — so the diagram in `results/timing_diagram.svg` is always in sync with the most recent simulation run. The JSON companion records the event annotations (`ap_start`, `kernel_done`, `host_done`) and the cycle counts used to render the diagram, which makes the SVG re-renderable from a different tool if needed and provides a checkable record of what the diagram is claiming.

The Python and cosim transactions are stacked with cycle ticks; the `delta` from the verdict is visible at a glance, and the event labels show where the two timelines diverge (or, ideally, don't).

### The committed figure

So this page renders the same diagram without anyone re-running Vitis, a copy is committed under [`images/`](./images/) and embedded here:

![Register-map timing diagram — the ap_start launch, the busy window, and ap_done completion, Python vs cosim cycle counts](./images/timing_diagram.svg)

It is promoted from the generated `results/timing_diagram.svg` by `SyncDocsFiguresStep`, mirroring the [shared_mem committed-figure workflow](../shared_mem/timing.md). To **refresh** it after a code or timing change:

```bash
cd examples/regmap
python simp_fun_build.py --through generate_timing_diagram   # regenerate results/timing_diagram.svg
python simp_fun_build.py --through sync_docs_figures          # copy it into docs/.../images/ + record provenance
git diff -- docs/examples/regmap/images/                      # review the change, then commit
```

Unlike the shared_mem figures (which re-render from a committed VCD with no Vitis), this diagram measures the real cosim cycle count, so a refresh runs the Vitis flow. Either way the committed figure changes only when you deliberately sync it — a reviewable `git diff`, never silent churn from a routine run.

## End-to-end inspection

After a successful run, the four files worth opening in order:

```bash
cat results/verify_csim.json     # bit-exact functional agreement
cat results/loop_df.csv          # one pipeline II row per loop, all should be ≤ 1
cat results/timing_verdict.json  # py vs cosim cycles, delta, pass/fail
open results/timing_diagram.svg  # visual confirmation
```

If all four show the expected results, this kernel passes the full cycle-approximate-Python contract: same outputs, same expected timing, and a renderable artifact to prove it.

## Next

This is the last page in the Vitis Register Map walkthrough. For the broader framework references, see:

- [Build System](../../guide/build/index.md) — the full BuildDag reference.
- [Register Maps](../../guide/interface/regmap.md) — the underlying regmap abstractions.
- [Components](../../guide/components/index.md) — the component kinds (`HostActivated`, `FreeRunComp`, `CompositeComp`) deep dive.
