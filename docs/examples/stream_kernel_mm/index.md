---
title: The polynomial on a bus
parent: Examples
nav_order: 4
has_children: true
example_dir: examples/stream_kernel
summary: "One kernel put on a bus: the free-running polynomial of the stream example -- the same class, the same csynth'd RTL -- placed behind memory-mapped queues on an AXI-MM crossbar and driven by a software host. The kernel's class declares its views (mm_views: a queue in front of its input, a queue behind each output) and build_mm_device builds them; the system is HwSystem(sim, dut) with the device and the crossbar as the DUT; the host is a two-thread SwHost -- a writer and a reader that sizes each result from its header -- that never polls, waiting on the queues' interrupts, with a C++ twin that runs the same scenario at RTL and must leave identical traces. The device also runs in its own harness, the host on a direct link with no crossbar. The composition check: pysim's cycles, composed from the kernel's calibrated record and the queue and crossbar models with nothing fitted, land within 2% of the RTL (Vitis/Vivado 2025.1)."
---
# The polynomial on a bus

The [free-running polynomial](../stream_kernel/index.md) answers commands on three streams. Here the
same kernel -- the same class, [`PolyKernel`](../../../examples/stream_kernel/stream_kernel.py), and the
same csynth'd RTL -- is put **on a bus**: behind memory-mapped queues on an AXI-MM crossbar, driven by a
software host ([`stream_kernel_mm.py`](../../../examples/stream_kernel/stream_kernel_mm.py)). It is
the smallest bus system in the examples: one kernel, one crossbar, one host.

Nothing in the kernel changes. Its class declares which of its streams become which memory-mapped views,
the framework builds them, and the host reaches them by name. The kernel's timing is the record
calibrated in its stream harness ([Timing](../stream_kernel/timing.md)), loaded here unchanged, and the
point of the example is that the system's timing **composes** from it.

## Learning Objectives

What this adds to the [stream version](../stream_kernel/index.md#learning-objectives), in the order the
pages teach it:

1. Declare a kernel's memory-mapped views in [`mm_views`](../../guide/interface/axi_mm/slave.md#using-it-from-a-kernel)
   and build its **device** with `build_mm_device`, the kernel left stream-only.
2. Put the device on an [**AXI-MM crossbar**](../../guide/interface/axi_mm/crossbar.md): address ranges,
   a bound adaptor that reaches each view by name, an interrupt line per view, and the DUT named with
   [`HwSystem(sim, dut)`](../../guide/systems/rtl.md#the-system).
3. Write a [**two-thread `SwHost`**](../../guide/build/sw_threads.md): a writer that sends each command as
   packets, and a reader that sizes each result from its header and reads in the order the kernel writes.
4. Make the host **wait on interrupts, never poll**, and check that it does not.
5. Write the host's **C++ twin** on its generated endpoints, and run the system flow with prefixed steps
   (`mm_`): the host's traces at RTL equal pysim's.
6. Run the device in **its own harness**: the host on a direct link, no crossbar.
7. Check the **composition**: pysim's cycles, composed from parts calibrated separately with nothing
   fitted, within 5% of the RTL.

## The pages

- [The device](device.md) -- `mm_views`, `build_mm_device`, the address layout, and why the status is a
  queue for now.
- [The system](system.md) -- the crossbar, the address map, the interrupt lines, `HwSystem(sim, dut)`,
  and the `mm_` steps on the build DAG.
- [The host](host.md) -- two threads, results sized from their header, interrupts and no polling, and the
  C++ twin.
- [Composition](composition.md) -- the device's own harness (`mm_system(direct=True)`) and the
  composition check against the RTL.

The protocol, the golden and the kernel are the stream version's:
[The protocol](../stream_kernel/protocol.md), [The golden](../stream_kernel/golden.md),
[The kernel](../stream_kernel/kernel.md).

## Running it

```bash
python -m examples.stream_kernel.stream_kernel_build --through mm_pysim       # pysim only
python -m examples.stream_kernel.stream_kernel_build --through mm_compare     # behind the crossbar (Vitis, Vivado)
python -m examples.stream_kernel.stream_kernel_build --through dev_compare    # the device alone, a direct link
```

The bus systems share the stream example's build DAG
([`stream_kernel_build.py`](../../../examples/stream_kernel/stream_kernel_build.py)), its `codegen` and
the kernel's `csynth`. The gates are
[`tests/examples/test_stream_kernel_mm.py`](../../../tests/examples/test_stream_kernel_mm.py). Cycle
counts on these pages were measured with Vitis HLS and Vivado 2025.1 at 100 MHz.

For a larger bus system -- two kernels, a routed credit link, a shared memory -- see
[Two kernels on a bus](../markov/index.md); for a kernel the host reconfigures through a register bank,
[A memory-mapped FIR](../mm_fir/index.md).
