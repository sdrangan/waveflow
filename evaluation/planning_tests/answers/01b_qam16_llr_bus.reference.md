# Reference plan: 01b the soft demapper behind a bus

What a good plan for [01b_qam16_llr_bus.md](../01b_qam16_llr_bus.md) looks like (tree of 2026-10-17:
`stream_kernel` on D9, the `stream_kernel` and `bus_system` frames, `docs/guide/systems/`).  The answer
key, never shown to an agent.

## Expected

- **Frame / references:** `bus_system`, its one-device reference `stream_kernel`'s memory-mapped system
  (`docs/examples/stream_kernel/memory_mapped.md`, `stream_kernel_mm.py`, `poly_kernel_host.h`); the
  recipe `docs/guide/systems/recipe.md` for the order.
- **The kernel alone first:** rung 1's kernel in its harness (`harness(Qam16Llr, workload=...)`), its
  gates, and its timed phases calibrated there (`add_calib_steps`) -- the record the system's pysim then
  loads.
- **The device:** the kernel declares `mm_views` -- `QueueIn` on `s_in`, `QueueOut` on `m_llr` and on
  `m_status` -- and `build_mm_device(kernel, ...)` builds them; queue depths reasoned (the input queue
  against the largest command, or the writer and reader overlapping for scenario 3).
- **The host:** a `SwHost` with `add_bus_master` and `add_irq` per view; a writer thread and a reader
  thread (`start`); at most two commands in flight (`SwSemaphore`); the reader takes `Resp` with
  `get_schema`, then the announced count, then the `Status` -- the order the kernel writes; no polling;
  a C++ twin (`cpp_model`, `cpp_header`) doing the same reads (typed `get<T, W>()`).
- **The system:** modules on one `Simulation`, `AXIMMCrossBarIF`, `assign_address_ranges`, `IrqIF` lines,
  `BoundMemSlaveAdaptor` endpoints for the host, `DutTop.of(...)`, `HwSystem(sim, dut, run_until=host.done,
  check=...)` -- `mm_system` as the template.
- **The flow:** `add_system_steps` for the system (a prefix), sharing the kernel's `codegen` and
  `csynth`; gates: `mm_compare` traces empty, `report.json` done and zero polls, pysim cycles within
  5% of RTL with nothing fitted.

## Must have

1. The kernel verified in its own harness before the system.
2. `mm_views` and `build_mm_device` -- not a hand-built adaptor.
3. A two-thread `SwHost` on interrupts, sized reads from the header, the status after the results.
4. `HwSystem(sim, dut)` with the device and the crossbar as the DUT.
5. The host's C++ twin, and the trace comparison at RTL.
6. The composition check: pysim from the kernel's calibrated record, nothing fitted, within 5%.

## Counts against a plan

A hand-written AXI testbench; polling; reading the status before the results with a queue smaller than a
command; the results sized from what was sent rather than from the header; an `HwSystem` subclass where
`HwSystem(sim, dut)` does; fitting timing to the system.
