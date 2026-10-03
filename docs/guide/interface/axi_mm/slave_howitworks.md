---
title: Slave adaptor — how it works
parent: AXI-MM
grand_parent: Interfaces
nav_order: 5
audience: python
api: [MemSlaveAdaptor, render_view_slot, render_adaptor_slot, AxiXbarConfig, generate_axi_xbar]
summary: "Internals of the memory-mapped slave adaptor, for readers changing or verifying it: the RTL modules and the generated decoder, the AXI front end, what each view does in RTL, why the register bank needs no lock, serialize_transactions in pysim, the AXI crossbar, how closely pysim matches RTL, and the gates."
---
# Slave adaptor — how it works

This page is for readers who change the [slave adaptor](./slave.md) or want to know why it behaves as
it does. Using one needs only the [overview](./slave.md) and the [views](./slave_views.md).

## The RTL modules

All of it is in [`waveflow/build/rtl/`](../../../../waveflow/build/rtl/):

- **`axi_slave_front.v`** is the only module that speaks AXI. It turns each beat into one request on
  a simple **request bus** and is the source of the ordering guarantee.
- **One module per view** (`mm_queue_in.v`, `mm_queue_out.v`, `mm_regbank.v`, `mm_bram_port.v`)
  implements that view's semantics on the request bus. Widths, depths and sizes ride on Verilog
  parameters; the files are never rewritten.
- **The wiring** — instances, nets, and the decoder when several views share a front — is generated
  by [`mm_adaptor_gen.py`](../../../../waveflow/build/mm_adaptor_gen.py). The decoder is the only
  generated logic, and it is a table.

Each module has a pysim twin with the same semantics: `MemSlaveWStream`, `MemSlaveRStream`,
`MemSlaveRegBank`, `MemSlaveBramWindow`, and `MemSlaveAdaptor` for the front plus decoder.

### The front end

- Serves **one AXI transaction at a time**, reads and writes alike, in the order accepted; when an AW
  and an AR are both waiting it alternates between them.
- **Refuses** (SLVERR, the beat never reaches a view): an `AxSIZE` other than the bus width — a stream
  cannot carry a partial word — a WRAP burst, and any W beat whose `WSTRB` is not all ones (consumed
  and dropped, so the burst still completes).
- INCR bursts advance one word per beat; FIXED bursts repeat one address.
- Keeps at most one read outstanding at a view and issues the next in the cycle the previous response
  is taken, so a view with a one-cycle registered response sustains **one beat per cycle**.

Measured through the crossbar: both directions sustain one beat per cycle after a fixed 4–5 cycles,
and a refused burst completes at full speed with SLVERR.

### The decoder

With several views behind one front (`render_adaptor_slot`), view *k* answers local addresses
`[k × 4 KB, (k+1) × 4 KB)`. The decoder routes a request by the address bits above 12, takes the read
response from the view that accepted the read (one read outstanding, so a latched select is enough),
and answers an address in the span's unused tail with SLVERR so a stray read cannot hang the bus. The
span is the view count rounded up to a power of two, because the select field is whole bits.

## The address map behind the endpoints

A bus master's endpoints ([Reaching the views](./slave.md#reaching-the-views-from-a-bus-master))
turn each call into reads and writes at these offsets from the start of the view's window
(`W` = the window size, 4 KB inside an adaptor). The endpoints are in
[`waveflow/hw/mm_host.py`](../../../../waveflow/hw/mm_host.py), and the offsets are written once, on
`ViewEntry` (`commit_addr`, `status_addr`, `max_burst`). Anything driving the bus without the
endpoints — a C++ testbench today, real host software later — follows the same table.

| view | offset | write | read |
|---|---|---|---|
| queue in | anywhere | push: a packet is `[len, d0 .. d(len-1)]` | the free slots, `0..depth`; no side effects |
| queue out | `[0, W/2)` | ignored | **pop** one word per beat; an empty queue answers 0 with SLVERR |
| | `[W/2, W)` | ignored | the words ready, `0..depth`; no side effects |
| register bank | `[0, W/2)` | the config shadow, word *i* at `i × bytes-per-word` | the shadow |
| | `W/2` | **COMMIT**: snapshot the shadow, send it as one message | the number of commits so far |
| | `[3W/4, W)` | — | word *i* of the latest complete status message |
| BRAM window | `i × bytes-per-word` | word *i* of the memory | word *i* of the memory |

What the endpoints do with it:

| endpoint call | on the bus |
|---|---|
| queue in `write(words)`, *n* ≤ depth | read the free slots until ≥ *n*; write `[n, words]` |
| queue in `write(words)`, *n* > depth | read the free slots; write as much as fits (the length first); repeat |
| queue out `get(nwords_max=n)` | read the ready count until > 0; pop up to that many; repeat until *n* |
| register bank `write(cfg)` | write the shadow; write COMMIT |
| register bank `status.read()` | read `nstat` words at `3W/4` |
| BRAM `read_slice` / `write_slice` | read / write at `i × bytes-per-word` |

No burst is longer than 256 beats (AXI4's limit) or runs past the part of the window it addresses.

## The views in RTL

### Queue in

The module counts words: the first word of a packet is the length, the next *len* words go to the
AXIS output, and TLAST is asserted on the last. Because it counts rather than looking at bursts, a
packet may span any number of bursts, and an interconnect that splits a burst cannot move a packet
boundary. Words cut through to the stream as each beat arrives. A full FIFO holds WREADY low, which is
how the writer stalls. Gated at RTL with the real synthesized `mem_w_stream` as the producer.

### Queue out

The lower half of the window pops, the upper half returns the occupancy. A read never waits for data:
a slave that held RVALID until data arrived would hold the whole bus, so an empty pop answers 0 with
SLVERR. TLAST from the kernel is dropped, and so is a write (the request bus has no write-error path).

### Register bank

The kernel never reads the registers. `s_cfg.get_schema(Cfg)` **receives a message** — one per
COMMIT, and nothing until the next. Between the host's writes and that message, the configuration
lives in up to three places:

| copy | where | why it exists | size |
|---|---|---|---|
| shadow | register bank | where the host's field writes land, one at a time | `NCFG` words |
| snapshot | register bank | taken at COMMIT; the message is sent from it, so the host can keep writing the shadow while it drains | `NCFG` words |
| the kernel's own | kernel state | whatever the kernel keeps to compute with | up to the kernel |

There is no FIFO between the bank and the kernel: `m_cfg` is driven straight from the snapshot, one
word per cycle. That is why the pysim stream on `m_cfg` must hold exactly one message: the RTL has one
snapshot register, so a second COMMIT while the first message is still untaken stalls the bus until it
has gone (measured: 87 cycles with the kernel side held off).

The kernel's copy is unavoidable — a kernel that uses a configuration across many samples has to hold
it somewhere, just as a host-activated kernel latches its `s_axilite` arguments at `ap_start`. The
snapshot is the one copy that could be dropped, at the price of stalling the host's shadow writes for
the few cycles a message takes to drain; it costs `NCFG × DW` flip-flops (320 bits for `mm_fir`'s
config). For a few dozen words that is negligible; for a large table it is the wrong design, which is
why the views page sends large tables through a BRAM window.

**There is no lock, because nothing is shared that would need one.** The bank never lets the host and
the kernel touch the same storage:

- **The host only writes the shadow, and the kernel never reads it.** A write in progress has nothing
  to collide with.
- **COMMIT is the handoff.** At that instant the snapshot is taken, and from then on the message is
  immutable: later shadow writes cannot reach it.
- **The kernel only ever sees whole messages** — all `NCFG` words of one commit, in order, never half
  of an old configuration and half of a new one.
- **The only blocking is on the bus.** `get_schema` waits until a commit *exists*, not because a write
  is in progress. A second COMMIT while the first message is still untaken stalls the *host's* write;
  a write never makes the kernel wait.

Status works the other way round: the kernel's words go into a live buffer, and a message is published
to the bus side all at once when its last word (or TLAST) arrives, so a read never mixes two.

### BRAM window

The memory is `bram_t2p`, beside the kernel as for any [BRAM between modules](../primitive/bram.md):
the view's module drives port A, the kernel keeps port B.

- A write is **done at its request handshake** — the word is in the memory at the next clock edge,
  before the front can decode the next transaction. That is this view's part in the ordering guarantee.
- One read in flight; the module's read latency is a parameter the generator reads from `bram_t2p.v`'s
  published `READ_LATENCY`, so the two have one source.

## pysim: `serialize_transactions`

`MemSlaveAdaptor`'s port is declared both `half_duplex` and `serialize_transactions`. The second needs
a word of explanation.

The pysim crossbar charges a burst's transfer time *before* taking the slave's channel, and holds the
channel only while the slave's callback runs. For a memory that is harmless. For the adaptor it was
wrong: a short doorbell issued after a long burst reached the views first — the opposite of the RTL.
`MMIFSlave.serialize_transactions = True` makes the crossbar hold the channel for the whole
transaction, as the front does. It is opt-in; every other slave behaves as before.

## The crossbar

The slave ports are reached through AMD's `axi_crossbar` IP, the same core a board design uses, so the
cycle counts measured through it transfer. `AxiXbarConfig` describes it and `generate_axi_xbar`
produces it with `create_ip` (about 30 s, cached). See
[MM Interfaces — how it lowers](modeling.md#how-it-lowers).

## pysim and RTL

With the crossbar's `latency_init` set to the measured 4 cycles, every adaptor operation in pysim lands
within **2 cycles** of RTL — except an operation that waited on a full queue or an untaken config
packet, which pysim releases up to one packet early: a pysim stream hands the kernel a whole packet in
one event, where RTL drains it a word per cycle. The tests assert that difference as a bound, so a
change to the stream model that moves it shows up.

**Behind one front,** two more settings matter, both found by lining up mm_fir's bus operations in
the two backends:

- **`latency_travel = 2`** on the crossbar. Of the 4 cycles, 2 are the request travelling to the
  front, and that travel overlaps whatever the front is serving. Without it, every switch between a
  read and a write behind the front cost pysim 2 extra cycles.
- **`max_outstanding = 1`** on the host's master: one read and one write in flight, as the C++ host.
  Without it, a host's two processes could send two reads through the crossbar together.

With those (and the host's own pacing, `issue_cycles = 2`), mm_fir behind one front is within 1.1% of
RTL. See [the crossbar page](crossbar.md#matching-pysim-to-it).

## Gates

| | pysim | RTL (`-m xsi`) |
|---|---|---|
| queues | `tests/hw/test_mm_queue.py` | `tests/build/test_mm_queue_xsi.py`, `test_mm_queue_memw_xsi.py` |
| register bank | `tests/hw/test_mm_regbank.py` | `tests/build/test_mm_regbank_xsi.py` |
| BRAM window, ordering | `tests/hw/test_mm_bram.py` | `tests/build/test_mm_bram_order_xsi.py` |
| several views, one front | `tests/hw/test_mm_adaptor.py` | `tests/examples/test_mm_fir_xsi.py` (both shapes) |
| crossbar | — | `tests/build/test_axi_xbar_xsi.py` |
