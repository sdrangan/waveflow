---
title: Slave adaptor views
parent: AXI-MM
grand_parent: Interfaces
nav_order: 4
audience: python
api: [MemSlaveWStream, MemSlaveRStream, MemSlaveRegBank, MemSlaveBramWindow, QueueView, RegBankView, BramView]
summary: "The four views of the memory-mapped slave adaptor — queue in, queue out, register bank, BRAM window. For each: what it is for, its constructor, what the kernel does with it, and what a bus master does with it. Also: register bank versus register map."
---
# Slave adaptor views

A [slave adaptor](./slave.md) is made of views, each a way for a bus master to reach a free-running
kernel. This page takes them one at a time. Each section says what the view is for, gives its
constructor, then describes its two sides:

- **The kernel side** is what the kernel's code does: read or write a stream (or, for the BRAM window,
  a memory port). The kernel never sees an address.
- **The bus side** is what a bus master — a host program, or another kernel through its `m_axi` port —
  does: read and write addresses in the view's window. Those addresses are the interface of whoever
  writes the host software, so each view lists them. An offset is relative to the start of the view's
  window; [Building an adaptor](./slave.md#building-an-adaptor) says where the window starts.

All four constructors take `name`, `sim` and `clk` like any module, and these two:

| parameter | default | meaning |
|---|---|---|
| `mem_dwidth` | `64` | the bus width in bits; one bus word |
| `window` | `4096` | the window size in bytes: a power of two, at least 4 KB. Inside a `MemSlaveAdaptor` it must be 4096 |

How the views are built in RTL, and why they behave as they do, is in
[Slave adaptor — how it works](./slave_howitworks.md).

## Queue in

A **queue in** is an input FIFO for the kernel that a bus master fills. The bus master writes packets
of words; the kernel reads them as an ordinary stream, one packet at a time.

```python
qin = MemSlaveWStream(name="qin", sim=sim, depth=16, clk=clk)
```

| parameter | default | meaning |
|---|---|---|
| `depth` | `512` | FIFO depth in words; a power of two, at least 2 |

**The kernel side** is the stream endpoint `qin.m_out`. Bind it to a stream whose `depth` equals the
queue's `depth` (that stream *is* the FIFO, and the simulation checks it at start), and give the
kernel a `StreamIFSlave` on the other end. The kernel reads exactly as it would read any stream:

```python
pkt = yield from self.s_in.get()        # one packet, as the bus master wrote it
```

Each packet ends with TLAST, so the kernel sees where the bus master's packets begin and end.

**The bus side.** A bus master pushes a packet by writing its length, then its words:

| to | do | at offset |
|---|---|---|
| push a packet of *n* words | write `[n, d0, d1, ..., d(n-1)]` | anywhere in the window, typically `0` |
| see how much room is left | read one word: the number of free slots, `0..depth` | anywhere in the window |

- **The length goes first.** The first word of each packet is its length (low 32 bits) and is not
  passed to the kernel. A packet may be split across any number of bursts, and a length of 0 is an
  empty packet.
- **A full queue stalls the writer.** When the FIFO is full the write waits until the kernel drains
  it — the bus master's whole port waits with it. A bus master whose port carries other traffic reads
  the free slots first, or uses credits ([Credit Stream](../derived/credit_stream.md)).
- **A completed write means the queue took the words, not that the kernel has read them.**
- **Kernel to kernel.** A kernel that writes memory through `MemWStream` can feed another kernel's
  queue by pointing its base address at the window and putting the length first.

**RTL:** `QueueView(name, kind="in", axis, depth=512, law=12)`, where `axis` names the kernel's AXIS
port and `law` is log2 of the window size; module `mm_queue_in.v`.

## Queue out

A **queue out** is an output FIFO from the kernel that a bus master drains. The kernel writes words to
a stream; the bus master reads them out.

```python
qout = MemSlaveRStream(name="qout", sim=sim, depth=16, clk=clk)
```

| parameter | default | meaning |
|---|---|---|
| `depth` | `512` | FIFO depth in words; a power of two, at least 2 |

**The kernel side** is the stream endpoint `qout.s_in`. Bind it to a stream whose `depth` equals the
queue's `depth`, and give the kernel a `StreamIFMaster` on the other end. The kernel writes as it would
to any stream, and blocks when the queue is full:

```python
yield from self.m_out.write(words)
```

**The bus side.** The window is split in two: the lower half pops data, the upper half reports how
much there is. `qout.status_addr()` returns the start of the upper half (`window / 2`).

| to | do | at offset |
|---|---|---|
| see how many words are ready | read one word: `0..depth` | `window / 2` (`0x800`) |
| take *n* words | read *n* words; each word read is removed from the queue | `0` |

- **Check before popping.** A read never waits for data: a pop from an empty queue returns 0 and an
  error response (SLVERR). Read the count, then pop at most that many. (A slave that waited would hold
  the whole bus.)
- At 64 bits, the lower half of a 4 KB window is 256 words, one maximal AXI burst.
- Packet boundaries (TLAST) from the kernel are not visible to the bus master.
- Writes to the window are ignored.

**RTL:** `QueueView(name, kind="out", axis, depth=512, law=12)`; module `mm_queue_out.v`.

## Register bank

A **register bank** holds a kernel's configuration and status. A bus master writes configuration
fields, then **commits** them; each commit reaches the kernel as one complete configuration message.
The kernel, in turn, sends status messages, and the bus master reads the latest one. It is the
free-running kernel's counterpart of a [register map](./regmap.md); the two are compared
[below](#register-bank-or-register-map).

```python
regs = MemSlaveRegBank(name="regs", sim=sim, cfg_type=Cfg, status_type=Status, clk=clk)
```

| parameter | default | meaning |
|---|---|---|
| `cfg_type` | required | the configuration, a `DataSchema`. Its size in bus words is `regs.ncfg`; it must fit in half the window |
| `status_type` | required | the status, a `DataSchema`. Its size in bus words is `regs.nstat`; it must fit in a quarter of the window |

**The kernel side** is two stream endpoints:

- `regs.m_cfg` sends one config message per commit. Bind it to a stream of depth exactly `regs.ncfg`
  (one message; the simulation checks it), and give the kernel a `StreamIFSlave`. The kernel receives
  a whole configuration at once:

  ```python
  cfg = yield from self.s_cfg.get_schema(Cfg)       # waits for the next commit
  cfg = yield from self.s_cfg.get_schema_nb(Cfg)    # or: check, and carry on if there is none
  ```

- `regs.s_status` receives status messages. Give the kernel a `StreamIFMaster` and write whole status
  messages, as often as it likes:

  ```python
  yield from self.m_status.write(Status(nsamp=self.nsamp))
  ```

**The bus side:**

| to | do | at offset |
|---|---|---|
| stage a configuration | write `regs.cfg_words(cfg)` (word *i* of the config at `i × bytes-per-word`) | `0` |
| send it to the kernel | write any value | `regs.commit_offset()` = `window / 2` (`0x800`) |
| count the commits so far | read one word | `regs.commit_offset()` |
| read the staged configuration back | read | `0` |
| read the latest status | read `regs.nstat` words; decode with `Status().deserialize(words, word_bw=64)` | `regs.status_offset()` = `3 × window / 4` (`0xC00`) |

- **Nothing reaches the kernel until the commit.** Writes to the configuration only change a staging
  copy (the *shadow*), so the kernel never sees a half-written configuration. The commit sends the
  shadow as it was at that moment; writes after it go into the next configuration.
- **A commit is never merged or dropped.** If the kernel has not yet taken the previous configuration,
  the commit write waits until it has.
- **Status is the latest complete message.** A read never mixes words of two status messages.
- **The kernel chooses when a new configuration takes effect.** Checking with `get_schema_nb` at a
  safe point — between packets, or between samples — means a configuration never changes mid
  computation. [mm_fir](../../../examples/mm_fir/) does this, and goes further by naming the exact
  sample a configuration applies at.
- **A register bank has one owner.** Two bus masters writing the same bank are each served whole, but
  one could commit the other's half-written configuration. If two writers are needed, give each its own
  bank, or send complete configurations through a queue.
- **For a large table, use a BRAM window.** The configuration is copied at each commit, which is
  cheap for a few dozen words. For a thousand filter taps or a lookup table, write the table into a
  [BRAM window](#bram-window) and announce it with a doorbell.

**RTL:** `RegBankView(name, ncfg, nstat, cfg_axis, status_axis, law=12)`, with the word counts in
place of the types; module `mm_regbank.v`.

### Register bank or register map?

A [register map](./regmap.md) is the same idea — a register file a host writes to configure a kernel —
realized for the other kind of kernel:

| | Register map (`RegMap`) | Register bank (`MemSlaveRegBank`) |
|---|---|---|
| for a | host-activated kernel | free-running kernel |
| realized as | Vitis-generated `s_axilite`, inside the kernel | hand-written RTL (`mm_regbank`), beside the kernel |
| bus | AXI-Lite: one word per transaction | AXI4 through the adaptor and crossbar: bursts |
| layout | named fields, each at its own offset | one `DataSchema` in a shadow at offset 0; COMMIT and status at fixed offsets |
| the host writes | a field at a time, straight into what the kernel uses | into a shadow the kernel never sees |
| the kernel sees a change | at its next launch, when `ap_start` latches its arguments | when it takes the next config message, one per COMMIT |
| status | output fields the host reads (typically after `ap_done`) | the latest complete status message |
| per-field access modes (`W1C`, `W1S`, hooks) | yes | no: whole messages only |

COMMIT plays the role `ap_start` plays for a host-activated kernel: the one moment the configuration
changes hands. The bank exists because `s_axilite` is no use to a free-running kernel, which cannot see
a write happen. The two share no code today; laying the bank's shadow out with `RegMap`'s field
offsets — so a host could write one field by name, and one generated host header could serve both — is
a natural next step, not yet built.

## BRAM window

A **BRAM window** is a block of memory that a bus master and the kernel share: the bus master reads
and writes it by address through one port of a dual-port BRAM, and the kernel uses the other port. It
is for data the kernel wants random access to — a table, a frame — rather than a stream.

```python
bram = MemSlaveBramWindow(name="bram", sim=sim, nelem=256, clk=clk)
```

| parameter | default | meaning |
|---|---|---|
| `nelem` | `512` | the memory's size in bus words; a power of two, and `nelem × bytes-per-word` must fit in the window |

**The kernel side** is the memory's port B. In pysim it is two plain method calls, which take no
simulated time (the kernel's own model accounts for the BRAM's one-word-per-cycle access):

```python
x = bram.port_b_read(i)
bram.port_b_write(i, value)
```

**The bus side:**

| to | do | at offset |
|---|---|---|
| write words *i* .. *i+n-1* | write *n* words | `i × bytes-per-word` |
| read words *i* .. *i+n-1* | read *n* words | `i × bytes-per-word` |

An access beyond `nelem` is an error: a write there is dropped, and a read returns 0 with SLVERR.

**The kernel is not told when the data changes.** This view moves data, not messages. To tell the
kernel that new data is ready, the bus master writes the memory, then writes a doorbell — a packet to
a queue in, or a commit to a register bank. For that order to hold, the BRAM window and the doorbell
view must be **in the same adaptor**; see [Ordering](./slave.md#ordering).

**RTL:** `BramView(name, kport, baw=9, law=12)`, where `baw` is log2 of the memory's size in words and
`kport` names the kernel's port B nets; modules `mm_bram_port.v` and `bram_t2p.v`.
