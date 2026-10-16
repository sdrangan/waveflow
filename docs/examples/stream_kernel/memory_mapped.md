---
title: The polynomial behind AXI-MM
parent: Free-running polynomial
nav_order: 6
---

# Behind AXI-MM

The same kernel -- the same class, the same csynth'd RTL -- placed behind memory-mapped queues on a
crossbar and driven by a software host
([`stream_kernel_mm.py`](../../../examples/stream_kernel/stream_kernel_mm.py)).

## The device

The kernel declares its memory-mapped side as views of its ports:

```python
mm_views: ClassVar[tuple] = (
    QueueIn("q_in", port="s_in", depth=QDEPTH),
    QueueOut("q_out", port="m_out", depth=QDEPTH),
    QueueOut("q_resp", port="m_resp", depth=RDEPTH),
)
```

`build_mm_device(kernel)` builds them: a queue in front of `s_in` deep enough for the largest command's
samples, a queue behind each output. The kernel itself stays stream-only. See
[MM queues](../../guide/interface/axi_mm/index.md).

## The host

`PolyHost` is a [software host](../../guide/build/sw_threads.md) of two threads:

- a **writer** sends each command's header and samples into `q_in`, each one packet;
- a **reader** takes each command's results from `q_out` and its response from `q_resp`.

Nothing polls: both sleep on the queues' interrupts. The host has a C++ twin
([`poly_kernel_host.h`](../../../examples/stream_kernel/poly_kernel_host.h)) that runs the same scenario
file at RTL, and the two hosts' traces must be identical.

## The system

```python
sysm = HwSystem(sim, dut)        # dut: the device and the crossbar; the host is the testbench
```

The DUT is named; the testbench is everything else, derived. Its steps are the same framework steps,
with prefix `mm_`.

## The composition check

The point of calibrating the kernel alone was to use it here. pysim's estimate of this system is
**composed** from parts calibrated separately -- the kernel's record from its harness, the queue views'
and the crossbar's models -- with nothing fitted to this system:

| system | pysim | RTL (Vitis 2025.1) | |
|---|---|---|---|
| behind the crossbar (`mm_`) | 396 cycles | 392 cycles | +1.0% |
| the device alone, host on a direct link (`dev_`) | 421 cycles | 408 cycles | +3.2% |

Both are within the 5% gate ([`test_stream_kernel_mm.py`](../../../tests/examples/test_stream_kernel_mm.py)).
The second is the **device's own harness**: its views behind one front and the host wired straight to it,
so what is measured is the device's timing alone.

That is the practice this example teaches: a unit verified and calibrated alone, then composed. For it
in general -- several units, hostile stand-ins, incremental rebuilds -- see
[Developing module by module](../../guide/flows/units.md), and [Two kernels on a bus](../markov/index.md)
for a system of several units.
