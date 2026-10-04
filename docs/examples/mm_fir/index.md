---
title: A memory-mapped FIR
parent: Examples
nav_order: 9.55
has_children: true
example_dir: examples/mm_fir
summary: "The first kernel that is REACHED over the bus rather than driving it. A free-running FIR whose taps are a register bank and whose samples and results are queues, all behind an AXI slave port that a host program writes and reads through AMD's crossbar. The kernel stays stream-only; a memory-mapped adaptor in the RTL top turns bus transactions into its messages. Because a config and the samples travel on different streams, the order between them is carried in the messages: each packet's in-band header names the config it needs by its id, the kernel waits for that config, and a response FIFO echoes which config each packet was filtered with. Bit-exact against an exact-integer golden in pysim and at RTL, in two adaptor topologies."
---

# A memory-mapped FIR

Every example before this one has a kernel that *drives* the bus: it sends commands to a
`MemRStream` / `MemWStream`, and those turn them into AXI bursts. This example is the first kernel
that is **reached** over the bus. A host writes its taps into registers, pushes samples into a queue,
and reads results out of another queue — all through one AXI slave address range, the way a
processor talks to a peripheral.

Vitis HLS cannot generate that slave side. So the kernel stays exactly what the earlier examples'
kernels are — a stream-only `FreeRunMod` — and a [memory-mapped slave
adaptor](../../guide/interface/axi_mm/slave.md) in the RTL top does the translation: bus
transactions in, stream messages out. This example is the worked design for that adaptor, and for the
[components of the XSI simulation](../../guide/flows/concurrent_layers.md) a design with one has.

## Learning objectives

In going through this example, you will learn how to:

- Give a stream-only kernel **memory-mapped access** with a slave adaptor (`MemSlaveAdaptor`): the
  kernel **declares its views** (`mm_views`) and `build_mm_device` builds the adaptor from them --
  the kernel's own code stays streams only.
- Choose a **view** for each kind of traffic: a **register bank** for configuration (shadow and
  commit) and status (latest value), a **queue in** for a stream of samples, a **queue out** for
  results and for responses.
- Lay out the views' **local address regions** -- a 4 KB window each, in declaration order -- as the
  kernel type's **layout** (`MemSlaveLayout`), with no base.
- Assign the slave a **global base address** on a crossbar (`assign_address_ranges`), so a view's bus
  address is base + offset.
- Give a bus master **transactional endpoints** for the views (`BoundMemSlaveAdaptor`:
  `stream_master`, `stream_slave`, `status`), so the host program holds endpoints and never names an
  address.
- Pass the **global and local addresses** to a C++ master: a per-type layout header and a per-system
  bases header, found by walking the crossbar (`bus_address_headers`) and combined as `at(view, base)`.
- Create **interrupt interfaces** (`IrqIF`) from the queue views to the host, so it waits for room or
  data without polling.
- Carry the **order between two streams** in the messages: a **config id** (`cfg_id`) that the host
  puts in each config and each packet's header names, and a **response FIFO** that echoes it -- the
  [command-response pattern](../../guide/patterns/command_response.md).
- Run the same host program in **pysim** and at **RTL** (XSI, AMD's crossbar), in two adaptor
  topologies, and **calibrate pysim's timing** against the RTL with handshake probes.

## Pages

- [Protocol](protocol.md) — what the host and the kernel say to each other: a config, then per packet a
  header naming its config and its samples, the results and a response; why the config id is needed,
  and what the example demonstrates.
- [Design](design.md) — the slave adaptor that carries it: why it is needed, its views, constructing
  it from the kernel's declaration, the local memory map and the global base, and reaching the views
  from the host.
- [Python model](python.md) — the schemas, the kernel's `run_iter`, the host program, the golden.
- [Python simulation](pysim.md) — the system wired in pysim, the tap switch, the negative control, and how
  close pysim's timing is.
- [Code generation](codegen.md) — the generated top and headers, the hand-written HLS body, and why
  it had to change to pipeline.
- [RTL simulation](rtlsim.md) — the RTL top (crossbar, adaptor, kernel), the C++ host program, the two
  topologies, and the results.

The code is in [`examples/mm_fir`](../../../examples/mm_fir).
