---
title: Python Simulation Pattern
parent: Build System
nav_order: 3
---

# Python Simulation Pattern

Waveflow doesn't ship a generic "run a SimPy simulation" build step — every design has its own components, testbench, and result format, and a generic step would either need a long parameter list or constrain you to one shape. Instead this page is the **pattern**: a worked recipe for writing your own `BuildStep` that runs a Python simulation, derived from the poly accelerator's [`PySimStep`](https://github.com/sdrangan/waveflow/tree/main/examples/stream_inband/poly_build.py).

When Waveflow has more than one example that follows this pattern, the common scaffolding will be extracted into a framework-level base class. Until then, copy this recipe.

---

## Anatomy of a simulation step

A Python simulation step typically:

1. **Reads input data** from upstream artifacts (file paths produced by an input-building step, or in-memory objects from a generator step).
2. **Constructs the simulation** — instantiates components, testbench, clocks, loggers, and wires interfaces.
3. **Runs `sim.run_sim()`**.
4. **Writes results** — either to disk (so the C-sim validation step can compare against them) and/or as an in-memory object (so a timing-validation step can analyse the log without re-parsing).
5. **Declares all produced artifacts** in the returned dict.

The poly version handles inputs as files (so the same data feeds both Python sim and Vitis C-sim) and emits both file artifacts (response binaries) and a file log (for downstream timing analysis):

```python
@dataclass(kw_only=True)
class PySimStep(BuildStep):
    description = "Run the Python SimPy simulation and write results to results/sim/."
    consumes    = ["poly_source", "coeffs", "data_cmd_hdr", "samp_in"]
    produces    = {"sim_dir": Path("results/sim"),
                   "log":     Path("results/sim_log.csv")}
    params      = {"clk_freq": 100e6, "in_bw": 32, "out_bw": 32,
                   "unroll_factor": 1, "log_file": "results/sim_log.csv"}

    def expected_paths(self, config: BuildConfig) -> dict[str, Path]:
        log_file = config.params.get("log_file", self.params["log_file"])
        return {"log": config.root_dir / log_file}

    def run(self, config: BuildConfig,
            coeffs, data_cmd_hdr, samp_in,
            clk_freq, in_bw, out_bw, unroll_factor, log_file,
            **_) -> dict:
        cmd_hdr_obj = PolyCmdHdr().read_uint32_file(data_cmd_hdr)
        samp_in_arr = np.array(
            read_uint32_file(samp_in, elem_type=Float32, shape=int(cmd_hdr_obj.nsamp)),
            dtype=np.float32,
        )
        coeffs_obj = CoeffArray().read_uint32_file(coeffs)
        sim = Simulation()
        clk = Clock(freq=clk_freq)
        log_path = config.root_dir / log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger = Logger(name="poly_log", sim=sim, file_path=log_path,
                        fields=["event", "job"])
        accel = PolyAccel(
            name="poly_accel", sim=sim,
            in_bw=in_bw, out_bw=out_bw, unroll_factor=unroll_factor,
            clk=clk, logger=logger,
        )
        tb = PolyTB(name="poly_tb", sim=sim,
                    cmd_hdr=cmd_hdr_obj, samp_in=samp_in_arr,
                    coeffs=np.asarray(coeffs_obj.val, dtype=np.float32),
                    word_bw=in_bw)
        connect(sim, tb, accel, clk)
        sim.run_sim()

        sim_dir = config.root_dir / "results" / "sim"
        sim_dir.mkdir(parents=True, exist_ok=True)
        tb.resp_hdr.write_uint32_file(sim_dir / "resp_hdr.bin")
        write_uint32_file(tb.samp_out, elem_type=Float32,
                          file_path=sim_dir / "samp_out.bin", nwrite=len(tb.samp_out))
        (sim_dir / "regmap_status.json").write_text(
            json.dumps({"halted": int(tb.halted), "error": int(tb.error),
                        "tx_id": int(tb.tx_id_status)}, indent=2),
            encoding="utf-8",
        )
        return {"sim_dir": sim_dir, "log": log_path}
```

---

## Walking through the design choices

### `consumes` lists three artifacts

- `poly_source` is a `SourceStep` artifact pointing at `poly.py` itself. It's not used inside `run()` — the import happens at module load. But declaring it as a consumed artifact means **touching `poly.py` invalidates the simulation results**, which is what you want: the mtime of the source file gates the freshness of every downstream simulation output.
- `coeffs`, `data_cmd_hdr`, and `samp_in` are the binary input files produced by `BuildInputsStep`. They arrive as `Path` objects.

### `produces` mixes a directory and a file

- `sim_dir = Path("results/sim")` declares a directory artifact. The DAG's freshness check (`_path_mtime`) understands directories: it walks them recursively and takes the max mtime of any file inside. So `sim_dir` is "fresh" when every file under it is newer than every consumed file.
- `log = Path("results/sim_log.csv")` declares the CSV log explicitly so `ValidateTimingStep` can consume it as a file artifact.

### `params` and `expected_paths` together handle a config-driven log path

The log filename is configurable per build (`config.params["log_file"]`), but it must still be declared as a file artifact so freshness works. The pattern:

1. Declare the static fallback in `params`.
2. Declare a placeholder in `produces` so the DAG knows the artifact name.
3. Override `expected_paths(config)` to return the actual path resolved against `config.params`.

The DAG calls `expected_paths()` after the static `produces` declaration, and the override takes precedence. This is the only mechanism for artifacts whose path can't be expressed as a plain `Path` literal.

### `run()` reads input by Path, writes output by Path

Even though the simulation runs entirely in memory, the contract for downstream steps is files on disk — the C-sim validation step in particular needs to read the same binaries Vitis produces and compare. So the in-memory results are serialized at the end via `write_uint32_file`.

### The `**_` swallows unconsumed params

The DAG injects every declared param as a kwarg. `**_` catches any future param additions without forcing a `run()` signature change. Use it.

---

## When to use in-memory artifacts instead

If a downstream step is purely Python (no Vitis-comparable output) and the simulation result is large or expensive to serialize, return an `ObjectArtifact`-style value instead of writing to disk:

```python
@dataclass(kw_only=True)
class PySimStep(BuildStep):
    consumes = ["cmd_hdr", "samp_in"]
    produces = {"sim_result": None,            # None = in-memory artifact
                "log":        Path("results/sim_log.csv")}
    params   = {"clk_freq": 100e6}

    def run(self, config, cmd_hdr, samp_in, clk_freq, **_):
        # ... run sim ...
        result = PySimResult(resp_hdr=tb.resp_hdr,
                             samp_out=tb.samp_out,
                             halted=tb.halted, error=tb.error,
                             tx_id=tb.tx_id_status)
        return {"sim_result": result, "log": log_path}


@dataclass(kw_only=True)
class AnalyseSimStep(BuildStep):
    consumes = ["sim_result"]
    produces = {"summary": Path("results/sim_summary.json")}

    def run(self, config, sim_result, **_):
        # sim_result is the actual Python object, no I/O
        ...
```

The freshness model treats in-memory artifacts as always-rerun. Downstream consumers also re-run by cascade if they need the value. This is the right trade-off when serialisation cost would dominate sim time.

The poly example uses **files** for the simulation outputs because the C-sim step needs to read them anyway — once you're paying for serialization, you get to skip re-running on subsequent builds. The choice is per-step: files for cross-tool exchange, in-memory for Python-only consumers.

---

## Validating timing in a downstream step

A common companion to a simulation step is one that reads the simulation log and asserts on event durations. The poly `ValidateTimingStep` is small and shows the pattern:

```python
@dataclass(kw_only=True)
class ValidateTimingStep(BuildStep):
    description = "Verify timing events in the simulation log."
    consumes    = ["log"]
    produces    = {"durations": Path("results/durations.json")}
    params      = {}

    def run(self, config: BuildConfig, log) -> dict:
        events: dict[str, float] = {}
        with open(log, newline="") as f:
            for row in csv.DictReader(f):
                ev = row["event"]
                if ev not in events:
                    events[ev] = float(row["time"])
        t_start = events.get("samp_read_begin")
        t_end = events.get("samp_out_write_end")
        if t_start is None or t_end is None:
            raise RuntimeError(f"Missing timing events in log: {list(events)}")
        durations = {"samp_read_to_write_end": t_end - t_start}
        durations_path = config.root_dir / "results" / "durations.json"
        durations_path.parent.mkdir(parents=True, exist_ok=True)
        durations_path.write_text(json.dumps(durations, indent=2), encoding="utf-8")
        return {"durations": durations_path}
```

Notes:
- `RuntimeError` on a missing event halts the build and surfaces the message in `BuildResult.message`.
- The step writes a small JSON for downstream consumption (a report, a CI check, a notebook). Even if nothing consumes it today, having the artifact gives you something to point at in `results_status()`.
- `consumes = ["log"]` is everything — no source files, no params. The step is fully determined by the log it reads.

---

## CLI integration

If you want a `poly_build.py` style CLI on top of your DAG, the [poly version](https://github.com/sdrangan/waveflow/tree/main/examples/stream_inband/poly_build.py) is the reference:

- `--through STEP` → `dag.run(config, through=STEP)`
- `--force` / `--force-step STEP` → `dag.run(config, force=...)`
- `--list-steps`, `--list-steps-verbose` → `dag.step_names()` / `dag.info()`
- `--list-artifacts` → `dag.artifact_paths(config)` + `dag.artifact_owners()`
- `--status` → `dag.results_status(config)`
- Per-param flags (`--nsamp`, `--clk-freq`, etc.) → packed into `BuildConfig(params={...})`
- `on_step_begin` / `on_step_end` callbacks → progress printing

That CLI scaffolding doesn't change per-example, so when we have a second simulation example, the CLI plumbing (and probably the timing-validation pattern) will be the first thing extracted into a framework helper. Until then, copy `main()` from poly.
