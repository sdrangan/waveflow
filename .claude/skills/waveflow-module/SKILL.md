---
name: waveflow-module
description: Write a Waveflow hardware module -- an HwModule / FreeRunMod (or, when the spec says the host launches it, a HostActivated kernel) -- from the interfaces a user describes in their own words: map each one to a stream port and the adaptor it needs (a queue, a register bank, an interrupt, a memory reader or writer), choose how commands and data travel, write its pysim behaviour (run_iter, one firing per message) and its harness test that asserts counts. Use whenever you design or review a module's ports, its protocol on them, its Python model, or the user describes what a block receives, sends, is told or tells the host.
---

# Waveflow modules: from the user's interfaces to streams

A user describes a block by what it talks to: "samples from the ADC", "the host sets a threshold",
"results go to memory", "tell the host when it is done". A Waveflow module turns every one of those into
a **stream port**; whatever is not a stream (a bus, registers, memory, an interrupt) is an **adaptor**
outside the module's logic. Your job is that mapping, then the module's behaviour on its streams, then
a test that cannot mistake a deadlock for success.

Building a whole accelerator? Run the process first (`waveflow frames --text`, then `waveflow process
<frame> --text`, or the MCP tools `waveflow_list_frames` / `waveflow_get_process`): it picks the
reference example and the order of work. This skill is the module step inside it.

## The rules (all of them, every time)

1. **Free-running unless the spec says the host launches it.** If the spec names `ap_start`, a launch,
   or status in AXI-Lite registers that the host waits on, it is a `HostActivated` kernel: follow the
   `stream_inband` example and `flows/sequential.md`. Otherwise -- including when a host merely sends
   it data, configures it or reads from it -- it is a `FreeRunMod`. The word "host" alone decides
   nothing.
2. **The module's own logic reads and writes only streams.** No `MMIFMaster`, no register map, no
   address in a leaf's `run_iter`. Memory, bus slaves and interrupts are adaptors (the table below).
3. **Command-response is the default protocol**: a command carrying `tx_id` and `n` (and its
   parameters), the work on `n` elements, a `Resp(tx_id, n)` heading the results on `m_out`, and one
   `Status(tx_id, code)` per command on its own `m_status`. Ports are framed
   (`FramedStreamIFSlave` / `FramedStreamIFMaster`): a header is one burst, its data another.
4. **An error is fatal.** Close the results short (TLAST), write the error status, then stop taking
   input until a reset (in pysim: `yield self.event()`, never triggered).
5. **Every message by its schema** (`get_schema`, `get_array`, `write(msg)`, `write_array`), never
   words by hand. A complex sample is one element, a `ComplexField` of its integer or fixed type
   (`schema/python/complex.md`), not two integers split by hand. (The `waveflow-schema` skill has the
   details.)
6. **Read in the order the writer writes.** A blocking read on the wrong stream deadlocks. Where the
   module must not wait (a setting that may or may not have changed), check with `get_schema_nb` --
   and the order of those checks is the module's priority: state it. A check made once before a
   blocking wait for the next data leaves a window: a setting written during the wait applies one
   block late. If that matters, check both streams in one loop until the data arrives, or state the
   window.
7. **Order between two streams is never free.** Two input streams have no order between them; if a
   command needs a particular configuration from another stream, the configuration carries an id and
   the command names it (`mm_fir`'s `cfg_id`), never a count kept at both ends.
8. **Tests assert counts**: bursts on every output, statuses, `tx_id`s in order, nothing after a fatal
   error. A deadlock in simulation looks like a run that finished.
9. **If Waveflow cannot express an interface, stop and say so** (check `interface/axi_mm/slave.md`,
   "What is not built yet"). Do not work around it with a hand-made substitute.

## Choosing how commands and data travel -- and saying so

The user may put control and data on one stream, on separate streams, or not say. **When they do not
say, choose, do not ask, and tell them**:

- **Default: one input stream**, the command header then its data (`s_in: Cmd | x[0..n-1]`), the
  parameters inside the command; **two outputs**, results and status. One stream has one order; the
  header's `n` says where the data ends.
- **Two input streams instead** when the data and the commands come from **different sources** (one
  stream is then impossible); when a configuration is **too large to repeat** with each command (an id
  pairs them: rule 7); when the data is **in memory** (a command stream; the data from `MemRStream`);
  or when there are **no jobs**, only rarely-changed settings over a continuous stream (the *configured
  stream* shape of `systems/command_response.md`).
- **When the user specifies**, follow them, and add what their choice needs (an id across two streams).

Then write one line in the design notes and in your reply: the choice, why, and the alternative. For
example: *"Control and data share `s_in` (each command carries its parameters and `n`); a separate
configuration stream would be needed only if the configuration were too large to repeat or came from
another source."* Ask only when the choice changes an interface someone else has already built.

## The mapping: what the user says -> what you declare

| the user says | the module declares | outside the module |
|---|---|---|
| data arrives / leaves as a stream (another kernel, an ADC, a DAC) | `FramedStreamIFSlave` / `FramedStreamIFMaster` | nothing, or the RF edge |
| a host (or another master) sends jobs or data over the bus | a stream slave, e.g. `s_in` | a `QueueIn` view in `mm_views` |
| the host takes results over the bus | a stream master, e.g. `m_out`; a queue out drops TLAST, so the host sizes what it reads from a header or a fixed-size message | a `QueueOut` view; the host waits on its `IrqIF` |
| the host sets something, at any time, by register | a stream slave `s_cfg`, one message per commit; read with `get_schema` (must have it) or `get_schema_nb` (may have changed) at a stated point | a `RegBank` view (`cfg_port`) |
| the host reads a status at any time | the `m_status` stream | the same `RegBank` (`status_port`; it keeps the latest) |
| the host is woken when something is ready | nothing in the module | the queue view's level `IrqIF` (threshold set by the host's endpoint). Never polling |
| the module reads or writes a buffer in memory | a composite: the logic issues commands on a stream; `MemRStream` / `MemWStream` children own `m_axi` | the memory (`MemoryMod` in the harness) |
| a table it computes against by address | a `BramIF` port: storage, not an event; what says it is ready is still a stream message | the memory |
| it writes into something shared across a bus | a credit stream (`CreditStreamIF`), writes guarded on credit | a `CreditIn` view |

`mm_views` is a `ClassVar` tuple on the class (`QueueIn`, `QueueOut`, `RegBank`, `CreditIn`,
`BramWindow`, from `waveflow.hw.mm_device`); it declares the views. Building them and the bus around
the module is the system step, not this one.

## Which page to read before which step

Read the page **before** writing the code, with `waveflow_get_doc` (MCP) or the `docs/guide/` checkout.

| step | page |
|---|---|
| the rule and its adaptors; reading order; between kernels | `systems/streams.md` |
| the protocol, the default and the alternatives, errors, the bus mapping | `systems/command_response.md` |
| the class: `cpp_kernel_name`, fields, ports in `__post_init__`, `run_iter`, `kernel_task`, `workload` | `systems/hwmodule.md` |
| the kinds (`FreeRunMod`, `HostActivated`), leaf or composite | `flows/modules.md`, `flows/concurrent_python.md` |
| the host-launched kernel, only when the spec asks for one | `flows/sequential.md` |
| views: queues, register bank, interrupts, what is not built | `interface/axi_mm/slave.md`, `interface/axi_mm/slave_views.md` |
| reading or writing memory | `interface/axi_mm/master.md` |
| non-blocking reads, framed ports | `interface/primitive/stream.md`, `interface/primitive/index.md` |
| credit across a bus | `interface/derived/credit_stream.md` |
| a module alone: its harness, `workload(kind)`, `golden(kind)` | `systems/pysim.md`, `flows/units_harness.md` |
| what each test compares; asserting counts | `systems/verification.md` |

Worked examples: `stream_kernel` (one kernel, the default protocol: copy it), `stream_kernel_mm` (the
same kernel behind queues), `mm_fir` (a register bank and a config id across two streams), `block_eq`
(a composite), `memcpy` (memory adaptors). If a name is not on those pages, `waveflow_search` it --
**do not guess an API name.**

## The shape

```python
@dataclass
class Kern(FreeRunMod):
    cpp_kernel_name: ClassVar[str | None] = "kern"
    mm_views: ClassVar[tuple] = ()                 # only if a bus reaches it (the mapping table)
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=W)
        self.m_out = FramedStreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=W)
        self.m_status = FramedStreamIFMaster(name=f"{self.name}_m_status", sim=self.sim, bitwidth=W)
        for ep in (self.s_in, self.m_out, self.m_status):
            self.add_endpoint(ep)

    def run_iter(self):                            # one firing = one command
        cmd = yield from self.s_in.get_schema(Cmd)
        yield from self.m_out.write(Resp(tx_id=cmd.tx_id, n=cmd.n))
        x = yield from self.s_in.get_array(Elem, cmd.n)          # fewer if the burst ended early
        y = golden_fn(cmd, x)                                    # the golden's pure function
        yield from self.m_out.write_array(Elem, cmd.n, y)        # the results, one burst
        code = Code.OK if len(x.val) == cmd.n else Code.SHORT
        yield from self.m_status.write(Status(tx_id=cmd.tx_id, code=code))
        if code != Code.OK:
            yield self.event()                     # fatal: stop taking input

    def workload(self, kind="test"):               # {input port: bursts} for its harness
        ...
```

Its test: `sysm = harness(Kern, workload="test"); sysm.run()`, then the bursts each output's stand-in
recorded, `sysm.stand_ins["m_out"].words`, compared with the golden **by count and by value**.

## Before you finish, check your own work

- [ ] Every interface the user named is in a mapping: a port, a direction, a schema on it, its
      adaptor or view, or a stated "Waveflow cannot do this yet".
- [ ] `FreeRunMod` unless the spec asks for a launch; no memory, register or address in the logic.
- [ ] How commands and data travel is chosen and **stated** with its reason and the alternative; two
      input streams carry an id where order matters.
- [ ] One `Resp` and one `Status` per command, `tx_id` echoed; an error closes the output short,
      reports, and stops input.
- [ ] Every non-blocking check is at a stated point, in a stated priority.
- [ ] The tests assert the number of bursts on every output and that nothing follows a fatal error.
