---
title: System simulation
parent: Register mapped simple function
nav_order: 3
has_children: false
---
# System simulation

With the [kernel and the host](./python.md) written, you can simulate the **whole system** in Python —
and confirm it works — before writing a single line of testbench.

This is the first of the example's [two ways to simulate](./index.md#two-ways-to-simulate-it). Here a
host `SimObj` runs **concurrently** with the kernel, exchanging real AXI-Lite transactions over a
`DirectMMIF` link: it writes `x`/`a`/`b`, asserts `ap_start`, polls `ap_done`, and reads `y`. It is the
only path in this example that exercises the register-map protocol — the thing this example is *about* —
and the only one that yields a per-step event trace.

It is also **Python-only**, and that is fundamental rather than a gap: the host and the kernel are two
independent processes, and a Vitis C++ testbench is a single straight-line `int main()`, so there is
nothing for that concurrency to lower onto. Taking a concurrent system model down to RTL needs the
XSI / SystemC path — future work. The [sequential path](./seqtb.md) is the one that carries on into
C-simulation and co-simulation.

## Wiring the system

`simulate_case()` in [`simp_fun.py`](../../../examples/regmap/simp_fun.py) builds the system: a
`Simulation`, the kernel, the host, and the link between them.

```python
def simulate_case(case, *, clk_freq=100e6, latency_cycles=4, log_file=None):
    sim = Simulation()
    clk = Clock(freq=clk_freq)
    accel = SimpFun(name="simp_fun", sim=sim, clk=clk, latency_cycles=latency_cycles, ...)
    host  = SimpFunHost(name="host", sim=sim, case=case, clk=clk, ...)
    connect(sim, host, accel, clk)
    sim.run_sim()
    return SimpFunSimResult(case=case, y=int(host.y), ap_done=int(host.ap_done),
                            passed=bool(host.passed))
```

`connect()` is the wiring — it binds the host's `MMIFMaster` to the kernel's `VitisRegMapMMIFSlave`
through a `DirectMMIF`, the AXI-Lite link:

```python
def connect(sim, host, accel, clk):
    lite_link = DirectMMIF(sim=sim, clk=clk, byte_addressable=True)
    lite_link.bind("master", host.master)
    lite_link.bind("slave", accel.s_lite)
    host._regmap_ref = accel.regmap
```

Neither object reaches into the other: the host issues bus transactions, the slave services them and
launches `on_start` when `ap_start` is written. The kernel has no idea a host exists — exactly as in
hardware.

The verdict comes from the host itself. `SimpFunHost.run_proc` ends with:

```python
self.y = yield from rm.get("y")
self.passed = self.y == self.case.expected_y and self.ap_done == 1
```

so a run is only "passed" if the value is right **and** the kernel actually signalled completion.

## Running it in the build DAG

A **build DAG** is Waveflow's pipeline abstraction: a directed acyclic graph of typed steps, where each
step declares the artifacts it **consumes** and **produces**. The DAG works out the order, wires
consumers to producers, skips work that is already fresh, and lets you run any prefix. One pipeline
carries the design from inputs through Python simulation, code generation, C-simulation, synthesis, and
co-simulation. See [Build System](../../guide/build/index.md) for the full reference.

The system simulation is `SystemSimStep`:

```python
# examples/regmap/simp_fun_build.py
@dataclass(kw_only=True)
class SystemSimStep(BuildStep):
    description = "Run the Python-only system simulation (host SimObj + DUT) and verify it."
    consumes = ["simp_fun_source", "x_in", "a_in", "b_in"]
    produces = {
        "system_sim":     Path("results/system_sim.json"),
        "system_sim_log": Path("results/system_sim_log.csv"),
    }
    params = {"clk_freq": 100e6, "latency_cycles": 4}
```

Two things are worth pulling out of that contract:

- **It consumes the same `x_in`/`a_in`/`b_in`** the `SeqTB` golden and the C-simulation use. Both
  simulation paths are driven by identical stimulus, so they independently produce the same `y`.
- **It fails the build when the run does not verify.** The step raises if `passed` is false, so *"it
  ran"* is never mistaken for *"it is correct"* — the checkpoint is real.

Declaring `consumes` is all the wiring there is: the DAG sees `x_in` is produced by `BuildInputsStep`,
runs that first, and injects the paths into `run()` as keyword arguments. `params` holds the tunables
(the test vector, the clock), overridable at the CLI (`--x 5 --a 3 --b -4`) or via `BuildConfig`.

## What it produces

Run it:

```bash
cd examples/regmap
python simp_fun_build.py --through system_sim
```

`--through STEP` means "run everything `STEP` needs, then stop" — here `build_inputs → system_sim`,
skipping code generation and Vitis entirely. Then inspect the two artifacts.

**`results/system_sim.json`** — the verdict:

```json
{
  "x": 5, "a": 3, "b": -4,
  "expected_y": 11,
  "y": 11,
  "ap_done": 1,
  "passed": true
}
```

This is the confirmation the whole page exists for: the Python model computes the right answer, over the
real register protocol, and the kernel signalled done. At this point the design works — before any
testbench, any C++, or any Vitis.

**`results/system_sim_log.csv`** — the protocol trace:

```
time,event,value
0,ap_start_host,1
0,kernel_busy,1
4e-08,kernel_done,1
6e-08,host_done,1
```

## Reading the trace

Those four rows are the register-map handshake, timed. At 100 MHz (10 ns per cycle):

| event | time | cycle | what happened |
|---|---|---|---|
| `ap_start_host` | 0 | 0 | the host writes `ap_start` |
| `kernel_busy` | 0 | 0 | the slave launches `on_start` |
| `kernel_done` | 40 ns | 4 | the kernel finishes — its `latency_cycles` |
| `host_done` | 60 ns | 6 | the **host observes** `ap_done` |

The gap between the last two is the lesson: the kernel is done at **4** cycles, but the host does not
find out until **6**, because it polls every `poll_interval_cycles` and catches the flag on its second
read. That polling overhead is real — it is what a driver actually pays — and it is invisible to the
[sequential path](./seqtb.md), which reports the kernel transaction alone (4 cycles). Neither number is
wrong; they measure different things, and this trace is what lets you see the difference rather than be
told it.

The events come from `@sim_only` log calls on both sides of the link:

```python
# host (SimpFunHost.run_proc)          # kernel (SimpFun.on_start)
self._log("ap_start_host", 1)          self._log("kernel_busy", 1)
self._log("host_done", int(self.ap_done))   self._log("kernel_done", 1)
```

`_log` is a thin `@sim_only` wrapper around the `Logger`. The decorator tells the codegen extractor
"never lower this to C++" — these calls exist only in simulation, which is why the kernel can carry
instrumentation without it leaking into the generated hardware. The `Logger` timestamps each row with
the simulation time automatically.

> **Where the RTL-comparison timing comes from.** Not this page. The `py_timing` artifact that
> [`validate_timing`](./rtlsim.md) checks against the co-simulation is produced by the **`SeqTB`** on the
> [next page](./seqtb.md) — it times the kernel transaction, which is the like-for-like counterpart of
> what the RTL co-simulation measures. The trace here is the *protocol* story: per-step, host-side, and
> Python-only.

## Next

- [Sequential execution](./seqtb.md) — the `SeqTB`: one program that invokes the kernel, and the path to C-sim and co-sim.
- [Vitis HLS Code Generation](./codegen.md) — generating the kernel and testbench C++ from the same Python source.
- [C and RTL Simulation](./rtlsim.md) — running the Vitis flows and validating the measured cycles.
