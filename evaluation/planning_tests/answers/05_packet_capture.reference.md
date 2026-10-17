# Reference plan: 05 packet capture

What a good plan for [05_packet_capture.md](../05_packet_capture.md) looks like (tree of 2026-10-17).  The
answer key, never shown to an agent.  The rung tests the **system** pattern: two devices, a routed link
with credit, a shared memory, a host on interrupts -- markov's shape in another domain.

## Expected

- **Frame / references:** `bus_system`; reference `markov` (a host into kernel A, A -> B routed over the bus
  with credit, B into memory through the in-band memory writer, a response once the data is stored, a
  two-thread host with a semaphore); `stream_kernel`'s memory-mapped system for the device and host basics;
  the recipe (each kernel alone first).
- **detect:** a `FreeRunMod`, `mm_views` with a `QueueIn` for the jobs and a `CreditIn` for the credit back
  from capture; its output a `FramedCreditStreamMasterIF` (or the credit master) -- the routed link
  (`MmCreditStreamIF`, placed with `.place(...)` on capture's `QueueIn` and detect's `CreditIn`).  Its
  messages to capture: per detection a header (`tx_id`, `index`, `count`, `slot`) and the window; an end
  (`tx_id`, `ndet`) -- tag-first `DataList`s (or a fixed `DataUnion`), stated layouts.
- **capture:** a composite like `MarkovChain`: a core that turns each detection into a memory write command
  (`MemWCmd`, the slot's address) followed by the window, and the framework's in-band memory writer
  (`MemWStream`, `inband=True`, `emit_done=True`) that stores it and forwards the report once stored; its
  reports through a `QueueOut` the host reads.  The response is sent only after the data is in memory --
  markov's "the answer means done".
- **memory:** a `MemoryMod` on a crossbar slave, inside the cut; slot addresses computed from the
  layout, never typed by hand.
- **host:** a `SwHost` with `add_bus_master`, `add_irq` per view it waits on, two threads (`start`), a
  `SwSemaphore` of two, reads the reports by the tag, then each window from the memory; a C++ twin.
- **system:** `HwSystem(sim, dut)` with a `DutTop.of(...)` of the two devices, the memory and the crossbar
  (or markov's subclass form -- accept, note); `assign_address_ranges`.
- **verification:** each kernel's harness alone (credit stand-ins for detect's output: `CreditSink`;
  for capture's input: `CreditSource`), then the system; `add_system_steps`; the trace comparison, zero
  polls; calibration of each kernel's phases in its harness, then the composition check (markov: -1.6%).

## Must have

1. Two devices built with `build_mm_device` from each kernel's `mm_views`; the crossbar; the memory.
2. The detect -> capture link routed over the bus with credit (`MmCreditStreamIF`), its window sized.
3. capture writes memory through the in-band memory writer and reports only after the data is stored.
4. A two-thread `SwHost` on interrupts, two in flight, the reports read by their tag, windows read from
   the memory; no polling.
5. Each kernel verified alone in its harness before the system.
6. The system through `add_system_steps` to the trace comparison; the composition check.

## Counts against a plan

A host that polls; detect writing capture's queue without credit; a report sent before the window is
stored; hand-typed addresses; one kernel doing both jobs; hand packing.

## Recorded, not scored

Frame chosen; whether `markov` was found and followed; the system form (`HwSystem(sim, dut)` vs subclass);
the credit window and its reasoning; whether the per-kernel credit stand-ins were found; pages read.
