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
    QueueOut("q_status", port="m_status", depth=SDEPTH),
)
```

`build_mm_device(kernel)` builds them: a queue in front of `s_in` deep enough for the largest command's
samples, a queue behind each output. The kernel itself stays stream-only. See
[MM queues](../../guide/interface/axi_mm/index.md).

The status is meant for a **status register**, with an interrupt on an error, readable whenever the
host needs it ([On a bus](../../guide/systems/command_response.md#on-a-bus)). The register bank
Waveflow has needs a configuration port and raises no interrupt, so here the status goes to a queue,
one status per command.

## The host

`PolyHost` is a [software host](../../guide/build/sw_threads.md) of two threads:

- a **writer** sends each command's header and samples into `q_in`, each one packet;
- a **reader** takes each command's `Resp` header from `q_out`, then the `nsamp` results it announces,
  then the command's status from `q_status` -- in the order the kernel writes them.

```python
def _reader(self):
    for _ in self.items:
        h = yield from self.q_out.get_schema(Resp)
        y = yield from self.q_out.get(nwords_max=h.nsamp)
        st = yield from self.q_status.get_schema(Status)
        ...
```

The reader sizes the results **from the header**, not from what the writer sent. Queue out drops
TLAST -- the bus cannot see where the kernel's packets end -- so a host behind it can only count, and
the header is what makes the count part of the protocol.

Nothing polls: both threads sleep on the queues' interrupts. The host runs `good_cmds()`: an error is
fatal to the kernel, and recovering from one -- the status register's interrupt, a reset -- is not
built at the bus yet; the kernel's harness is where the error path is tested. The host has a C++ twin
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
| behind the crossbar (`mm_`) | 392 cycles | 385 cycles | +1.8% |
| the device alone, host on a direct link (`dev_`) | 383 cycles | 370 cycles | +3.5% |

Both are within the 5% gate ([`test_stream_kernel_mm.py`](../../../tests/examples/test_stream_kernel_mm.py)).
The second is the **device's own harness**: its views behind one front and the host wired straight to it,
so what is measured is the device's timing alone.

That is the practice this example teaches: a unit verified and calibrated alone, then composed. For it
in general -- several units, hostile stand-ins, incremental rebuilds -- see
[Developing module by module](../../guide/flows/units.md), and [Two kernels on a bus](../markov/index.md)
for a system of several units.
