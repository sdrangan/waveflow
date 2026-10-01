---
title: Python Simulation Pattern
parent: Build System
nav_order: 3
summary: "A recipe rather than a step: how to write the Python half of a build -- writing each scenario's stimulus and expected response, running a pure model and pysim on them, checking each stage against the expected responses, and extracting pysim's cycle estimate -- taken from the streaming polynomial example's poly_build.py. Waveflow ships no generic version because every design has its own scenarios, model and result format; the CLI is shared (run_dag_cli)."
---

# Python Simulation Pattern

Waveflow does not ship a generic "run a simulation" build step: every design has its own
scenarios, model and result format, and a generic step would either need a long parameter
list or force one shape on all of them.  This page is the **pattern** instead -- the Python
half of the [Streaming polynomial](../../examples/stream_inband/index.md) example's
[`poly_build.py`](https://github.com/sdrangan/waveflow/tree/main/examples/stream_inband/poly_build.py),
step by step, so it can be copied.  The Vitis half is the [Vitis Pattern](./vitis.md).

---

## The shape: one stimulus, an expected response, every stage checked against it

The Python half has five kinds of step:

| Step | What it does | Writes |
| --- | --- | --- |
| `ScenariosStep` | each scenario's stimulus and **expected** response, from its intent | `data/<scenario>/in/`, `expected/` |
| `ModelStep` | the pure bit-exact model on every scenario | `data/<scenario>/model/` |
| `PySimStep` | pysim -- the module's Python body with its timing model | `data/<scenario>/pysim/`, the event log |
| `CheckStep` | one stage's recorded responses vs the expected ones | `results/check_<stage>.json` |
| `ExtractPyTimingStep` | pysim's cycle count for the timing scenario | `results/py_timing.json` |

Every stage -- the model, pysim, and later csim and cosim -- writes its response **in the same
format, beside the same stimulus**, and the same checker compares each with the expected
response.  The expected response is computed from what the scenario *means* (its intent), not
by running any implementation: two implementations written by the same person, or the same
AI, can agree on the same mistake.

---

## Writing the scenarios

```python
@dataclass(kw_only=True)
class ScenariosStep(BuildStep):
    description = "Write every scenario's stimulus and expected response (scenarios.py)."
    consumes = ["poly_source", "scenarios_source"]
    produces = {"data_dir": Path("data"), "scenario_list": Path("data/scenarios.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        data = config.root_dir / "data"
        S.write_scenarios(data)
        return {"data_dir": data, "scenario_list": data / "scenarios.txt"}
```

- **`consumes` names the two source files**, though `run()` never reads them -- they are
  imported at module load.  Declaring them is what makes **editing `poly.py` or
  `scenarios.py` invalidate everything downstream**: freshness is by modification time.
- The step is thin on purpose.  What a scenario contains, and how its expected response is
  computed, lives in `scenarios.py`, a plain module that tests and notebooks import too.
- Stimulus is written as burst bundles (`write_bursts`), which the C++ testbench reads with
  `wf::play_stream` -- see [Body-only kernels](../custom_hooks/body_only.md#testing-one-stimulus-every-implementation).

---

## Running the model and pysim

```python
@dataclass(kw_only=True)
class ModelStep(BuildStep):
    description = "Run the pure bit-exact model (poly_stream_model) on every scenario."
    consumes = ["scenario_list"]
    produces = {"model_done": Path("results/model_done.txt")}
    params = {}

    def run(self, config: BuildConfig, scenario_list, **_) -> dict:
        data = config.root_dir / "data"
        names = Path(scenario_list).read_text(encoding="utf-8").split()
        for name in names:
            d = data / name
            coeffs = CoeffArray().read_uint32_file(d / "coeffs.bin").val
            res = poly_stream_model(read_bursts(d / "in"), coeffs)
            write_bursts(res.out, d / "model")
            _write_status(d / "model" / "status.json", res.status())
        done = config.root_dir / "results" / "model_done.txt"
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("\n".join(names) + "\n", encoding="utf-8")
        return {"model_done": done}
```

`PySimStep` has the same shape, but builds a `Simulation` per scenario -- the module, its
testbench, a clock, and on the timing scenario a `Logger` -- and runs it:

```python
        for name in S.WELL_FORMED:
            d = data / name
            sim = Simulation()
            clk = Clock(freq=clk_freq)
            logger = (Logger(name="poly_log", sim=sim, file_path=log_path, fields=["event", "job"])
                      if name == "timing" else None)
            accel = PolyAccel(name="poly_accel", sim=sim, clk=clk, unroll_factor=unroll_factor,
                              **({"logger": logger} if logger else {}))
            tb = PolyTB(name="poly_tb", sim=sim, stimulus=d / "in",
                        coeffs=CoeffArray().read_uint32_file(d / "coeffs.bin").val,
                        n_out=len(read_bursts(d / "expected")))
            connect(sim, tb, accel, clk)
            sim.run_sim()
            write_bursts(tb.out, d / "pysim")
            _write_status(d / "pysim" / "status.json", tb.status)
```

Things to copy:

- **A "done" marker file is the produced artifact.**  The real outputs are spread over
  `data/<scenario>/<stage>/`, one directory per scenario.  A single small file written last
  gives the DAG one thing to check for freshness, and lists what ran.
- **pysim runs only the well-formed scenarios.**  The model is the reference for the
  malformed ones -- an early TLAST, a missing TLAST.  pysim exists for the timing model,
  and timing is only meaningful on transactions the kernel accepts.
- **Parameters arrive as keyword arguments.**  Every name in `params` is injected into
  `run()`, from `BuildConfig.params` or the default.  `**_` swallows the ones a step does
  not use; keep it, so adding a parameter never breaks a signature.

---

## Checking a stage

One step class checks every stage; the DAG gets one instance per stage:

```python
@dataclass(kw_only=True)
class CheckStep(BuildStep):
    stage: str
    done_artifact: str
    only: tuple[str, ...] | None = None
    params = {}

    @property
    def consumes(self) -> list:
        return [self.done_artifact, "scenario_list"]

    @property
    def produces(self) -> dict:
        return {f"check_{self.stage}": Path(f"results/check_{self.stage}.json")}

    def run(self, config: BuildConfig, **_) -> dict:
        report = S.check(config.root_dir / "data", self.stage,
                         list(self.only) if self.only else None)
        ...   # write the report, print PASS/FAIL per scenario
        if failed:
            raise RuntimeError(f"{self.stage}: {len(failed)} scenario(s) differ from the "
                               f"expected response: {sorted(failed)}")
        return {f"check_{self.stage}": out}

dag.add(CheckStep(name="check_model", stage="model", done_artifact="model_done"))
dag.add(CheckStep(name="check_pysim", stage="pysim", done_artifact="pysim_done",
                  only=S.WELL_FORMED))
```

- **`consumes` and `produces` are properties** here, because they depend on the instance.
  That is how one class serves several stages without the artifact names colliding.
- **The check is its own step**, not the tail of the simulation step.  A mismatch then fails
  a step whose name says what failed (`check_pysim`), and the simulation's outputs stay on
  disk to inspect.
- **Raising `RuntimeError` fails the build** and puts the message in `BuildResult.message`.

---

## Extracting the cycle estimate

```python
@dataclass(kw_only=True)
class ExtractPyTimingStep(BuildStep):
    description = "Extract the timing scenario's cycle count from the pysim event log."
    consumes = ["log"]
    produces = {"py_timing": Path("results/py_timing.json")}
    params = {"clk_freq": 100e6}

    def run(self, config: BuildConfig, log, clk_freq, **_) -> dict:
        events: dict[str, float] = {}
        with open(log, newline="") as f:
            for row in csv.DictReader(f):
                events.setdefault(row["event"], float(row["time"]))
        t0, t1 = events.get("samp_read_begin"), events.get("samp_out_write_end")
        if t0 is None or t1 is None:
            raise RuntimeError(f"missing timing events in {log}: {sorted(events)}")
        ...   # write {"transaction_cycles": round((t1 - t0) * clk_freq), ...}
        return {"py_timing": out}
```

The event names are the ones the module's Python body logs.  The result's format is the one
the framework's `ValidateTimingStep` compares with the co-simulated count -- see
[Vitis Pattern](./vitis.md#timing-pysim-against-cosim).

---

## When to use in-memory artifacts instead

A `produces` entry of `None` declares an **in-memory** artifact: `run()` returns the Python
object itself, and the consuming step receives it with no file in between.

```python
@dataclass(kw_only=True)
class SweepModelStep(BuildStep):
    consumes = ["scenario_list"]
    produces = {"model_result": None}             # None = in-memory

    def run(self, config, scenario_list, **_):
        return {"model_result": run_everything(scenario_list)}
```

The freshness model treats an in-memory artifact as always stale, and the steps that consume
it re-run by cascade.  That is the right trade when nothing outside Python reads the result.
The polynomial example writes files because Vitis has to read the same stimulus and the
checker has to read every stage's output.

---

## CLI integration

`run_dag_cli` (in `waveflow.build.cli`) gives a build script the whole command line without a
hand-written `main()`:

```python
def main() -> None:
    run_dag_cli(
        build_poly_dag,
        description="Build the streaming polynomial accelerator.",
        default_through="check_pysim",
        root_dir=_SOURCE_DIR,
        extra_args=[
            (("--clk-freq",), {"type": float, "default": 100e6, "metavar": "HZ"}),
            (("--unroll-factor",), {"type": int, "default": 1}),
            (("--live-output",), {"action": "store_true"}),
        ],
        params_from_args=lambda a: {"clk_freq": a.clk_freq, "unroll_factor": a.unroll_factor,
                                    "live_output": a.live_output},
    )
```

It provides `--through STEP`, `--force`, `--force-step STEP`, `--list-steps`,
`--list-steps-verbose`, `--list-artifacts` and `--status`, plus whatever `extra_args` adds,
packed into `BuildConfig.params` by `params_from_args`.

**`--through` runs only the target's ancestors.**  A step that is not upstream of the target
does not run, even if it is stale -- `--through gen_kernel` does not regenerate `include/`,
because nothing in `gen_kernel`'s ancestry consumes it.  Give the pipeline one final step that
depends on every check (the example's `summary`) and build through that.
