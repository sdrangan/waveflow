---
title: Core Components
parent: Build System
nav_order: 1
---

# Core Components

This page is the reference for everything the build framework exports from [waveflow/build/build.py](https://github.com/sdrangan/waveflow/tree/main/waveflow/build/build.py). Read top-to-bottom for the conceptual order: config → artifacts → result → step → DAG → incremental-rebuild model.

| Class / function | Purpose |
|---|---|
| `BuildConfig` | Per-build settings: root directory, Vitis version, calibration platform, free-form `params` dict |
| `BuildArtifact` (ABC) | Base for `FileArtifact` and `ObjectArtifact` |
| `FileArtifact` | A file on disk; freshness via mtime |
| `ObjectArtifact` | An in-memory Python object; freshness via creation timestamp |
| `source_artifact(path)` | Wrap an existing file as a `FileArtifact` (DAG-root convenience) |
| `BuildResult` | Outcome of one step's run — success/failure/skipped + produced artifacts |
| `BuildStep` (ABC) | Base class: declares `consumes` / `produces` / `params`, implements `run()` |
| `SourceStep` | Step subclass representing a source file at the root of the DAG |
| `Buildable` | Convenience base for steps that write fixed sets of generated text files |
| `BuildDag` | Owns steps, wires dependencies, runs in topological order |

---

## BuildConfig

Holds per-build configuration that every step can see.

```python
from waveflow.build.build import BuildConfig
from pathlib import Path

config = BuildConfig(
    root_dir="path/to/project",      # all relative paths are resolved against this
    vitis_version="2025.1",          # affects which compatibility files codegen emits
    platform="zynq7020_bfm_100mhz",  # the calibration platform (part + clock identity)
    part="xc7z020clg484-1",          # this build's FPGA part — confirmed against the platform
    clk_freq=100e6,                  # synthesis clock (Hz); its period drives HLS scheduling
    params={"nsamp": 100},           # free-form, consumed by steps via params
)
```

- `root_dir` defaults to `Path.cwd()`. Absolute paths in step `produces` declarations are used as-is; relative paths are joined with `root_dir`.
- `vitis_version` is parsed into `(major, minor)` via `config.vitis_version_tuple()`. `None` means "be conservative" — `config.needs_legacy_streamutils_cpp()` returns `True` in that case so `StreamUtilsStep` also writes `streamutils.cpp`.
- `params` is a free-form dict. Steps that declare a key in their `params` class attribute can read its value out of `config.params` at run time (see [BuildStep.params](#params) below).

### Platform selection

`platform` names a [calibration platform](../platform/identity.md) — the FPGA part + synthesis clock the build targets and calibrates for. When set, it is resolved at construction into `config.platform_info` (a `Platform`): an absent platform is created and **seeded** with `part` / `clk_freq`; an existing one is **confirmed** against them.

- `platform` — the platform name (a directory under `platforms_root`). `None` (default) = no platform: components degrade to the plain per-word timing.
- `part` — the `set_part` string, e.g. `"xc7z020clg484-1"`.
- `clk_freq` — synthesis clock in Hz. Its *period* drives HLS scheduling, so it (with the part) determines the cycle counts — this is why a platform is keyed by both.
- `platforms_root` — the build's primary platform root (write target, searched first); defaults to `calib/platforms` (project-local). Resolution falls back through the `WAVEFLOW_PLATFORM_PATH` env, a per-user library, and the reference platforms shipped in `waveflow/calib/platforms/` — so a `pip`-installed build resolves `zynq7020_bfm_100mhz` with no checkout.
- `allow_platform_mismatch` — when the selected platform's stored part/clock differ from this build's, warn instead of raising `PlatformMismatchError` (default `False` = raise).

`config.platform_info` is the single source both the calibration steps and codegen read — codegen's `set_part` / `create_clock` come from it (via `tcl_target`), so the synthesised part cannot drift from the calibrated one. See [Platforms](../platform/identity.md).

---

## BuildArtifact

The value produced by a step. Two concrete subclasses; you rarely instantiate them yourself — `BuildDag` constructs `FileArtifact` and `ObjectArtifact` for you based on what `step.run()` returns and what the step's `produces` declares.

### FileArtifact

A path on disk. Freshness is checked via the file's `stat().st_mtime`. `FileArtifact` is comparable to `Path` and delegates attribute access through, so you can treat it like a `Path` in most situations.

### ObjectArtifact

Wraps an arbitrary Python value with a creation timestamp. Used when a step's output is a `PolySimResult`, a `DataFrame`, or any other in-memory value. The freshness model treats object artifacts as **always rerun** — there is no way to mtime-check a Python object, so any step that produces one re-runs every build (and any downstream consumer that runs because of this also re-runs by cascade).

### source_artifact(path)

A small helper that wraps an existing source file as a `FileArtifact` with its mtime as the timestamp. Useful when constructing test fixtures or roots outside of a `SourceStep`.

---

## BuildResult

What `step.run()` returns. The DAG fills it in for you.

```python
@dataclass
class BuildResult:
    success: bool
    message: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    skipped: bool = False
```

- `success` is `False` when the step raised. `message` carries the exception text.
- `artifacts` maps each name declared in the step's `produces` to either a `Path` (file artifact) or a Python value (object artifact).
- `skipped=True` means the step was up-to-date and not actually re-run; `artifacts` still contains the expected paths.

Two convenience accessors are provided: `result.path(name)` and `result.object(name)` — both just look up `artifacts[name]`, so they're interchangeable but make intent clearer at the call site.

---

## BuildStep

The abstract base for every node in the DAG.

```python
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from waveflow.build.build import BuildStep, BuildConfig

@dataclass(kw_only=True)
class MyStep(BuildStep):
    description: str = "Short, human-readable summary of what this step does."
    consumes: ClassVar[list] = ["upstream_artifact_a", "upstream_artifact_b"]
    produces: ClassVar[dict] = {
        "my_output_file": Path("subdir/output.bin"),   # file artifact (relative to root_dir)
        "my_object":      None,                        # in-memory artifact (no path)
    }
    params: ClassVar[dict] = {"nsamp": 100, "live_output": False}   # name → default

    def run(self, config: BuildConfig,
            upstream_artifact_a, upstream_artifact_b,    # consumed by name
            nsamp, live_output,                           # params by name
            **_) -> dict:
        # ... do work ...
        return {
            "my_output_file": config.root_dir / "subdir/output.bin",
            "my_object": some_python_value,
        }
```

The four class attributes tell the DAG everything it needs to wire dependencies and inject inputs.

### description

A short human-readable string used by `dag.info()` / `dag.describe()` and the `--list-steps-verbose` CLI output. Settable as a class attribute (no annotation needed) or per-instance via the dataclass field.

### consumes

A list of artifact names produced by upstream steps. When this step is added to a DAG, the DAG looks up each name in its registry of artifact owners and wires the producing step as a dependency. Names referencing artifacts no step produces raise `ValueError` at `dag.add()` time — there's no late binding.

### produces

A `dict` from artifact name to one of:
- `Path("relative/path.ext")` — file artifact at that path (resolved against `config.root_dir`).
- `Path("/abs/path.ext")` — file artifact at an absolute path.
- `None` — in-memory artifact (no file on disk).
- A string `"other_artifact_name"` — alias: this step produces the same path as another already-registered artifact. Used by `CSimStep` to express "Vitis writes back into the same `data_dir` that `BuildInputsStep` created."

Each name in `produces` must appear in the dict returned from `run()`, or the DAG raises `RuntimeError`. No silent under-production.

### params

A `dict` from param name to default value. At run time the DAG resolves each param against `config.params` (falling back to the declared default if absent) and injects the value as a keyword argument into `run()`. This is how steps consume configuration without each one being constructed with its own settings — the `BuildConfig.params` dict is the single source.

### run(config, **kwargs)

The actual work. `kwargs` contains:
- One key per `consumes` name, bound to the upstream step's produced value (a `Path` for file artifacts, a Python value for object artifacts).
- One key per `params` name, resolved against `config.params`.

Must return a `dict[str, Any]` covering every name in `produces`. Raise `RuntimeError` (or let any exception propagate) to signal failure — the DAG catches it, records a failed `BuildResult`, and halts the rest of the build.

### expected_paths(config)

Optional override. Returns a `dict[str, Path]` for any file artifact whose path depends on `config.params` rather than being a static `Path` declared in `produces`. The DAG calls this during pre-build path resolution (used by `--list-artifacts` and `--status`). See `PySimStep` in [examples/stream_inband/poly_build.py](https://github.com/sdrangan/waveflow/tree/main/examples/stream_inband/poly_build.py) for the canonical example: its log file path comes from `config.params["log_file"]`.

---

## SourceStep

The root node for any source file the DAG depends on. Pre-built and ready to use:

```python
from waveflow.build.build import SourceStep

dag.add(SourceStep(
    artifact="poly_cpp",
    path="poly.cpp",                                  # relative to config.root_dir
    description="Top-level Vitis HLS kernel source.",
))
```

`SourceStep.run()` simply checks the file exists and returns its `Path` as the named artifact. Downstream steps that declare `consumes=["poly_cpp"]` receive that path. The mtime is used for freshness checks against everything downstream — touch the source file, and the next build re-runs every step that transitively depends on it.

Use `SourceStep` as the DAG-roots convention. Don't manually pass paths into steps — wire them through the DAG so freshness propagation works.

---

## Buildable

`Buildable` is a `BuildStep` subclass that provides a fixed shape for the common case of "write a fixed set of named text files generated as strings." Subclasses declare a `build_outputs` property (a dict of name → relative `Path`) and a `generate(key, config) -> str` method that returns the contents of each named file. The default `run()` iterates over `build_outputs` and writes each file.

Used by the codegen steps (`DataSchemaStep`, `StreamUtilsStep`, `MemMgrStep`, `ArrayUtilsStep`). Buildable steps **always run** under the DAG (the freshness check is bypassed for them — see [Incremental rebuild](#incremental-rebuild) below); writing a handful of small generated text files is cheap, and downstream steps still skip on freshness when these outputs land unchanged.

Use `Buildable` for steps that fit its shape (text files, no upstream artifact consumption, no in-memory outputs). Use `BuildStep` directly with explicit `consumes` / `produces` for everything else.

See [Code Generation Steps](./codegen.md) for the steps that use `Buildable`.

---

## BuildDag

Owns the set of steps and runs them.

```python
from waveflow.build.build import BuildDag

dag = BuildDag()
dag.add(SourceStep(artifact="src", path="my.py"))
dag.add(MyStep(name="my_step"))
```

### add(step)

Registers `step`. Three things happen:
1. **Name uniqueness check.** Duplicate `step.name` raises `ValueError`.
2. **Produces registration.** Each name in `step.produces` is recorded as owned by this step; duplicate ownership raises `ValueError`.
3. **Consumes resolution.** Each name in `step.consumes` is looked up; if no registered step produces it, `ValueError`. Otherwise the producing step is wired as a dependency.

Returns `step`, so you can chain: `s = dag.add(MyStep(name="..."))`.

Steps must be added in an order consistent with their dependencies (consumed artifacts must already be produced by some prior `add()` call). The DAG does not buffer pending dependencies for later resolution.

### run(config, through=None, force=False, on_step_begin=None, on_step_end=None)

Executes the DAG in topological order. Returns `dict[str, BuildResult]` keyed by step name.

| Parameter | Behaviour |
|-----------|-----------|
| `config` | `BuildConfig` shared by every step. |
| `through="step_name"` | Run only that step and its transitive dependencies. Raises `ValueError` for unknown names. |
| `force=True` | Force every step to re-run, ignoring freshness checks. |
| `force=["a", "b"]` | Force those named steps. Downstream steps re-run via cascade as their inputs become newer. |
| `force=False` (default) | Skip steps whose produced files are up-to-date with their consumed files. |
| `on_step_begin(step, will_run, paths)` | Fires before each step. `will_run=False` means the step is being skipped. `paths` maps each declared file artifact to its resolved `Path`. |
| `on_step_end(step, result)` | Fires after each step with its `BuildResult`. |

If a step fails, execution stops — subsequent steps are not invoked. Their `BuildResult` is simply absent from the returned dict.

### Introspection

The DAG offers a few read-only views useful for CLIs, AI tools, and debugging:

- `dag.step_names()` — step names in topological order.
- `dag.steps()` — `BuildStep` objects in topological order.
- `dag.artifact_owners()` — `{artifact_name: producing_step_name}` in add() order.
- `dag.artifact_paths(config)` — `{artifact_name: resolved_Path}` for every file artifact in the DAG.
- `dag.info()` — list of `{step, description, consumes, produces, params}` dicts. Machine-readable.
- `dag.describe()` — same data as a Markdown table.
- `dag.results_status(config)` — pre-build freshness status: per file artifact, `{path, exists, mtime, stale, stale_because}`.

These are the primitives behind `--list-steps`, `--list-artifacts`, `--status`, and the verbose listing in the [poly CLI](https://github.com/sdrangan/waveflow/tree/main/examples/stream_inband/poly_build.py).

---

## Incremental rebuild

The freshness model is mtime-based with two propagation rules. The DAG computes which steps must actually run before running anything, so a step that's skipped never even starts.

### Decision rule for a single step

A step runs if any of the following are true:

1. It is in the forced set (`force=True` or the step's name is in the `force=[...]` iterable).
2. It is a `Buildable` step (Buildable doesn't participate in skip logic — those steps always run).
3. **Files are stale:** any of its produced files are missing, or any of its consumed files are newer than its oldest produced file.
4. **Cascade:** any of its direct dependencies must run (computed by transitively applying rules 1–5).
5. **In-memory demand:** it produces an `ObjectArtifact` (declared as `None` in `produces`) that is consumed by some other step that must run. Object artifacts have no on-disk representation to mtime-check, so anyone downstream forces them to recompute.

Rules 4 and 5 propagate to a fixed point — `BuildDag._determine_must_run` loops until the must-run set stops growing.

### What this means in practice

- Touching a source file (`SourceStep`'s path) forces every step transitively downstream to re-run.
- A step that produces only in-memory artifacts always re-runs, but only if something downstream needs it. If you `dag.run(config, through="gen_include")` and the in-memory step isn't on the path to `gen_include`, it stays idle.
- A step that produces files but whose files are up-to-date is genuinely skipped — its `BuildResult` has `skipped=True` and `success=True`, and its artifact paths are populated (so downstream steps can still find the files) without re-invoking the step's `run()`.

### Forcing a specific step

```python
dag.run(config, force=["csim"])
```

Re-runs `csim` even if its outputs are fresh. Downstream steps re-run if their inputs (now newer) trigger rules 3–5 — typically yes.

The poly CLI exposes this via `--force-step csim` (can be passed multiple times) and `--force` (force everything).
