---
title: Vitis Pattern
parent: Build System
nav_order: 4
summary: "The Vitis half of a build -- generate the headers and the kernel boundary, C-simulate every scenario with a hand-written testbench, check it against the expected responses, synthesize and co-simulate, inspect the synthesis report, and compare pysim's cycle estimate with cosim -- taken from the streaming polynomial example. The csim and csynth steps are a pattern to copy; the toolchain call, the csynth.xml parser and the cosim timing steps are framework pieces."
---

# Vitis Pattern

This is the Vitis half of the [Streaming polynomial](../../examples/stream_inband/index.md)
example's [`poly_build.py`](https://github.com/sdrangan/waveflow/tree/main/examples/stream_inband/poly_build.py);
the Python half is the [Python Simulation Pattern](./python.md).  Some of it is framework and
some is a pattern to copy:

| Piece | Where | Status |
| --- | --- | --- |
| `toolchain.run_vitis_hls(tcl, work_dir, env, capture_output)` | `waveflow.toolchain.toolchain` | framework |
| `HlsCodegenStep`, `StreamUtilsStep`, `DataSchemaStep`, `ArrayUtilsStep` | `waveflow.build`, `waveflow.hw` | framework -- see [Code Generation Steps](./codegen.md) |
| `CsynthParser(sol_path)` | `waveflow.utils.csynthparse` | framework |
| `ExtractCosimTimingStep`, `ValidateTimingStep` | `waveflow.build.cosim_steps` | framework |
| `CSimStep`, `CSynthStep`, `InspectSynthStep`, `SummaryStep` | the example | **pattern** |

The pattern steps stay in the example because what they check is the design's business: which
scenarios run, which loops must reach II = 1, what a passing build has to show.

---

## Code generation: the boundary around a hand-written body

```python
dag.add(HlsGenIncludeStep(name="gen_include"))       # include/: schema headers, serializers,
                                                     #   streamutils, bundle_tb.h
dag.add(SourcesStep(name="sources"))                 # the hand-written C++ in place
dag.add(HlsCodegenStep(name="gen_kernel", comp_class=PolyAccel,
                       source_artifact="kernel_sources", output_dir="gen", impl_dir="."))
```

`PolyAccel` is a [body-only kernel](../custom_hooks/body_only.md): `gen_kernel` writes the
boundary -- `gen/poly.cpp` with the interface pragmas and one call into the body, and
`gen/poly.hpp` declaring it -- and the body itself is the hand-written `poly_body_impl.tpp`.

**`SourcesStep` must come before `gen_kernel`.**  The generator writes the body file only when
it is missing, as a stub marked `TODO: implement body`.  A build in another directory (the
tests build in a temporary one) has to copy the real body in first, or the generator writes
its stub there and the copy then rightly refuses to overwrite it.  `CSimStep` also refuses to
simulate a stub: an empty body is not an error to Vitis, so the symptom would be every
scenario failing at once for no visible reason.

---

## CSimStep

```python
def _run_vitis(config: BuildConfig, stage: str, live_output: bool, clk_freq: float) -> None:
    _ensure_sources(config.root_dir)
    # The stage goes in the environment: vitis-run 2025.1 has no --tclargs.
    env = {"WAVEFLOW_POLY_STAGE": stage, "WAVEFLOW_POLY_CLK_PERIOD_NS": f"{1e9 / clk_freq:g}"}
    try:
        result = toolchain.run_vitis_hls(config.root_dir / "run.tcl", work_dir=config.root_dir,
                                         capture_output=not live_output, env=env)
    except Exception as exc:  # CalledProcessError carries the Vitis log
        out = getattr(exc, "stdout", "") or ""
        raise RuntimeError(f"Vitis {stage} failed: {exc}\n{out[-3000:]}") from exc


@dataclass(kw_only=True)
class CSimStep(BuildStep):
    description = "Vitis C simulation of every scenario, with the hand-written poly_tb.cpp."
    consumes = ["poly_cpp", "poly_hpp", "poly_body_impl", "include_dir", "scenario_list"]
    produces = {"csim_done": Path("results/csim_done.txt")}
    params = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, scenario_list, live_output, clk_freq, **_) -> dict:
        _require_real_body(config.root_dir)
        names = Path(scenario_list).read_text(encoding="utf-8").split()
        for name in names:
            (config.root_dir / "data" / name / "csim").mkdir(parents=True, exist_ok=True)
        _run_vitis(config, "csim", live_output, clk_freq)
        ...   # write results/csim_done.txt
        return {"csim_done": done}
```

Things to notice:

- **`consumes` lists every source Vitis compiles.**  `poly_cpp` and `poly_hpp` come from
  `gen_kernel`, `poly_body_impl` is the hand-written body it published, and `include_dir` is
  the generated headers.  Editing any of them makes C-simulation stale.
- **The testbench is ordinary C++.**  `poly_tb.cpp` loops over `data/scenarios.txt`, plays each
  scenario's stimulus into the kernel with `wf::play_stream`, calls `poly(...)`, and records the
  response with `wf::record_stream` into `data/<scenario>/csim/` -- the same format the model
  and pysim wrote.  So the check is the same `CheckStep`, with `stage="csim"`.
- **The step creates the output directories.**  The testbench's `write_bundle` writes into a
  directory that must exist.
- **Arguments reach `run.tcl` through the environment.**  `vitis-run` 2025.1 has no
  `--tclargs`, so `run_vitis_hls(args=...)` raises; the script reads
  `$::env(WAVEFLOW_POLY_STAGE)`.  The variable names are the example's own convention.
- **`live_output`** streams Vitis's output instead of capturing it (`--live-output` on the
  command line), for debugging.

`run.tcl` is short; the part that matters is one project, one solution, and the stage switch:

```tcl
open_project -reset waveflow_poly_proj
set_top poly
add_files gen/poly.cpp -cflags "-I."
add_files -tb poly_tb.cpp -cflags "-I."
...
if {$stage eq "csim"} {
    csim_design -argv "$data_dir csim"
} else {
    csynth_design
    cosim_design -argv "$data_dir cosim timing" -trace_level $trace_level
}
```

**Keep the project one directory deep.**  Vitis HLS 2025.1 records a design file relative to
a one-level project, so `open_project builds/w32` drops the kernel from C-simulation and the
link fails with `undefined symbol: poly(...)`.  To group projects, `cd builds` first and then
`open_project w32`.

---

## CSynthStep

```python
@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    description = "Vitis C synthesis, then RTL co-simulation of the timing scenario."
    consumes = ["poly_cpp", "poly_hpp", "poly_body_impl", "include_dir", "check_csim"]
    produces = {"report_dir": Path("waveflow_poly_proj/solution1"),
                "cosim_done": Path("results/cosim_done.txt")}
    params = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, live_output, clk_freq, **_) -> dict:
        (config.root_dir / "data" / "timing" / "cosim").mkdir(parents=True, exist_ok=True)
        _run_vitis(config, "synth", live_output, clk_freq)
        ...
        return {"report_dir": config.root_dir / "waveflow_poly_proj" / "solution1",
                "cosim_done": done}
```

- **It consumes `check_csim`**, so synthesis runs only after C-simulation has *passed*, not
  merely run.  That is policy -- nothing forces it -- but there is no point spending minutes
  on synthesis that a seconds-long C-simulation would have rejected.
- **Co-simulation runs one scenario**, `timing`.  The same testbench writes its response to
  `data/timing/cosim/`, checked by `CheckStep(stage="cosim", only=("timing",))`.  RTL
  simulation is slow; the other scenarios are covered by C-simulation.
- **`report_dir` is hard-coded** to the project and solution names in `run.tcl`.  Change both
  together.

---

## InspectSynthStep

```python
@dataclass(kw_only=True)
class InspectSynthStep(BuildStep):
    description = "Parse the C-synthesis report: loop II, latency and resources."
    consumes = ["report_dir"]
    produces = {"loop_df": Path("results/loop_df.csv"), "res_df": Path("results/res_df.csv")}
    params = {}

    def run(self, config: BuildConfig, report_dir, **_) -> dict:
        from waveflow.utils.csynthparse import CsynthParser

        parser = CsynthParser(sol_path=str(report_dir))
        parser.get_loop_pipeline_info()
        parser.get_resources()
        bad = parser.loop_df[parser.loop_df["PipelineII"].apply(
            lambda v: isinstance(v, (int, np.integer)) and v > 1)] if not parser.loop_df.empty else []
        if len(bad):
            raise RuntimeError(f"loops with PipelineII > 1:\n{bad.to_string()}")
        ...   # write loop_df.csv and res_df.csv
```

The parsing is framework; the **assertion** is the design's.  This one fails the build on any
loop above II = 1.  Another design might check a resource budget, or only record the tables.

---

## Timing: pysim against cosim

```python
dag.add(ExtractCosimTimingStep(name="extract_cosim_timing", top="poly",
                               report_dir_artifact="report_dir"))
dag.add(ValidateTimingStep(name="validate_timing", py_timing_artifact="py_timing",
                           cosim_timing_artifact="cosim_timing", tolerance_cycles=20))
```

`ExtractCosimTimingStep` reads `<top>_cosim.rpt` from the solution directory and writes the
transaction's cycle count in the same JSON shape as the Python side's
[`ExtractPyTimingStep`](./python.md#extracting-the-cycle-estimate).  `ValidateTimingStep` fails
the build when the two differ by more than `tolerance_cycles`, and writes its verdict either
way.  On the polynomial example cosim measures 143 cycles against pysim's 140.

---

## SummaryStep: the target that runs every check

```python
@dataclass(kw_only=True)
class SummaryStep(BuildStep):
    consumes = ["check_model", "check_pysim", "check_csim", "check_cosim", "loop_df", "res_df",
                "timing_verdict"]
    produces = {"summary": Path("results/summary.json")}
```

`--through` runs only the target's ancestors, so a pipeline whose last step is
`validate_timing` would skip the cosim response check and the synthesis report -- neither is
upstream of it.  A final step that consumes every check closes that gap.  Build through it:

```
python poly_build.py --through summary
```

---

## Wiring the whole pipeline

```python
def build_poly_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="poly_source", path=_SOURCE_DIR / "poly.py"))
    dag.add(SourceStep(artifact="scenarios_source", path=_SOURCE_DIR / "scenarios.py"))

    # Python: the model and pysim, checked against the expected responses.
    dag.add(ScenariosStep(name="scenarios"))
    dag.add(ModelStep(name="py_model"))
    dag.add(CheckStep(name="check_model", stage="model", done_artifact="model_done"))
    dag.add(PySimStep(name="py_sim"))
    dag.add(CheckStep(name="check_pysim", stage="pysim", done_artifact="pysim_done",
                      only=S.WELL_FORMED))
    dag.add(ExtractPyTimingStep(name="extract_py_timing"))

    # Code generation: headers, and the kernel boundary around the hand-written body.
    dag.add(HlsGenIncludeStep(name="gen_include"))
    dag.add(SourcesStep(name="sources"))
    dag.add(HlsCodegenStep(name="gen_kernel", comp_class=PolyAccel,
                           source_artifact="kernel_sources", output_dir="gen", impl_dir="."))

    # Vitis: csim on every scenario, then synthesis and cosim of the timing scenario.
    dag.add(CSimStep(name="csim"))
    dag.add(CheckStep(name="check_csim", stage="csim", done_artifact="csim_done"))
    dag.add(CSynthStep(name="csynth"))
    dag.add(InspectSynthStep(name="inspect_synth"))
    dag.add(CheckStep(name="check_cosim", stage="cosim", done_artifact="cosim_done",
                      only=("timing",)))
    dag.add(ExtractCosimTimingStep(name="extract_cosim_timing", top="poly",
                                   report_dir_artifact="report_dir"))
    dag.add(ValidateTimingStep(name="validate_timing", py_timing_artifact="py_timing",
                               cosim_timing_artifact="cosim_timing", tolerance_cycles=20))
    dag.add(SummaryStep(name="summary"))
    return dag
```

The command line around it is `run_dag_cli` -- see
[Python Simulation Pattern → CLI integration](./python.md#cli-integration):

```
python poly_build.py --through check_pysim    # Python only, no Vitis
python poly_build.py --through summary        # everything
```
