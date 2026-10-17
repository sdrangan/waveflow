---
title: Building a system
parent: Systems
nav_order: 1
audience: python
summary: "The recipe for building a Waveflow system from a spec, in the order you take the steps: decompose it top-down (the testbench and the DUT, the DUT's modules, the interfaces, the messages), then write it bottom-up -- the schemas and a golden first, then each module with its own harness, verified alone in pysim and at RTL, then the system itself, its testbench and its run through the system flow to the RTL comparison, and finally timing when the spec asks for numbers. Each step names the reference example to start from and the guide page with the details."
---

# Building a system

Decompose a system **top-down**; write it **bottom-up**. The first tells you what the parts are; the
second builds each part on ones already verified.

## Decompose: from the outside in

Answer these before writing code -- they are the system's design, and every later step follows from
them ([Systems](index.md)):

1. **The testbench and the DUT.** What is synthesized, and what drives it: stream drivers and sinks, a
   host program on a bus, a memory model.
2. **The DUT's modules.** Each kernel and what it does; which are composites of stages; which sit
   behind a bus (devices), and which memory they share.
3. **The interfaces.** Each link between modules, or to the testbench: a stream, a credit stream, a
   queue reached over AXI-MM, a memory window, an interrupt.
4. **The messages.** On each link, the messages and their fields -- usually the
   [command-response](../patterns/command_response.md) protocol: a command and its data, the results
   headed by `Resp`, a `Status` per command.

## Write: from the inside out

Each step names where to start. Do not move on until a step's gate passes.

1. **The schemas** of every message ([Data schemas](../schema/index.md)): `DataList`, `EnumField`,
   `FixedField`, `ComplexField`. Every word is packed by generated code -- never by hand.
2. **A golden model**: the operation as pure functions, independent of every module, bit-exact. Pin it
   with worked examples; compute the expected outputs from the scenarios' **intents**, not from their
   stimulus. *Start from:* [the polynomial's golden](../../examples/stream_kernel/golden.md).
3. **Each module, with its own harness.** Write the module -- a
   [`FreeRunMod`](../flows/concurrent_python.md) with framed stream ports and a `run_iter` per command,
   its scenarios in `workload(kind)`; for a composite, a child per stage, and between stages the
   framework's [stream blocks](../streams/index.md) (a `StreamBroadcast` to give two stages one input) --
   and verify it **alone**:
   `harness(Module, workload=...)` puts a stand-in at each port. *Gate:* pysim bit-exact against the
   golden. Then its HLS body behind `kernel_task()` and the build steps, and *gate:* the harness through
   `compare`, RTL against pysim. *Start from:* [the polynomial's kernel](../../examples/stream_kernel/kernel.md)
   and [its system flow](../../examples/stream_kernel/system_flow.md);
   [Developing module by module](../flows/units.md).
4. **The system.** The modules on one simulation, the devices built from the kernels' views, the
   interfaces bound, the host if there is one, then `HwSystem(sim, dut)`. *Gate:* pysim bit-exact.
   *Start from:* [the polynomial behind AXI-MM](../../examples/stream_kernel/memory_mapped.md);
   [Two kernels on a bus](../../examples/markov/index.md) for several devices and a memory.
5. **The system flow.** The build DAG -- the example's `codegen`, then `add_system_steps` -- on
   `run_dag_cli`. *Gate:* `compare`, the system's RTL against its pysim; with a host, its traces at RTL
   identical to pysim's and no polls ([The system flow](../build/xsi_system.md#running-it)).
6. **Timing, when the spec asks for numbers.** Declare each kernel's
   [timed phases](../timing_model/timed_phases.md) and calibrate them in its harness
   (`add_calib_steps`); the system's pysim then composes them, and its cycle count is checked against
   the RTL's with nothing fitted.

## The rules that cost the most when broken

- **Read in the order a kernel writes**, or buffer a whole job: a blocking read on the wrong stream
  deadlocks ([Command-response](../patterns/command_response.md#the-rules-that-make-it-robust)).
- **An un-paced free-running pipeline deadlocks** at RTL: every stage waits for a command or a credit.
- **A deadlock looks like success**: every gate asserts counts -- bursts recorded, statuses written,
  jobs answered -- not just "it finished".
- **Never edit a generated file** (`gen/`, `include/`); if the machinery cannot express something,
  stop and report it.
