---
title: Sequential execution
parent: Register Map (simple function)
nav_order: 4
has_children: false
---
# Sequential execution

The [system simulation](./pysim.md) confirmed the design works. Now we write the testbench that will
carry it into Vitis.

This is the second of the example's [two ways to simulate](./index.md#two-ways-to-simulate-it). Where
the system simulation runs a host *concurrently* with the kernel, this path is **one sequential
program** — a `SeqTB` — that reads the inputs, invokes the kernel, and writes the result. Because that
is precisely the shape of a Vitis C++ testbench (a straight-line `int main()` that calls a function),
the **same** `main()` runs in three places: Python, C-simulation, and RTL co-simulation.

## The testbench

From [`simp_fun.py`](../../../examples/regmap/simp_fun.py):

```python
class SimpFunTBHls(SeqTB):
    cpp_kernel_name: ClassVar[str | None] = "simp_fun"

    def main(self) -> None:
        dut = SimpFunComponent()
        x = Int32().read_uint32_file(self.data_dir + "/x.bin")
        a = Int32().read_uint32_file(self.data_dir + "/a.bin")
        b = Int32().read_uint32_file(self.data_dir + "/b.bin")
        self._start_timer()
        yield from dut.run_once_sim(x, a, b)
        self._stop_timer_and_log()
        dut.regmap.write_status_json(self.data_dir + "/regmap_status.json",
                                     fields=["ap_done", "y"])
```

That is the whole thing. Note what is *absent*: no `ap_start`, no polling, no register addresses. The
protocol the [system simulation](./pysim.md) spent its whole page exercising is hidden inside one call.
That is the point — a C++ testbench does not bit-bang AXI-Lite either; it calls
`simp_fun(x, a, b, y)` and lets the synthesized interface handle the handshake.

A few pieces are worth pulling apart.

**`dut = SimpFunComponent()`** — no `sim=`. In code generation this is just the DUT declaration. When
the testbench *runs*, the harness makes a `Simulation` **ambient**, and the component binds to it
automatically. One spelling, both modes.

**`Int32().read_uint32_file(path)`** — the standard schema file-IO, the same spelling a build script
uses. It returns a real `Int32`, which is passed straight into the kernel. (Earlier this example loaded
the inputs into the DUT's regmap fields and read them back out to pass them in — a round-trip that was
only ever a Python-side detour; the generated C++ was always this.)

**`cpp_kernel_name = "simp_fun"`** names the generated file, `simp_fun_tb.cpp`.

## Why the invocation is a process

`run_once_sim` is a **generator** — hence `yield from`. That is what makes this page's timing possible:

```python
yield from dut.run_once_sim(x, a, b)
```

It sets the input registers, drives the kernel's `on_start` **through SimPy** — so `on_start`'s modelled
latency actually advances the clock — and reads the outputs back. Its signature is derived from the
register map (inputs are the host-writable fields in declaration order; the return is the readable
one), so it cannot drift from the kernel it invokes.

There is also a synchronous `run_once` (`y = dut.run_once(x, a, b)`), which calls `on_start` directly
and advances no clock. It is the right tool when you only want the value. Here we want the value **and**
the elapsed time, so we use the process form.

Either way, **both lower to the identical C++ call** — `simp_fun(x, a, b, y)`. The `yield from` is a
simulation concern; it leaves no trace in the generated testbench.

## Timing it in-process

The two timer calls bracket the invocation:

```python
self._start_timer()
yield from dut.run_once_sim(x, a, b)
self._stop_timer_and_log()
```

Both are `@sim_only`. That marker tells the code-generation extractor *"never lower this to C++"*, so
the calls simply vanish from `simp_fun_tb.cpp` — C-simulation is untimed, and the cycle numbers there
would be meaningless anyway. In a Python run they read the simulated clock and record the interval,
which `transaction_seconds()` hands back.

This is the same mechanism the kernel and host use for their event logs on the
[previous page](./pysim.md#reading-the-trace): instrumentation lives in the model, and `@sim_only` keeps
it out of the hardware.

## What `py_timing` measures

`py_timing` is the artifact the [co-simulation comparison](./rtlsim.md) checks against, so it matters
what it counts:

> The timer brackets **`run_once_sim`** — so `py_timing` is the **kernel transaction**: ~**4 cycles**,
> the kernel's own `latency_cycles`.

Compare that with the system simulation, which reported the host observing `ap_done` at **6** cycles.
Both numbers are correct; they measure different things:

| | measures | cycles |
|---|---|---|
| [System simulation](./pysim.md#reading-the-trace) | host-observed round trip, **including polling overhead** | 6 |
| **Sequential execution** (this page) | the **kernel transaction** alone | 4 |
| RTL co-simulation | the kernel transaction, in hardware | 5 |

The sequential number is the one worth comparing against co-simulation, because it measures the same
thing: Vitis co-simulation drives the kernel directly — there is no polling driver in the loop. That
like-for-like framing is why `py_timing` comes from this page and not the last one.

## Running it in the build DAG

`PySimStep` is the step that runs this testbench:

```python
# examples/regmap/simp_fun_build.py
@dataclass(kw_only=True)
class PySimStep(BuildStep):
    description = "Run the single-process SeqTB golden and write results/sim/ + py_timing."
    consumes = ["simp_fun_source", "x_in", "a_in", "b_in"]
    produces = {
        "sim_dir":   Path("results/sim"),
        "py_timing": Path("results/py_timing.json"),
    }
    params = {"clk_freq": 100e6}
```

Its `run()` stages the same `x`/`a`/`b` vector into a `data_dir`, drives the testbench, and turns the
timer into cycles:

```python
tb = SimpFunTBHls(name="simp_fun_tb_golden")
tb.run(data_dir=str(sim_dir))          # runs main() as ONE process, to completion
...
transaction_seconds = tb.transaction_seconds()
transaction_cycles  = int(round(transaction_seconds * clk_freq))
```

`SeqTB.run()` is the harness: it creates a fresh `Simulation`, makes it ambient (so the bare
`SimpFunComponent()` inside `main()` binds to it), spawns `main()` as a **single** process, and runs to
completion. Cycles come from seconds via the configured `clk_freq` — the same frequency Vitis uses for
co-simulation, so the two cycle counts are directly comparable.

It produces two things:

- **`results/sim/`** — the functional **golden**: `regmap_status.json` (`{ap_done, y}`) and `y.bin`.
  `validate_csim` on the [C and RTL page](./rtlsim.md) compares the C-simulation's output against this.
- **`results/py_timing.json`** — the cycle measurement, tagged `"source": "seq_tb"`.

Run it:

```bash
cd examples/regmap
python simp_fun_build.py --through py_sim
cat results/py_timing.json
```

## One source, three places

That is the payoff of the sequential form. This single `main()`:

1. **runs in Python** — producing the golden and `py_timing` (this page);
2. **becomes C++** — lowered to `simp_fun_tb.cpp` ([code generation](./codegen.md));
3. **drives the RTL** — the same testbench in C-simulation and co-simulation ([C and RTL simulation](./rtlsim.md)).

There is no second, hand-written C++ testbench to keep in sync, and no chance of the Python golden and
the C-simulation testing different things — they are the same program.

The [system simulation](./pysim.md) cannot make that trip: it is *concurrent*, and concurrency has no
straight-line `int main()` to lower onto. Getting a concurrent system model onto RTL needs the XSI /
SystemC path — future work. Sequential is what reaches Vitis today.

## Next

- [Vitis HLS Code Generation](./codegen.md) — how this `main()` and the kernel become C++.
- [C and RTL Simulation](./rtlsim.md) — running it in C-sim and co-sim, and the cycle comparison.
