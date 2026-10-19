---
title: A memory-mapped FIR
parent: Examples
nav_order: 9
has_children: true
example_dir: examples/mm_fir
summary: "A kernel the host configures over the bus. A free-running FIR whose taps are a register bank and whose samples and results are queues, all behind an AXI slave port that a host program writes and reads through AMD's crossbar. The kernel stays stream-only; a memory-mapped adaptor in the RTL top turns bus transactions into its messages. Because a config and the samples travel on different streams, the order between them is carried in the messages: each packet's in-band header names the config it needs by its id, the kernel waits for that config, and a response FIFO echoes which config each packet was filtered with. Bit-exact against an exact-integer golden in pysim and at RTL, in two adaptor topologies."
---

# A memory-mapped FIR

[The polynomial on a bus](../stream_kernel_mm/index.md) put a stream-only kernel behind memory-mapped
queues, reached by a host over a crossbar. This example keeps that shape and adds what a kernel the
host **configures** needs: a **register bank** beside the queues, an **order** between a config and the
samples it applies to, and a choice of how the views sit on the bus. A host writes the FIR's taps into
registers, pushes samples into a queue, and reads results out of another queue — all through one AXI
slave address range, the way a processor talks to a peripheral.

Vitis HLS cannot generate that slave side. So the kernel stays a stream-only `FreeRunMod` — and a [memory-mapped slave
adaptor](../../guide/interface/axi_mm/slave.md) in the RTL top does the translation: bus
transactions in, stream messages out. This example is the worked design for that adaptor, and for the
[components of the XSI simulation](../../guide/flows/concurrent_layers.md) a design with one has.

## Learning objectives

What this adds to [The polynomial on a bus](../stream_kernel_mm/index.md#learning-objectives) (views
declared in `mm_views`, `build_mm_device`, a crossbar, a host on interrupts and its C++ twin), in the
order the pages teach it:

1. Choose a **view** for each kind of traffic: a **register bank** for configuration (shadow and
   commit) and for status (the latest value), queues for the samples, the results and the responses.
2. Lay out the views' **local address regions** as the kernel type's layout (`MemSlaveLayout`, a 4 KB
   window each in declaration order), give the slave a global base with `assign_address_ranges`, and
   hand both to the C++ host as a per-type layout header and a per-system bases header, combined as
   `at(view, base)`.
3. Carry the **order between two streams** in the messages: a **config id** (`cfg_id`) the host puts in
   each config and names in each packet's header, a kernel that waits for that config, and a
   **response FIFO** that echoes which config each packet was filtered with -- the
   [command-response pattern](../../guide/systems/command_response.md) for a config.
4. Build the device in **two adaptor topologies** -- a front per view (`per_view`) or every view behind
   one front and a generated decoder (`one_front`) -- with one address map and the same host program.
5. Find where pysim's cycles and the RTL's differ with **handshake probes**, and calibrate pysim's
   timing against the RTL.

## Pages

- [Protocol](protocol.md) — what the host and the kernel say to each other: a config, then per packet a
  header naming its config and its samples, the results and a response; why the config id is needed,
  and what the example demonstrates.
- [Slave adaptor](slave_adaptor.md) — the adaptor that carries it: why it is needed, its views, constructing
  it from the kernel's declaration, the local memory map and the global base, and reaching the views
  from the host.
- [Python model](python.md) — the schemas, the kernel's `run_iter`, the host program, the golden.
- [Python simulation](pysim.md) — the system wired in pysim, the tap switch, the negative control, and how
  close pysim's timing is.
- [Build flow](build.md) — the build DAG (`mm_fir_build.py`) for both topologies: which steps are the
  example's (codegen and the scenario), which are the framework's, the CLI, and what a second run skips.
- [Code generation](codegen.md) — what each pysim object generates (Fig. 1), the generated top and
  headers, the hand-written HLS body, and why it had to change to pipeline.
- [Synthesis](synth.md) — csynth of the kernel, the crossbar IP, and the Verilog top walked from the
  graph in both topologies, with the generated decoder (Fig. 2).
- [XSI testbench](xsi.md) — the RTL under a testbench (Fig. 3): the C++ host program, the conformance
  gate, and the bus master that overlaps a read and a write.
- [RTL simulation](rtlsim.md) — the results: both topologies bit-exact, 618 and 611 cycles, and how the
  numbers got there.

The code is in [`examples/mm_fir`](../../../examples/mm_fir).
