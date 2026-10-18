---
title: The polynomial's device
parent: The polynomial on a bus
nav_order: 1
---

# The device

A **device** is a kernel with everything that belongs to it on the bus: its memory-mapped views and the
front that decodes their addresses ([What becomes what](../../guide/systems/rtl.md#what-becomes-what)).
The polynomial's device is built from a declaration on the kernel's class; the kernel itself is not
touched.

## The kernel declares its views

[`PolyKernel`](../../../examples/stream_kernel/stream_kernel.py) names, on its class, which of its stream
ports become which views, in address order:

```python
mm_views: ClassVar[tuple] = (
    QueueIn("q_in", port="s_in", depth=QDEPTH),           # QDEPTH = 256
    QueueOut("q_out", port="m_out", depth=QDEPTH),
    QueueOut("q_status", port="m_status", depth=SDEPTH),  # SDEPTH = 16
)
```

- **`q_in`**, a [queue in](../../guide/interface/axi_mm/slave_views.md#queue-in) in front of `s_in`: the
  host writes each command's header and its samples into it, each one packet, and the kernel reads them
  as it reads any stream. 256 words holds a command's samples whole (the largest command the kernel is
  calibrated on has 200).
- **`q_out`**, a [queue out](../../guide/interface/axi_mm/slave_views.md#queue-out) behind `m_out`: the
  `Resp` header and the results, drained by the host.
- **`q_status`**, a queue out behind `m_status`: one `Status` word per command.

The declaration is the **type's** address layout, with no instance and no simulation: view *k* sits at
`k * 4 KB` in declaration order, the lower half of each window for data and the upper half for control
(the interrupt threshold) ([The address map](../../guide/interface/axi_mm/slave.md#the-address-map-a-layout-per-type-a-base-per-instance)).
At `MM_BASE = 0`, `q_in` is at `0x0000`, `q_out` at `0x1000` and `q_status` at `0x2000`.

## `build_mm_device` builds them

[`build_mm_device(kernel, ...)`](../../../waveflow/hw/mm_device.py) turns one kernel instance into its
memory-mapped side, an `MmSlaveDevice`:

```python
kernel = PolyKernel(name=PolyKernel.cpp_kernel_name, sim=sim, clk=clk)
device = build_mm_device(kernel, sim=sim, clk=clk, mem_dwidth=W, one_front=direct)
```

- a view module per declared view -- `MemSlaveWStream` for a queue in, `MemSlaveRStream` for a queue out;
- a stream channel joining each view to the kernel's named port, whose depth **is** the queue's depth;
- and either one `MemSlaveAdaptor` in front of all three views (`one_front=True`), or one bus port per
  view (`one_front=False`) ([One adaptor, or one per view](../../guide/interface/axi_mm/slave.md#one-adaptor-or-one-per-view)).

The crossbar system gives each view its own crossbar slot; the device's own harness puts all three behind
one front ([Composition](composition.md#the-devices-own-harness)).

The device is a composite module: the kernel, its views and its adaptor are its children, and it has
no behavior of its own, so building it changes no cycle. At RTL it is one sub-top of the system's Verilog
([Devices](../../guide/build/xsi_system.md#devices)).

## The kernel is unchanged

`PolyKernel`'s ports are framed -- its results close with TLAST, as its stream harness needs. A queue out
**drops TLAST**: the bus cannot see where the kernel's packets end. `build_mm_device` sees that the
producer is framed and declares the queue's input framed to match; the queue's RTL accepts TLAST and
drops it. So the same class and the same csynth'd RTL serve both the stream harness and the bus, and a
host behind `q_out` must count what it reads -- which is what the `Resp` header is for
([The host](host.md#results-sized-from-their-header)).

The kernel's calibration finds it here too. Its record is filed under its **identity**, a digest of its
structure free of what its ports are bound to, so the kernel in its stream harness, behind the crossbar
and on the direct link has one key (`test_one_key_wherever_the_kernel_sits` in
[`test_phase_store.py`](../../../tests/calib/test_phase_store.py)).

## The status: a queue, for now

The pattern maps `m_status` to a **status register, with an interrupt on an error**, readable whenever
the host needs it ([On a bus](../../guide/systems/command_response.md#on-a-bus)). The register bank
Waveflow has ([Register bank](../../guide/interface/axi_mm/slave_views.md#register-bank)) needs a
configuration port and raises no interrupt, so the status goes to a queue, one status per command. The
host therefore runs only `good_cmds()` -- the stream example's scenario without the command that ends
early ([The golden](../stream_kernel/golden.md)): an error is fatal to the kernel, and the error path at
the bus (the interrupt, a reset) is not built yet. The error path is tested in the kernel's harness
([The system flow](../stream_kernel/system_flow.md)).

Next, [The system](system.md): the device on a crossbar, with its host.
