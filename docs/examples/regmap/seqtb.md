---
title: Sequential execution
parent: Register Map (simple function)
nav_order: 4
has_children: false
---
# Sequential execution

The second of the example's [two ways to simulate](./index.md#two-ways-to-simulate-it). Where the
[system simulation](./pysim.md) runs a host *concurrently* with the kernel, this path is **one
sequential program** — a `SeqTB` — that reads the inputs, invokes the kernel, and writes the result:

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

Because it is one program calling a function — which is exactly what a Vitis C++ testbench is — **this
same `main()` runs in all three places**: in Python (the `py_sim` golden and the `py_timing`
measurement), in C-simulation, and in RTL co-simulation. That is the payoff of the sequential form, and
the reason the protocol is hidden behind `run_once_sim` rather than driven register-by-register.

> **Status: this page is being written.** It will cover: `run_once_sim` and why the invocation is a
> process; the `@sim_only` timers and what `py_timing` measures (the kernel transaction, ~4 cycles —
> the like-for-like counterpart of the co-simulation measurement, and why it differs from the
> host-observed 6 cycles in the [system simulation trace](./pysim.md#reading-the-trace)); and how the
> `py_sim` step produces the golden the C-simulation is checked against.

## Next

- [Vitis HLS Code Generation](./codegen.md) — how this same `main()` lowers to the C++ testbench.
- [C and RTL Simulation](./rtlsim.md) — running it in C-sim and co-sim, and the cycle comparison.
