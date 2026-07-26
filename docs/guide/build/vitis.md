---
title: Vitis Pattern
parent: Build System
nav_order: 4
---

# Vitis Pattern

> **Status: pattern only.** Waveflow does not yet ship framework-level steps for Vitis C-sim, C-synth, or report inspection. The steps below live in [examples/stream_inband/poly_build.py](https://github.com/sdrangan/waveflow/tree/main/examples/stream_inband/poly_build.py) and serve as the canonical recipe to copy. Once a second design uses them, the genuinely-common pieces (toolchain invocation, report parsing) will be extracted into `waveflow/build/`.

A typical Vitis pipeline has four steps:

1. **`CSimStep`** — invoke `vitis_hls run.tcl` with `cosim=0`, let the C++ testbench drive the kernel and write outputs to a data directory.
2. **`ValidateCSimStep`** — compare Vitis's output binaries against the Python simulation's binaries (from [`PySimStep`](./python.md)) and fail the build on mismatch.
3. **`CSynthStep`** — invoke `vitis_hls run.tcl` again with `cosim=1`, producing the synthesis solution directory.
4. **`InspectSynthStep`** — parse `solution1/syn/report/csynth.xml` and emit a CSV of loop pipeline information; fail the build if any reported loop has `PipelineII > 1`.

The framework primitives all four steps lean on:

| Primitive | Location | What it does |
|---|---|---|
| `toolchain.run_vitis_hls(tcl, work_dir, env, capture_output)` | `waveflow.toolchain.toolchain` | Invokes Vitis HLS with the given TCL script and environment; returns a `subprocess.CompletedProcess`. |
| `CsynthParser(sol_path)` | `waveflow.utils.csynthparse` | Parses `csynth.xml` from a Vitis solution directory; exposes `loop_df` and `res_df` as pandas DataFrames. |

These are the things you can reuse today. The build-step *shape* is what varies per example, which is why we're documenting it as a pattern rather than a class.

---

## CSimStep

```python
@dataclass(kw_only=True)
class CSimStep(BuildStep):
    description = "Invoke Vitis HLS C-simulation."
    consumes    = ["poly_cpp", "poly_hpp", "poly_tb", "include_dir", "data_dir"]
    produces    = {"csim_data_dir": "data_dir"}
    params      = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, include_dir, data_dir, live_output, clk_freq, **_) -> dict:
        vitis_env = {"WAVEFLOW_POLY_COSIM": "0",
                     "WAVEFLOW_POLY_TRACE_LEVEL": "none",
                     "WAVEFLOW_POLY_CLK_PERIOD_NS": f"{1e9 / clk_freq:g}"}
        try:
            result = toolchain.run_vitis_hls(
                config.root_dir / "run.tcl",
                work_dir=config.root_dir,
                capture_output=not live_output,
                env=vitis_env,
            )
            if result.stdout: print(result.stdout)
            if result.stderr: print(result.stderr)
        except Exception as exc:
            raise RuntimeError(str(exc))
        return {"csim_data_dir": data_dir}
```

Things to notice:

- **`consumes` lists every source file Vitis will touch.** `poly_cpp` / `poly_hpp` / `poly_tb` are codegen artifacts from `HlsCodegenStep` instances. Touching any of them invalidates the C-sim results — exactly what you want. `include_dir` is the generated headers directory from `HlsGenIncludeStep`. `data_dir` is the input binaries directory from `BuildInputsStep`.
- **`produces = {"csim_data_dir": "data_dir"}`** uses string aliasing. Vitis writes its output binaries back into the same `data_dir` that `BuildInputsStep` created (the testbench writes alongside the inputs), so this step doesn't produce a *new* path — it re-publishes the same path under a new name so downstream steps can express "I depend on Vitis having run here." String aliasing is the right tool for this; declaring `Path("data")` again would conflict with the existing producer.
- **The `WAVEFLOW_POLY_*` env vars are a contract** between the build step and the C++ testbench. The testbench reads `WAVEFLOW_POLY_COSIM` to decide whether to skip the cosim flow; reads `WAVEFLOW_POLY_CLK_PERIOD_NS` to set timing. This is per-example — every design defines its own env-var schema. There is no framework-level convention (yet).
- **`run.tcl` is hard-coded** as living at `config.root_dir / "run.tcl"`. Most Vitis flows have one canonical TCL script per project; this convention matches that. If you need multiple solutions, parameterize via the env dict or write a second step.
- **`live_output: False`** captures stdout/stderr by default. Pass `--live-output` on the CLI to stream Vitis output in real time when debugging.
- **`try/except` around the toolchain call** converts subprocess failures into `RuntimeError` so the DAG records a clean `BuildResult.success=False` rather than crashing.

---

## ValidateCSimStep

```python
@dataclass(kw_only=True)
class ValidateCSimStep(BuildStep):
    description = "Compare Vitis C-sim outputs against the Python model."
    consumes    = ["sim_dir", "csim_data_dir", "data_cmd_hdr"]
    produces    = {"vitis_dir": Path("results/vitis")}
    params      = {}

    def run(self, config: BuildConfig, sim_dir, csim_data_dir, data_cmd_hdr) -> dict:
        try:
            data_hdr = PolyCmdHdr().read_uint32_file(data_cmd_hdr)
            nsamp = int(data_hdr.nsamp)
            sim_resp_hdr = PolyRespHdr().read_uint32_file(sim_dir / "resp_hdr.bin")
            sim_status = json.loads(
                (sim_dir / "regmap_status.json").read_text(encoding="utf-8"))
            sim_samp_out = np.array(
                read_uint32_file(sim_dir / "samp_out.bin", elem_type=Float32, shape=nsamp),
                dtype=np.float32,
            )
            got_resp_hdr = PolyRespHdr().read_uint32_file(csim_data_dir / "resp_hdr_data.bin")
            got_status = json.loads(
                (csim_data_dir / "regmap_status.json").read_text(encoding="utf-8"))
            got_samp_out = np.array(
                read_uint32_file(csim_data_dir / "samp_out_data.bin", elem_type=Float32,
                                 shape=nsamp),
                dtype=np.float32,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to read sim or Vitis outputs: {exc}")
        if not got_resp_hdr.is_close(sim_resp_hdr):
            raise RuntimeError("Response header mismatch after Vitis C-simulation.")
        for label, status in (("python", sim_status), ("vitis", got_status)):
            if int(status["halted"]) != 0 or int(status["error"]) != int(PolyError.NO_ERROR):
                raise RuntimeError(
                    f"{label} regmap reports halted={status['halted']}, "
                    f"error={status['error']}, tx_id={status['tx_id']}.")
        if not np.allclose(got_samp_out, sim_samp_out[:got_samp_out.size],
                           rtol=1e-6, atol=1e-6):
            raise RuntimeError("Sample output mismatch after Vitis C-simulation.")
        vitis_dir = config.root_dir / "results" / "vitis"
        vitis_dir.mkdir(parents=True, exist_ok=True)
        got_resp_hdr.write_uint32_file(vitis_dir / "resp_hdr.bin")
        write_uint32_file(got_samp_out, elem_type=Float32,
                          file_path=vitis_dir / "samp_out.bin", nwrite=len(got_samp_out))
        (vitis_dir / "regmap_status.json").write_text(
            json.dumps(got_status, indent=2), encoding="utf-8")
        return {"vitis_dir": vitis_dir}
```

This step is **deeply example-specific** — it knows about `PolyRespHdr`, the `regmap_status.json` schema, file naming conventions, and tolerance thresholds. There is no generic version here. Per-design template:

- Consume the Python sim output directory and the Vitis output directory.
- Read each schema instance / array from both.
- Assert equality (or `is_close` / `np.allclose` for floats).
- Copy the validated Vitis outputs into a `results/vitis/` directory so they're easy to find when reviewing the build.
- Raise `RuntimeError` with a useful message on any mismatch.

The "copy to `results/vitis/`" step is a small piece of UX — the consumed directory (`csim_data_dir`) is buried in the Vitis project tree, while `results/vitis/` is colocated with `results/sim/` for easy diff. Worth keeping in your own steps.

---

## CSynthStep

```python
@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    description = "Run Vitis HLS C-synthesis and RTL co-simulation."
    consumes    = ["poly_cpp", "poly_hpp", "include_dir", "csim_data_dir"]
    produces    = {"report_dir": Path("waveflow_poly_proj/solution1")}
    params      = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, include_dir, csim_data_dir, live_output, clk_freq, **_) -> dict:
        vitis_env = {"WAVEFLOW_POLY_COSIM": "1",
                     "WAVEFLOW_POLY_TRACE_LEVEL": "none",
                     "WAVEFLOW_POLY_CLK_PERIOD_NS": f"{1e9 / clk_freq:g}"}
        try:
            result = toolchain.run_vitis_hls(
                config.root_dir / "run.tcl",
                work_dir=config.root_dir,
                capture_output=not live_output,
                env=vitis_env,
            )
            if result.stdout: print(result.stdout)
            if result.stderr: print(result.stderr)
        except Exception as exc:
            raise RuntimeError(str(exc))
        report_dir = config.root_dir / "waveflow_poly_proj" / "solution1"
        return {"report_dir": report_dir}
```

Mostly identical to `CSimStep` — same TCL, same toolchain wrapper, different env (`COSIM=1` triggers the synthesis branch in `run.tcl`). Differences worth noting:

- **`consumes` includes `csim_data_dir`** — C-synth depends on C-sim having validated first. This is policy: you could write a `CSynthStep` that consumes only the source files, but in practice you don't want to spend 5 minutes on synth when C-sim would have caught a 5-second bug.
- **`produces = {"report_dir": Path("waveflow_poly_proj/solution1")}`** is hard-coded to the project / solution names defined in `run.tcl`. If your TCL uses different names, edit the path. If you have multiple solutions, you need either multiple `CSynthStep` instances with different `produces`, or to parameterize via `expected_paths(config)` as the [`PySimStep` log_file pattern](./python.md#params-and-expected_paths-together-handle-a-config-driven-log-path) does.

---

## InspectSynthStep

```python
@dataclass(kw_only=True)
class InspectSynthStep(BuildStep):
    description = "Parse the Vitis HLS C-synthesis report and write results/loop_df.csv."
    consumes    = ["report_dir"]
    produces    = {"loop_df": Path("results/loop_df.csv")}
    params      = {}

    def run(self, config: BuildConfig, report_dir) -> dict:
        from waveflow.utils.csynthparse import CsynthParser

        if not report_dir.exists():
            raise RuntimeError(f"Solution directory not found: {report_dir}")

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

        loop_df_path = config.root_dir / "results" / "loop_df.csv"
        loop_df_path.parent.mkdir(parents=True, exist_ok=True)
        parser.loop_df.to_csv(loop_df_path, index=False)
        return {"loop_df": loop_df_path}
```

This is the closest thing to a reusable step in the Vitis path — it consumes a solution directory, runs `CsynthParser` on it, writes a CSV. The example-specific bit is the **assertion** (PipelineII > 1 fails the build). Per-design assertions vary: one design might fail on II > 1, another might check resource budgets, another might just emit the CSV without asserting. Don't try to make the assertion generic; copy this step and write the assertion you need.

If we extract anything to the framework first, it'll probably be this step in two pieces:
- `CsynthParseStep(report_artifact, output_path)` — pure parse + CSV emission, no policy.
- Per-design assertion as a separate step that consumes the resulting DataFrame as an in-memory artifact.

---

## Wiring the whole pipeline

The poly DAG composes all of the above with the codegen and Python-sim steps:

```python
def build_poly_dag() -> BuildDag:
    dag = BuildDag()

    # Source files
    dag.add(SourceStep(artifact="poly_source", path="poly.py"))

    # Build steps (groups expressed as docstring comments in poly_build.py)
    dag.add(BuildInputsStep(name="build_inputs"))                  # writes data/*.bin
    dag.add(PySimStep(name="py_sim"))                              # SimPy → results/sim/*
    dag.add(ExtractPyTimingStep(name="extract_py_timing"))         # py_timing.json
    dag.add(HlsGenIncludeStep(name="gen_include"))                 # codegen sub-DAG → include/*.h
    dag.add(HlsCodegenStep(name="gen_kernel", comp_class=PolyAccel, ...))
    dag.add(HlsCodegenStep(name="gen_tb",     comp_class=PolyTBHls, is_testbench=True, ...))
    dag.add(CSimStep(name="csim"))                                 # Vitis C-sim
    dag.add(FunctionalVerifyStep(name="validate_csim", ...))       # generic comparator
    dag.add(CSynthStep(name="csynth"))                             # Vitis C-synth + cosim
    dag.add(InspectSynthStep(name="inspect_synth"))                # parse csynth.xml
    dag.add(ExtractCosimTimingStep(name="extract_cosim_timing", top="poly"))
    dag.add(ValidateTimingStep(name="validate_timing"))            # py vs cosim cycles
    return dag
```

Then per-run:

```python
config = BuildConfig(root_dir=".", params={"clk_freq": 100e6, "nsamp": 100, ...})

# Just Python sim + extracted timing (no Vitis)
dag.run(config, through="extract_py_timing")

# Full build with cosim timing comparison
dag.run(config, through="validate_timing")
```

The CLI scaffolding around this is documented under [Python Simulation Pattern → CLI integration](./python.md#cli-integration); the same `main()` covers both Python-sim-only and full-Vitis runs because of `--through`.

---

## What's likely to be extracted first

When the second design lands and we have something to triangulate against, my best guess at the framework extraction order:

1. **A small `VitisRunStep` base class** — takes a TCL path, env dict, and named output directory, calls `toolchain.run_vitis_hls`, returns `{output_name: dir}`. `CSimStep` and `CSynthStep` reduce to a `VitisRunStep` subclass plus per-design env construction.
2. **`CsynthParseStep`** — the policy-free version of `InspectSynthStep`. Emits the loop and resource DataFrames as in-memory artifacts (or CSV).
3. **Env-var-prefix convention** — almost certainly something like `WAVEFLOW_<DESIGN>_*` formalized as a helper that builds the env dict from `config.params`.

`ValidateCSimStep` will probably never be extracted — too design-specific. The recipe stays as a copy-and-modify template.
