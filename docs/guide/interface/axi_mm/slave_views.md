---
title: Slave adaptor views
parent: AXI-MM
grand_parent: Interfaces
nav_order: 4
audience: python
api: [MemSlaveWStream, MemSlaveRStream, MemSlaveRegBank, MemSlaveBramWindow, MemSlaveCreditIn, QueueView, RegBankView, BramView, CreditInView]
summary: "The views of the memory-mapped slave adaptor — queue in, queue out, register bank, BRAM window, and credit in (the producer end of a credit stream routed over the bus). For each: what it is for, its constructor, what the kernel does with it, and what a bus master does with it. Also: register bank versus register map."
---
# Slave adaptor views

A [slave adaptor](./slave.md) is made of views, each a way for a bus master to reach a free-running
kernel. This page takes them one at a time. Each section says what the view is for, gives its
constructor, then describes its two sides:

- **The kernel side** is what the kernel's code does: read or write a stream (or, for the BRAM window,
  a memory port). The kernel never sees an address.
- **The bus side** is what a bus master — a host program, or a test standing in for one — does. It
  asks the adaptor for the view by name and gets an ordinary endpoint
  ([Reaching the views from a bus master](./slave.md#reaching-the-views-from-a-bus-master)); the
  endpoint turns each call into bus reads and writes. The addresses it uses are in
  [how it works](./slave_howitworks.md#the-address-map-behind-the-endpoints).

All the constructors take `name`, `sim` and `clk` like any module, and these two:

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

**The bus side** is a `StreamIFMaster`, given the host's end of the view's interrupt line
(`qin.m_irq`, see [Interrupts](./slave.md#interrupts)):

```python
qin_ep = mm.stream_master("qin", irq=qin_irq)
yield from qin_ep.write(samples)        # one packet; returns once the queue has taken it
```

- **One `write` is one packet**, and the kernel's `get()` returns it whole.
- **`write` waits for room on the interrupt** — high while the free space is at least the threshold
  the endpoint sets — never by stalling the bus, and never by reading the free space. A packet goes in
  one piece once there is room for all of it.
- **The window's upper half is control:** a write there sets the interrupt threshold. Pushes go to the
  lower half (an AXI4 burst — at most 2 KB at 64 bits — from the base stays there).
- **A completed write means the queue took the words, not that the kernel has read them.**
- **Kernel to kernel.** A kernel that writes memory through `MemWStream` can feed another kernel's
  queue by pointing its base address at the window and putting the packet's length first — the format
  in [how it works](./slave_howitworks.md#queue-in). That path stalls rather than waits, so it suits a
  master port that carries nothing else.

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
queue's `depth`, and give the kernel a `StreamIFMaster` on the other end, declared
`has_tlast=False`: queue out carries no packet boundaries (see below). The kernel writes as it would
to any stream, and blocks when the queue is full:

```python
yield from self.m_out.write(words)
```

**The bus side** is an `MmStreamIFSlave` — a `StreamIFSlave` declared `has_tlast=False` — given the
host's end of the view's interrupt line (`qout.m_irq`):

```python
qout_ep = mm.stream_slave("qout", irq=qout_irq)
y = yield from qout_ep.get(nwords_max=n)            # exactly n words
y = yield from qout_ep.get_array(Sample, count=n)   # n typed elements
```

- **Reads name their size.** The bus cannot see where the kernel's packets end (the RTL drops TLAST),
  so `get()` without a count is refused. `get(nwords_max=n)` returns **exactly** *n* words, waiting
  until they are all there — what an HLS read of *n* words does.
- **Reads wait on the interrupt.** The endpoint sets the threshold to the words it wants, sleeps until
  the interrupt says they are there, and pops them. It never reads the count, and never pops an empty
  queue. A write to the upper half of the window sets the threshold.
- The `*_nb` reads return `None` unless all the words are already there (the interrupt is high).

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

**The bus side** is two endpoints, one per direction:

```python
cfg_ep = mm.stream_master("regs")
yield from cfg_ep.write(make_cfg(...))   # one config message to the kernel

status_ep = mm.status("regs")
st = yield from status_ep.read()         # the latest status message, decoded
```

- **One `write` is one configuration.** It writes the configuration registers (a staging copy the
  kernel never sees, the *shadow*), then COMMIT, which sends the shadow to the kernel as one message.
  The kernel never sees a half-written configuration.
- **A commit is never merged or dropped.** If the kernel has not yet taken the previous configuration,
  the commit waits until it has. This is the one place the endpoint can stall the bus, because the
  bank reports how many commits it accepted, not how many the kernel took; the stall lasts until the
  kernel reads.
- **`status.read()` does not consume.** Read it twice and you get the same message. It is a
  `LatestValueIFSlave`, the same endpoint a direct connection gets from a `LatestValueIF`.
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

**The bus side** is a `Region` — the same element-indexed view of memory a kernel uses through
its `m_axi` port:

```python
buf = mm.region("bram", Sample)
yield from buf.write_slice(0, table)       # elements 0 .. len(table)-1
x = yield from buf.read_slice(16, 32)      # elements 16 .. 31
```

An access beyond `nelem` is an error: a write there is dropped, and a read returns 0 with SLVERR.

**The kernel is not told when the data changes.** This view moves data, not messages. To tell the
kernel that new data is ready, the bus master writes the memory, then writes a doorbell — a packet to
a queue in, or a commit to a register bank. For that order to hold, the BRAM window and the doorbell
view must be **in the same adaptor**; see [Ordering](./slave.md#ordering).

**RTL:** `BramView(name, kport, baw=9, law=12)`, where `baw` is log2 of the memory's size in words and
`kport` names the kernel's port B nets; modules `mm_bram_port.v` and `bram_t2p.v`.

## Credit in

A **credit in** is the producer end of a [credit stream](../derived/credit_stream.md) routed over the
bus. The consumer kernel's bus writer writes its **cumulative** count of words consumed here; the
producer kernel receives each new count on its credit stream and knows how much room the consumer's
queue in has before it writes. You will rarely build one by hand: a kernel declares it, with
`CreditIn("u_crd", port="m_u")` in its `mm_views`, and
[`MmCreditStreamIF`](../derived/credit_stream.md#over-a-shared-bus) does the wiring.

```python
crd = MemSlaveCreditIn(name="u_crd", sim=sim, clk=clk)
```

| parameter | default | meaning |
|---|---|---|
| `ctr_bits` | `16` | the counter's width; the stream to the kernel is this wide |

**The kernel side** is the stream endpoint `crd.m_out`, joined to the credit half (`crd_ep`) of the
kernel's `CreditStreamMasterIF`. The kernel does nothing new: its `write` sleeps on that stream until
the credit covers the burst.

**The bus side** is a single word written anywhere in the window — by a writer, not a host program, so
there is no host endpoint for it.

- **A write never waits.** The view is a latest-value register, not a queue: a count is cumulative, so
  the newest value is the whole truth and may replace one the kernel has not taken yet. (Contrast a
  register bank's COMMIT, which waits for the kernel to take the previous config.) That is what lets a
  consumer return credit over a shared bus without ever stalling it.
- **The kernel sees fewer values when it lags, never older ones.**
- A read returns the current value — for debugging; nothing needs it.

**RTL:** `CreditInView(name, axis, law=12)`; module `mm_credit_in.v`.
