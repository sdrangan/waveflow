# Plan: host-side endpoints for the memory-mapped slave adaptor

> **Status (2026-10-03): Stages 0, 1 and 2 BUILT** (branch `mm-host-endpoints`, unpushed). mm_fir
> runs one host program over the bus in pysim, directly in pysim, and over the bus at RTL (811 / 776,
> bit-exact). Stage 3 is the deferred list. One new open question: which bus-master model is right. Plan text below is the design as proposed; where the build departed from it, a
> **Built:** note says how and why. Section headings are cited from code — do not rename them.

## Motivation

The [slave adaptor](../docs/guide/interface/axi_mm/slave.md) keeps the **kernel** side clean: the
kernel reads and writes ordinary streams and never sees an address. The **bus master** side has no
such layer. Every host writes the protocol by hand from address tables:

- write `[len | data]` to queue in, after reading the free slots so the bus never stalls;
- read queue out's occupancy at `window/2`, then pop at most that many words at `0`;
- write the config shadow at `0`, then any value at `window/2` to COMMIT;
- read status at `3·window/4` and decode it.

That protocol is written **three times** today, and nothing checks the copies agree:

| copy | where |
|---|---|
| pysim host | `examples/mm_fir/mm_fir.py::FirHost` (`_commit`, `_drain`, the vacancy loop; `REGS + 0x800`, `+ 0xC00`, `QOUT + 0x800` as literals) |
| XSI host | `examples/mm_fir/mm_fir_xsi.py::host_actions` + `render_tb` (an action list `W / POLL_NCFG / WAIT_VAC / DRAIN` and a hand-written C++ state machine with `STATUS_A = REGS + 0xC00`, `OCC_A = QOUT + 0x800`) |
| docs | `docs/guide/interface/axi_mm/slave.md` (the runnable example's `Host`) and `slave_views.md` (the bus-side tables) |

The rules belong in one place, and a user should not have to learn them.

## The idea

**A bus master gets the same endpoints it would get from a direct connection.** For each view, the
master asks for an endpoint by the view's name and receives an ordinary `StreamIFMaster`,
`StreamIFSlave` or `Region`. The endpoint's interface turns each call into the view's bus protocol on
a shared `MMIFMaster`:

```python
host_mm = BoundMemSlaveAdaptor(slave_map, master=self.m)     # name TBD, see D6
qin  = host_mm.stream_master("qin")       # a StreamIFMaster
qout = host_mm.stream_slave("qout")       # a StreamIFSlave, has_tlast=False
cfg  = host_mm.stream_master("regs")      # a StreamIFMaster: write(cfg) = shadow + COMMIT
stat = host_mm.status("regs")             # a latest-value endpoint, see D4
buf  = host_mm.region("bram", S16)        # a Region: read_slice / write_slice

yield from cfg.write(make_cfg(taps, apply_at=0))
yield from qin.write(samples)                       # blocks until queue in has taken them
y = yield from qout.get_array(S64, len(samples))    # blocks until that many are ready
```

Endpoints already delegate their transport to their interface (`StreamIFMaster.write` calls
`self.interface.write(...)`), and the derived interfaces (`CreditStreamIF`, `AckedStreamIF`) already
use that to put a different transport behind a standard endpoint. A memory-mapped stream is one more
derived interface.

What this buys:

- **Host code does not change between memory-mapped and direct.** The kernel side is already plain
  streams; now the host side is too. Switching is a wiring change, and a test can run one design both
  ways and compare.
- **The protocol is written once per backend**: once in Python (pysim, and later PYNQ), once in C++
  (the XSI BFM library), both driven by one generated address map.
- **HLS lowering becomes a wiring choice** (later, not this plan): a kernel acting as bus master just
  writes a stream, and the bus leg is realized outside it by a `MemWStream` aimed at the view, which is
  already gated at RTL.

## Decisions

**D1. Views are named by `view.name`.** Every view already has a `name=`; a dict key would be a second
name that can disagree. `MemSlaveAdaptor(views=[...])` keeps its list, because list order *is* the
address order (view *k* at `k × 4 KB`). The adaptor rejects two views with the same name.

**D2. The address map is plain data.** `MemSlaveMap` is a frozen dataclass with no sim objects:

```python
@dataclass(frozen=True)
class ViewEntry:
    name: str
    kind: str                 # "queue_in" | "queue_out" | "regbank" | "bram"
    base: int                 # absolute bus address of the view's window
    window: int
    mem_dwidth: int
    depth: int | None = None  # queues
    cfg_type: type | None = None      # regbank
    status_type: type | None = None   # regbank
    nelem: int | None = None  # bram

@dataclass(frozen=True)
class MemSlaveMap:
    views: dict[str, ViewEntry]
```

It is built **after** `assign_address_ranges`, because that is when bases exist:

- `MemSlaveMap.from_adaptor(adaptor)`: bases from `adaptor.s_mem.addr_range` + `offset_of(view)`;
- `MemSlaveMap.from_views([regs, qin, qout])`: each view on its own crossbar slot (mm_fir's
  `per_view` topology), bases from each view's own `s_mem.addr_range`.

Both raise if a range has not been assigned. Because the map is plain data, it can be serialized to a
C++ header for the XSI host (Stage 2), and later for real host software. That also closes "no host
header is generated" in the slave page's *What is not built yet*.

**D3. Blocking, implemented by polling — never by stalling the bus.** Every endpoint call blocks like
the stream method it replaces: `write()` returns when the queue has taken the words, and `get_*()`
when the words are there. Underneath, the interface **polls** (reads free slots or occupancy, sleeps
`poll_cycles`, tries again) and issues a data transfer only when it fits. It never issues a write a
full queue would stall.

The reason is that a stall is not equivalent to a blocked stream. A stalled write holds WREADY low,
which holds the adaptor's single front, which blocks **every view behind it, for every master**. A
host with one process pushing to queue in and another popping queue out then deadlocks as soon as the
kernel blocks on a full queue out: the pop cannot get through the stalled front. With direct streams
the same host runs fine. Polling keeps the two equivalent. Specifics:

- A packet larger than the free space goes out in pieces as room appears. The in-band framing allows
  this (a packet may span any number of bursts); the length header goes with the first piece.
- `poll_cycles` is a parameter of `BoundMemSlaveAdaptor` (default 8, mm_fir's value).
- Polls are real bus reads and cost real cycles. That is the honest cost of a memory-mapped queue.

**D4. Status is a latest-value channel, not a stream.** Reading the register bank's status twice
returns the same message, and a status the host never reads is simply overwritten. No stream endpoint
has those semantics, so status gets a new primitive, **`LatestValueIF`**:

- master side: a `StreamIFMaster` (the kernel's `m_status`, unchanged). Writes never block; each
  complete message replaces the previous one;
- slave side: `LatestValueIFSlave.read()` returns the latest complete message, typed by the
  interface's schema (all zeros before the first, matching the RTL bank's reset).

Memory-mapped, `host_mm.status("regs")` returns a `LatestValueIFSlave` whose `read()` is a bus read of
the status half. Direct, the kernel's `m_status` binds to a `LatestValueIF` and the host holds its
slave. Same host code either way.

*Open:* whether `LatestValueIF` lowers to anything at RTL other than the register bank's status half.
For this plan it is pysim-only in direct mode.

**D5. Queue out is unframed, end to end.** The RTL drops TLAST on the bus side, so a bus master cannot
see packet boundaries. The honest declaration is `has_tlast=False` on every endpoint of that path:

- the host-side endpoint is `StreamIFSlave(has_tlast=False)`, so `get()` needs a word count, and
  `get_schema` / `get_array` work because their size is known;
- `MemSlaveRStream.s_in` becomes `has_tlast=False` (today `True`, though it ignores TLAST);
- a kernel feeding queue out declares its `m_out` `has_tlast=False`. For mm_fir that is the truth
  already: its RTL kernel has no TLAST pins (`mm_fir_xsi.py::render_top` ties them off).

Direct mode needs this too: `StreamIF.bind` rejects a master and slave whose `has_tlast` differ.

**Exactly n, not at most n.** Today `QueuedTransferIFSlave.get(nwords_max=n)` returns *at most* n
words and **drops the rest of the burst** (`interface.py`, `words = words[:nwords_max]`). On an
unframed stream that loses data silently. The memory-mapped `get(nwords_max=n)` returns exactly n
(blocking until they are there), which is what an HLS read of n words does. Whether the existing
direct-stream `get` should change too is out of scope; it is recorded under *Open questions*.

**D6. Names.** `BoundMemSlaveAdaptor` for the host proxy, by analogy with `BoundRegMap` (a host-side
proxy binding a register map to an `MMIFMaster`). Not `...IF`: in this codebase an `Interface` is a
connection between two endpoints, and this is a factory of endpoints over one. Endpoint factories:
`stream_master(name)`, `stream_slave(name)`, `status(name)`, `region(name, element_type)`. Each
raises if the view's kind does not support it (`stream_master("qout")` is an error that names the
right call).

**D7. Endpoints are cached per view.** Asking twice for `stream_master("qin")` returns the same
endpoint, because the interface keeps per-view state (a packet in progress). Two *different* host
objects writing one queue in is the register bank's one-owner rule again, and is rejected.

## The interfaces (pysim)

All in a new `waveflow/hw/mm_host.py`, sharing one `MMIFMaster`:

| interface | host endpoint | a call does |
|---|---|---|
| `MmQueueInIF` | `StreamIFMaster` (framed) | `write(words)`: poll free slots; write `[len | first piece]`, then further pieces as room appears |
| `MmQueueOutIF` | `StreamIFSlave(has_tlast=False)` | `get(nwords_max=n)` / `get_array(T, k)` / `get_schema(T)`: poll occupancy until the words are there, then pop them in bursts of at most `window/2` bytes |
| `MmRegBankCfgIF` | `StreamIFMaster` | `write(cfg)`: write the shadow, then COMMIT. A COMMIT can stall the bus if the kernel has not taken the previous config, so this polls the commit count (`ncommit`) against messages taken — see *Open questions* |
| `LatestValueIF` (D4) | `LatestValueIFSlave` | `read()`: read `nstat` words at the status half, decode |
| (none) | `Region` | `region(name, T)` returns `master.region(base, T)`. `Region` already runs over an `MMIFMaster`; only the address was missing |

The `*_nb` variants (`get_array_nb`, `get_schema_nb`) do one poll and return `None` if the words are
not there.

## mm_fir

mm_fir is the witness, as it was for the adaptor. Every rung keeps its current pass criteria
(bit-exact against `fir_golden`, status `(nsamp, ncfg, late)`, `late == 1` for the negative
control).

### The host rewritten

`FirHost` becomes two processes on one `MMIFMaster`, the natural shape for stream code:

- **writer**: for each config, `cfg.write(make_cfg(...))`, then
  `while (yield from stat.read()).ncfg < want: sleep` (the existing "received before its sample"
  rule, which stays: it is protocol, not bus mechanics); for each packet, `qin.write(chunk)`;
- **reader**: `y = yield from qout.get_array(S64, len(x))`, or in packets.

What disappears: `_drain` before every push (it existed because one process could not both push and
pop; two processes and D3's polling remove the deadlock it guarded against), the vacancy loop, and
every address literal. `REGS`, `QIN` and `QOUT` remain only where the system assigns ranges.

The `lag` knob (negative control) is unchanged.

### Direct mode

`MmFirSystem` gains `link: str = "mm"` with `"mm"` or `"direct"`. Direct binds the host's endpoints
straight to the kernel: `cfg` to `fir.s_cfg`, `qin` to `fir.s_in`, `fir.m_out` to `qout`, and
`fir.m_status` to a `LatestValueIF`. **The host class is identical in both modes.** `one_front`
continues to select the topology when `link == "mm"`.

### Kernel declarations

`MmFir.m_out` becomes `has_tlast=False` (D5). `m_status` stays as it is (it feeds the register bank,
which completes a message on its `NSTAT`-th word).

### XSI host

`host_actions` and the hand-written state machine in `render_tb` are replaced by the BFM classes of
Stage 2 and a header generated from the `MemSlaveMap`. The host becomes two `XsiSimObj`s (writer,
reader) sharing one `AxiMmMaster`, mirroring the pysim host.

**The cycle gate will move.** `EXPECTED_CYCLES = {"per_view": 857, "one_front": 823}` measures the
current single-process protocol. The new protocol issues a different sequence of bus operations, so
both numbers are re-measured, not carried over, and the commit records old and new with the reason.
The comment block above `EXPECTED_CYCLES` (2096 → 857 history, pysim 17% optimistic) is updated with
the new pysim-versus-RTL figure.

### Docs

- `docs/examples/mm_fir/index.md`: the host section shows the endpoint code.
- `docs/guide/interface/axi_mm/slave.md`: the runnable example's `Host` uses `BoundMemSlaveAdaptor`;
  *Finding a view's address* becomes *Reaching the views from a bus master*; *What is not built yet*
  loses the address and host-header items.
- `slave_views.md`: each view's *bus side* becomes one line naming the endpoint it gives a master.
  The address tables move to `slave_howitworks.md`, as the contract the RTL modules implement.

## Stages

### Stage 0 — verify the shared master

Before building, confirm what two processes on one `MMIFMaster` do in
pysim: whether calls interleave per transaction (expected: the crossbar serializes them, and the
adaptor's `serialize_transactions` holds the channel for each), and that a poll from one process can
run while the other sleeps between polls. If they cannot interleave, either this stage adds that, or Stage 1
falls back to a single process that alternates writer and reader steps. (Its own stage so the answer is
recorded rather than assumed; it may take an hour, or it may reshape Stage 1.)

**Built:** answered from the code, then confirmed in a run. `MMIFMaster` has no per-master lock: each
call is its own crossbar process, and only the slave's channel is contended (for the adaptor,
`half_duplex` + `serialize_transactions` hold one channel per transaction). Two host processes on one
master therefore interleave per transaction. `test_polling_writer_and_reader_share_one_front_without_deadlock`
runs exactly that; its negative control (a raw, non-polling writer) deadlocks as D3 predicts.

### Stage 1 — the map, the pysim endpoints, mm_fir in pysim

1. `MemSlaveMap` / `ViewEntry`, `from_adaptor`, `from_views` (D1, D2); unique view names in
   `MemSlaveAdaptor`.
2. `waveflow/hw/mm_host.py`: `BoundMemSlaveAdaptor`, `MmQueueInIF`, `MmQueueOutIF`,
   `MmRegBankCfgIF`, `LatestValueIF`, `region()` (D3-D7).
3. D5: `MemSlaveRStream.s_in` unframed; exact-n `get` on the memory-mapped endpoint.
4. mm_fir: two-process host, `link="direct"`, `m_out` unframed.
5. Docs example and the three slave pages.

**Gates (pysim):**

- `tests/hw/test_mm_host.py`, per view: a packet larger than the queue goes through in pieces;
  `get(nwords_max=n)` returns exactly n across several kernel bursts; a config write is one message;
  `status.read()` is the latest message and does not consume it; `region()` round-trips.
- **The no-stall property:** a host with a writer and a reader process, a kernel that blocks on a
  full queue out — runs to completion through one front. A hand-written host that writes without
  polling, same scenario, deadlocks (negative control: the reason D3 exists).
- `tests/examples/test_mm_fir.py`: every existing rung, now parametrized over
  `link in ("mm", "direct")` and, for `"mm"`, `one_front in (False, True)`. Same `y`, same status,
  same `late` detection, all modes.
- The docs snippet harness runs the rewritten example.

**Built** as planned, with these departures:

- **D7's cross-proxy rejection is not built.** One proxy caches its endpoints (asking twice returns
  the same object); two *different* proxies on one view are not detected.
- **`MmStreamIFSlave` is a subclass, not a plain `StreamIFSlave`.** Stream writes go through the
  interface, but stream reads do not: `get_schema`, `get_array`, `get_pipelined` all call the base
  class's `get`, which reads the endpoint's own buffer. A mixin (`_InterfacePull`) placed between
  `StreamIFSlave` and that base replaces exactly that one step, so every typed read keeps its code.
- **Queue in waits for room for the WHOLE packet when it fits the queue**, instead of writing pieces
  as room appears. In pysim the view holds a partial packet until it completes and then hands it
  over whole (burst-granular back-pressure), so a piece-wise write of a packet that fits would end in
  a stall. Pieces are used only for a packet longer than the queue.
- **The mm_fir reader reads in the writer's packet sizes**, from a shared `host_schedule`. Queue out
  is unframed, and in direct mode a plain unframed `StreamIFSlave.get(nwords_max=n)` returns ONE
  burst cut to *n* (the open question below), so exact-size reads are the only host code that is the
  same in both wirings.
- **Two mm_fir assertions encoded the old single-process host** and were changed, not deleted:
  per-view vs one-front timing was *equal* and is now *one front slower* (writer and reader overlap
  behind separate fronts and take turns behind one: 423 vs 742 cycles on the docs scenario); the late
  config's switch sample was *exactly 128* and is now "the sample it arrived at", which is 128 over
  the bus and 112 direct.
- **pysim no longer compares with RTL** on mm_fir until Stage 2: the RTL gate (857 / 823) still runs
  the single-process C++ host. `docs/examples/mm_fir/pysim.md` says so.

### Stage 2 — XSI: generated header, BFM endpoint classes, mm_fir at RTL

1. `MemSlaveMap.to_cpp_header()` → `<name>_map.h`: per-view base, window, depth, `NCFG`, `NSTAT`.
2. In the XSI BFM library, beside `AxiMmMaster`: `MmQueueWriter`, `MmQueueReader`, `MmRegBankCfg`,
   `MmStatusReader`. Each is a small non-blocking state machine over `AxiMmMaster` ops, implementing
   D3's polling. Check first whether `AxiMmMaster` accepts ops from two `XsiSimObj`s; if not, add a
   per-caller queue.
3. mm_fir's `render_tb` rewritten on them; `host_actions` deleted.
4. Re-measure `EXPECTED_CYCLES` for both topologies (see *XSI host*).

**Gates (`-m xsi`):** `tests/examples/test_mm_fir_xsi.py` bit-exact and status in both topologies, new
cycle counts asserted exactly.

**Built** (2026-10-03):

- `waveflow/build/xsi/xsi_mm_host.h`: `MmView` (the C++ `ViewEntry`, offsets computed, never stored)
  and four endpoints -- `MmQueueWriter`, `MmQueueReader`, `MmRegBankCfg`, `MmStatusReader` -- each a
  start/step/busy state machine. Added to `XsiWorkspace.HARNESS_FILES` only; it is a new file, so no
  example's committed harness copy changes.
- **No per-caller queue was needed.** `AxiMmMaster` already queues ops from any caller and serves them
  in order, one at a time, so two programs share it as two pysim processes share an `MMIFMaster`.
- `MemSlaveMap.to_cpp_header`; mm_fir's `map_header(topology)` builds it from the SAME `MmFirSystem`
  the pysim gates run, so the RTL host restates no address. `host_actions` is deleted; `render_tb`
  renders `host_schedule` and a `Writer` + `Reader` on the endpoints.
- **The RTL on disk was stale** before this stage (`include/mm_fir_task.h` changed after its last
  csynth, not on this branch). Re-synthesized first, then the OLD host was run on the fresh RTL:
  still 857 / 823. So the move below is the host program and nothing else.
- **New counts: 811 / 776** (73 ops, 6 polls; was 857 / 823, 68 ops, 2 polls). Bit-exact, status
  200 / 2 / 0, both topologies.
- **pysim vs RTL:** one_front 742 vs 776 (4% optimistic). per_view 423 vs 811 -- attributed by a pysim
  probe to the **bus-master model**: `AxiMmMaster` keeps one transaction outstanding, a pysim
  `MMIFMaster` lets a read and a write run at once. Wrapping the pysim master in a capacity-1 resource
  makes per_view 742, equal to one_front. See *Open questions*.
- C++ trap: Windows headers define a `min` macro; `std::min(a, b)` breaks, `std::min<T>(a, b)` and
  `(std::min)(a, b)` do not, and `(std::min)<T>(...)` is not valid C++ at all.
- `WANT_XSI_GATES` unchanged: same tests, new numbers. `WANT_XSI_GATES` unchanged (same tests, new numbers) unless a test is
added for the BFM classes on their own, in which case it goes up by that count.

### Stage 3 — later, not in this plan

- Packet boundaries on queue out: a "words to the next TLAST" register in `mm_queue_out.v`, after
  which the host endpoint may be framed. Only if a design needs it.
- A PYNQ backend: the same `BoundMemSlaveAdaptor` over a hardware `MMIFMaster`, through the `Device`
  seam.
- A Vitis kernel as the bus master, lowered to `MemWStream` / `MemRStream` command streams aimed at
  the view. Note that path stalls rather than polls (D3), so it is safe only when the kernel's master
  port carries no other traffic.

## Open questions

- **Config backpressure without a stall.** A COMMIT while the previous config is untaken stalls the
  bus (by design in the RTL bank). To keep D3, `MmRegBankCfgIF` must know the previous message was
  taken before it commits. The bank exposes the commit count, not a taken count. Options: (a) accept
  this one stall, since it lasts at most `NCFG` cycles once the kernel reads (measured 87 with the
  kernel held off); (b) add a taken count to `mm_regbank.v`'s COMMIT read. Recommendation: (a) for
  Stage 1, recorded in the endpoint's docstring; revisit if a design holds its config off for long.
- **Direct-stream `get(nwords_max)` drops words.** D5 avoids it on the new endpoint, but the existing
  behaviour (truncate the burst, discard the rest) is a data-loss hazard on any unframed stream.
  Worth its own issue.
- **`LatestValueIF` at RTL** (D4): pysim-only in direct mode for now.
- **Which bus-master model is right** (found in Stage 2). The C++ `AxiMmMaster` keeps ONE transaction
  outstanding; a pysim `MMIFMaster` lets a read and a write proceed at once (separate AR/AW channels,
  as real AXI allows). With a two-process host and views on separate slots the two disagree by ~2x
  (423 vs 811 cycles); behind one front they agree to 4%, because the front serializes anyway.
  Options: (a) an opt-in `max_outstanding=1` on `MMIFMaster`, for hosts that really are one
  outstanding; (b) an opt-in dual-channel mode on `AxiMmMaster` (one read + one write outstanding),
  so the RTL testbench overlaps as pysim does -- touches `xsi_bfm.h` and therefore every example's
  committed harness copy; (c) leave both and record the gap. Recommendation: (b) if the target host
  is a CPU or DMA that overlaps reads and writes, (a) if it is a simple single-threaded driver.
