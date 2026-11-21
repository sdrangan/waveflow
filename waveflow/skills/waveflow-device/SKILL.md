---
name: waveflow-device
description: Build a Waveflow device -- kernels joined to what is not a stream through adaptors -- a kernel that reads or writes buffers in memory (MemRStream / MemWStream, MemRCmd / MemWCmd, the writer's echo as "done"), a kernel a host reaches over the AXI bus (mm_views: QueueIn, QueueOut, RegBank, CreditIn, BramWindow; build_mm_device; interrupts), a credit stream across a bus, BRAM shared by address, a MemoryMod; and what becomes the device's HLS top and sub-top. Use whenever a Waveflow design touches DDR or on-chip memory, a host's registers, queues or interrupts, or a bus between kernels.
---

# Waveflow devices: kernels, adaptors, and the world that is not streams

A **kernel** reads and writes streams only. Whatever is not a stream -- memory, a bus slave, an
interrupt -- is reached through an **adaptor**, and kernels with their adaptors are a **device**
(`systems/devices.md`; the words as `systems/glossary.md` defines them). This skill is how to choose and
wire the adaptors, and what each one guarantees. The kernel's own logic is the `waveflow-kernel` skill;
its HLS body is `waveflow-kernel-hls`; the system around the devices (`HwSystem`, the build) is
`waveflow-system`.

## The rules (all of them, every time)

1. **The kernel stays streams-only; the device holds the adaptors.** Memory: the framework's
   `MemRStream` / `MemWStream` as children of a composite beside the kernel (`add_comp`, framed `add_if`
   streams between them). That composite is a **device**, not a kernel: call it one. Never give a kernel
   an `MMIFMaster`, and never write a memory model or an AXI slave of your own.
2. **Memory, in-band.** Build the reader and writer with `inband=True`. A read is a `MemRCmd(addr, len,
   fwd_bursts)` on the reader's `s_cmd`; the data comes back as one burst after any relayed bursts. A
   write is one stream, `[MemWCmd | relayed bursts | data]`, on the writer's `s_in`. `addr` and `len` are
   **words** (bus words, not bytes, not elements); count a buffer's words with the schema
   (`get_nwords`), and pack elements with the array utils.
3. **"Done" means stored: answer through the writer.** Put the job's response or status behind the
   write (`MemWCmd(..., fwd_bursts=1)`, then the status, then the data) on a writer built with
   `emit_done=True` and `max_fwd_words` large enough for it; the writer echoes it on `m_done` only after
   the memory acknowledged the write. A status the kernel sends on its own stream can reach the host
   before the data lands. With no data to write (an error), send `MemWCmd(len=0, fwd_bursts=1)` and the
   status, then stop (`yield self.event()`).
4. **The writer writes whole words.** A buffer whose length is not a multiple of the elements per word
   gets its last word written whole: the spare lanes are overwritten. State it, and allocate whole words.
5. **The host reaches a device through views, declared on the class**: `mm_views: ClassVar[tuple]`
   (`QueueIn`, `QueueOut`, `RegBank`, `CreditIn`, `BramWindow` from `waveflow.hw.mm_device`), each naming a
   stream port of the class (a composite's ports may be its children's). `build_mm_device(dev, ...)`
   builds them; `BoundMemSlaveAdaptor(device.layout.at(base), host.m)` gives the host its endpoints by
   view name.
6. **Pick the view by what the host must do**:
   - sends jobs or data: a **queue in**; takes results: a **queue out**, which drops TLAST -- the host
     sizes each read from a header or a fixed-size message;
   - is **woken**: only a queue's level **interrupt** (`IrqIF`, source the view's `m_irq`, the host's
     endpoint given `irq=`). The host never polls;
   - sets settings at any time: a **register bank**, one config message per commit, the kernel taking it
     at a stated point (`get_schema_nb`); reads the latest status at any time: the bank's status side.
   - **The register bank raises no interrupt.** "A status register with an interrupt" cannot be built
     today: say so, and send what must wake the host to a queue out (a bank beside it is fine).
7. **Order holds only where the guide says it does.** Behind **one front** (`one_front=True`), bus
   transactions are served in order, so "write the data, then ring the doorbell" works there and not
   across fronts. Between two kernel-side streams there is **no** order: a configuration the data
   depends on carries an id that the data names.
8. **Bound what is outstanding.** A queue out that fills stalls whatever writes it (the writer, then the
   kernel). The host keeps at most as many jobs in flight as the response queue holds -- state the
   number.
9. **Across a shared bus, credit.** A kernel writing another device's queue over the bus uses a credit
   stream (`MmCreditStreamIF`: `CreditIn` on the producer, `QueueIn` on the consumer), never a plain
   stream; size the forward FIFO and the queue (the credit window) as `credit_streams.md` says.
10. **Storage is not an event.** A `BramIF` (or a `BramWindow` on the bus) is read by address; what says
    it is ready is a stream message (a doorbell). A memory shared by two tasks cannot live inside the HLS
    top: it is RTL beside it (`rtl_module()`).
11. **At RTL, the generator owns the `m_axi`.** The device's HLS top (its kernels, readers, writers, bus
    writers) is generated with `offset=off` + `stable` pointers; never hand-write those pragmas (an
    `offset=slave` pointer to an `hls::task` deadlocks when tasks fire unequally). In a kernel body, a
    request to the reader and the wait for its reply are two streams: never a write then a blocking read
    that can land in one state. The slave adaptor's front and views are framework Verilog, joined to the
    HLS top in the device's sub-top. Read *What is not built yet* in `slave.md` before promising a bus
    feature, and say what is missing.

## Which page to read before which step

Read the page **before** writing the code, with `waveflow_get_doc` (MCP) or the `docs/guide/` checkout.

| step | page |
|---|---|
| what a device, an adaptor, the HLS top and sub-top are; what you write and what Waveflow provides | `systems/devices.md` |
| memory as command-response; done means stored | `systems/command_response.md` |
| the memory reader and writer, the in-band protocol, splitting the endpoints | `interface/axi_mm/master.md` |
| the slave adaptor, `build_mm_device`, the address map, interrupts, ordering, what is not built | `interface/axi_mm/slave.md` |
| each view: queue in / out, register bank, BRAM window, credit in | `interface/axi_mm/slave_views.md` |
| credit across a bus; in an HLS body | `interface/axi_mm/credit_streams.md`, `interface/axi_mm/credit_streams_hls.md` |
| a memory in the system; the harness's memory stand-in | `memory/memorymod.md`, `systems/pysim.md` |
| BRAM shared by address; pre-written Verilog | `interface/primitive/bram.md`, `comp_codegen/rtl_module.md` |
| how a pointer reaches a task (`offset=off` + `stable`) | `comp_codegen/freerunning_composite.md` |
| the device's sub-top in the system's Verilog | `build/xsi_system.md` |

Worked examples: `memcpy` (a device: a sequencer, a reader, a writer; done = the writer's echo),
`markov` (two devices on a bus, a credit stream across it, results in shared memory, a two-thread host),
`stream_kernel_mm` (a kernel behind queue views and their interrupts), `mm_fir` (a register bank and a
config id), `interleaver` (reads issued by one stage, data taken by the next). If a name is not on those
pages, `waveflow_search` it -- **do not guess an API name.**

## The shape

```python
@dataclass
class Dev(FreeRunMod):                       # a device: a kernel and its memory adaptors
    cpp_kernel_name: ClassVar[str | None] = "dev"
    mm_views: ClassVar[tuple] = (            # how a host reaches it (only if one does)
        QueueIn("q_cmd", port="s_cmd", depth=...),
        QueueOut("q_done", port="m_done", depth=...),   # its IrqIF wakes the host
    )
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.core = Core(name=f"{self.name}_core", sim=self.sim, clk=self.clk)   # streams only
        self.rd = MemRStream(name=f"{self.name}_r", sim=self.sim, mem_dwidth=W, inband=True,
                             clk=self.clk)
        self.wr = MemWStream(name=f"{self.name}_w", sim=self.sim, mem_dwidth=W, inband=True,
                             emit_done=True, done_framed=False,          # to a queue out
                             max_fwd_words=Done.nwords_per_inst(W), clk=self.clk)
        for c in (self.core, self.rd, self.wr):
            self.add_comp(c)
        # framed StreamIFs: core.m_rd -> rd.s_cmd, rd.m_out -> core.s_rd, core.m_wr -> wr.s_in (add_if)
        ...
        self.boundary = ["s_cmd", "m_rd_mem", "m_wr_mem", "m_done"]   # add_comp x add_endpoint order
        self.s_cmd, self.m_done = self.core.s_cmd, self.wr.m_done
        self.m_rd_mem, self.m_wr_mem = self.rd.m_mem, self.wr.m_mem

# the core's firing: ask, compute, then [MemWCmd(fwd_bursts=1) | Done | data] to the writer
#     yield from self.m_rd.write(MemRCmd(addr=cmd.src, len=nw, fwd_bursts=0))
#     x = yield from self.s_rd.get_array(Elem, cmd.n)
#     yield from self.m_wr.write(MemWCmd(addr=cmd.dst, len=nw, fwd_bursts=1))
#     yield from self.m_wr.write(Done(tx_id=cmd.tx_id, ...))
#     yield from self.m_wr.write_array(Elem, cmd.n, array(Elem, y))
```

Its tests: the device alone, `harness(Dev, workload=kind)` -- the memory's image under one memory port
of the workload, the final arena in `vectors/mem` after `write_scenario` -- then on a bus with its host
(`waveflow-system`). Count the statuses; read every output back.

## Before you finish, check your own work

- [ ] Every kernel is streams-only; memory through `MemRStream` / `MemWStream` children, in-band;
      the composite holding them is called a device.
- [ ] Addresses and lengths in words; buffers counted and packed by the schema.
- [ ] The answer to each job comes through the writer's echo, so it means "stored"; the error path too.
- [ ] Every way the host reaches the device is a view in `mm_views`; every wait is on an interrupt.
- [ ] Anything the user asked for that Waveflow cannot build (an interrupt from a register bank,
      interrupt-enable registers, ...) is **stated**, with what you used instead.
- [ ] The host's jobs in flight are bounded by what the response queue holds.
- [ ] Choices the user left open are written down: the status path, whole words, the protocol.
