---
title: Testbench
parent: Module Code Generation
nav_order: 6
audience: hls
applies_to: [SeqTB]
api: [SeqTB, HlsCodegenStep, tb_files_to_str]
summary: "The sequential_vitis_tb target: a SeqTB's main() lowers to a C++ int main() emitted as <kernel>_tb.cpp. Same extractor as a kernel, different rule profile — a testbench may build a DUT, read files, push/pop streams and call dut.run(), which a kernel body may not. The body must be straight-line: spawning a SimPy process is rejected, not future work."
---

# Testbench

A [`SeqTB`](../flows/modules.md) is the **source for the `sequential_vitis_tb`** [target](./index.md): its
`main()` lowers to a C++ `int main()`, emitted as a single `<kernel>_tb.cpp`. That program is what
Vitis C-simulation and co-simulation run against the generated kernel — so the same Python that drives
your DUT in the sim also drives it in Vitis.

A `SeqTB` is **not** an `HwModule` and not a `SimObj`: it has no endpoints and no `run_proc`, only
`main()` and a `data_dir`. It is a codegen source that happens to be runnable.

## The same extractor, a different rule profile

`main()` goes through the [extractor](./extractor.md) exactly as a kernel body does — same statement
whitelist, same conditions, same ban on concurrency. What differs is the **vocabulary of operations**
allowed:

| Operation | In a testbench | In a kernel body |
|---|---|---|
| build a DUT — `dut = SimpFun()` | ✅ | ✗ |
| file I/O — `Int32().read_uint32_file(path)` | ✅ | ✗ |
| `ep.push(v)` / `ep.pop(v)` (+ `_array` forms) | ✅ | ✗ |
| invoke — `dut.run()` / `dut.run_once(...)` / `yield from dut.run_once_sim(...)` | ✅ | ✗ |
| `dut.regmap.write_status_json(path, fields=[...])` | ✅ | ✗ |
| `yield from ep.get(...)` / `ep.write(...)` — the process forms | ✗ | ✅ |
| pipelined stream ops | ✗ | hook bodies only |

The split is not arbitrary: a testbench is a *program on a host* — it opens files and calls the kernel.
A kernel is *the hardware* — it moves data over ports.

## Invoking the DUT

Two spellings, and they lower to the **same** kernel call:

```python
dut.run()                                  # no regmap args
dut.run_once(dut.regmap.get("x"), ...)     # args written to the regmap first
yield from dut.run_once_sim(x, a, b)       # the same, driven through SimPy so the clock advances
```

`run_once_sim` is the form that also *runs* in the Python simulation — it drives the kernel's
`on_start` through SimPy so a `SeqTB` can time the transaction in-process. For codegen it is
byte-identical to `run_once`.

From [`examples/regmap/simp_fun.py`](../../../examples/regmap/simp_fun.py), the whole testbench:

```python
class SimpFunTBHls(SeqTB):
    cpp_kernel_name: ClassVar[str | None] = "simp_fun"

    def main(self) -> None:
        dut = SimpFun()
        x = Int32().read_uint32_file(self.data_dir + "/x.bin")
        a = Int32().read_uint32_file(self.data_dir + "/a.bin")
        b = Int32().read_uint32_file(self.data_dir + "/b.bin")
        self._start_timer()
        yield from dut.run_once_sim(x, a, b)
        self._stop_timer_and_log()
        dut.regmap.write_status_json(self.data_dir + "/regmap_status.json",
                                     fields=["ap_done", "y"])
```

The `@sim_only` timer calls are **stripped** by the extractor — they capture latency in the Python run
and emit nothing, so the generated `int main()` is the same with or without them.

A stream DUT looks the same with push/pop instead of a regmap — see `PolyTBHls` in
[`examples/stream_inband/poly.py`](../../../examples/stream_inband/poly.py), which preloads
coefficients, pushes a command header and a sample array, calls `dut.run()`, and pops the response.

## The body must be straight-line

Spawning a SimPy process is **rejected** — not deferred:

> Concurrent process spawn '`self.sim.env.process`(...)'. This body is concurrent: it spawns a
> coroutine that runs alongside the rest of the body, so it has no straight-line lowering … It needs
> the SystemC path (Flow 3), not C-simulation.

A `sequential_vitis_tb` *is* a `int main()`; there is nowhere for a fork to go. A concurrently-driven
testbench is a different [target](./index.md) — `concurrent_systemc_tb`, the
[free-running, concurrently driven](../flows/freerun_conc.md) flow — and is not built yet.
Honest limit: this is a **gate, not a proof** — it rejects the syntax that certainly forks, but cannot
certify that a body is sequential.

## Generating it

`HlsCodegenStep` switches to testbench mode via `is_testbench=True` (or by detecting `_is_testbench`),
and emits one `<kernel>_tb.cpp`. From
[`examples/regmap/simp_fun_build.py`](../../../examples/regmap/simp_fun_build.py):

```python
dag.add(HlsCodegenStep(
    name="gen_tb",
    comp_class=SimpFunTBHls,
    source_artifact="simp_fun_source",
    output_dir="gen",
    is_testbench=True,
))
```

Note `cpp_kernel_name` on the TB names the **DUT**, not the testbench — that is what makes the output
`simp_fun_tb.cpp` and lets it call `simp_fun(...)`.

## API

- [`SeqTB`](../../../waveflow/hw/hw_testbench.py) — the base class; `HwTestbench` is a deprecated alias.
- [`SeqTB.main(self)`](../../../waveflow/hw/hw_testbench.py) — the sequential entry point.
- [`tb_files_to_str(tb_class)`](../../../waveflow/build/hwgen.py) — the generated testbench source.
- [`HlsCodegenStep(is_testbench=True)`](../../../waveflow/build/hwcodegen_steps.py) — emits it in a build DAG.

## Quick reference

- A `SeqTB` is the source for `sequential_vitis_tb`; `main()` → `<kernel>_tb.cpp`.
- Keep `main()` straight-line — `env.process(...)` is rejected, with a message pointing at the
  [concurrently driven](../flows/freerun_conc.md) flow.
- Invoke once with `dut.run()` / `dut.run_once(...)` / `yield from dut.run_once_sim(...)` — all one kernel call.
- Streams use `push`/`pop` (+ `_array`) here, never the `get`/`write` process forms.
- `@sim_only` calls (e.g. the timers) are stripped and emit nothing.
- `cpp_kernel_name` on a TB names the DUT it drives.
