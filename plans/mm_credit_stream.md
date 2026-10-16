# Plan: CreditStreamIF over the bus -- kernel-to-kernel flow control, and the Markov example

> **Status (2026-10-04): BUILT, Stages 0-5** (branch `mm-credit-stream`).  Departures are in **Built**
> at the end; the open items are pysim's 28% timing gap at RTL and the two "Open" questions below.
>
> Was: **proposed (2026-10-03).**  Supersedes Part B of `plans/no_polling_next.md` (the two-kernel
> example is this plan's gate).  Builds on `plans/mm_irq.md`, `plans/bus_address_map.md` (D6: a kernel
> as bus master) and `waveflow/hw/reverse_stream.py` (`CreditStreamIF`, `plans/rf_samp_new.md` Stage 0).

## Why

A kernel that writes another kernel's **queue in** over a shared bus must never write into a full
queue.  A stalled write holds the crossbar path and the target's one adaptor front, so every view
behind that front stops for every master -- including the reads that would drain the queue.  That is
a deadlock, not just lost bandwidth.

The host solved this with the queue's room interrupt.  Between kernels the general answer is
**credit-based flow control**: the writer holds a count of free slots per destination, and the
receiver returns *cumulative* consumed counts to that writer only.  That is point to point: no
broadcast, so no race between writers, state O(fan-out) per kernel, and traffic that grows with data
rather than with N.

**This already exists as `CreditStreamIF`** (a forward stream plus a reverse stream of cumulative
consumed counts, `avail = depth - (written - acked)` masked).  Only the **transport** is new.  This
plan routes that channel over the bus, and leaves the kernel-side endpoints unchanged.

## The design

### D1. One interface, transport chosen at assembly

A kernel declares a `CreditStreamIF` port, as today.  The system decides per link:

| | direct (today) | routed over a crossbar (new) |
|---|---|---|
| forward | stream into the consumer's FIFO | producer's bus writer -> consumer's **queue-in view** (`[len \| data...]`) |
| reverse | stream of cumulative counts | consumer's bus writer -> producer's **credit-in view** |
| receiver's buffer | the stream FIFO | the queue-in view's FIFO (same `depth`) |
| kernel endpoints | `CreditStreamMasterIF` / `CreditStreamSlaveIF` | **the same** |

As with the pysim/RTL boundary, the cut is a build choice and the kernel code does not change.  A
system helper (`route_credit_stream(...)`) does the rewiring; it also reports, per link, the bus
addresses each end needs.

### D2. The credit-in view: a latest-value register, which never stalls the bus

A new view kind, `CreditIn`: the bus writes one counter word; the kernel receives it as a stream
(its existing `crd` slave).  Unlike the register bank's COMMIT -- which stalls while the kernel has not
taken the previous config -- a credit write **overwrites**.  That is safe because credits are
cumulative (rule 1 of `reverse_stream.py`): the newest value is the whole truth.  For the same reason
rule 4's hazard (a saturated reverse FIFO dropping fresh values) cannot occur: the view holds one
value, always the newest.

### D3. Credits are sent by the consumer kernel's existing credit port

`CreditStreamSlaveIF` already emits the cumulative count when it consumes.  Routed, that stream goes
to the consumer's **bus writer**, which writes it to the producer's credit-in view.  The bus writer is
the same component the producer uses for its forward data: **one `MmStreamWriter`** (stream in ->
bus writes at `base + view offset`), used once per direction.

- Forward mode: frames each burst as the queue-in packet `[len | data...]`.
- Credit mode: **coalesces** -- it keeps only the newest pending count and writes that, so a slow bus
  costs staleness, never a backlog.

Rejected: having the queue-in view send credits itself.  It would make every routed view a bus master
(more master ports on the crossbar) and duplicate a count the consumer endpoint already keeps.

### D4. A blocking write for producers that may wait

`CreditStreamMasterIF` was built for RF producers that must never stall: it has only `write_nb`,
which refuses when there is no room.  A kernel like the Markov generator can wait, so add `write`:
sleep on the credit stream until `avail` covers the burst, then write.  That waits on an arrival; it
does not poll.  Rule 2 still holds (it constrains the consumer's credit *offer*, which stays
non-blocking).  Rule 3 holds where it matters: the non-blocking credit read stays bounded.

### D5. Credit batching

Over the bus every credit is a bus write, so the consumer must not offer one per word.  The consumer
endpoint offers every `crd_every` words (default `depth // 2`, the same half-queue rule as the room
interrupt), and whenever it drains to empty.  The producer's `avail` is then stale by at most
`crd_every` words, in the safe direction (it understates room).

### D6. Who still uses the room interrupt

The host.  It is not a slave and holds no credit-in view, so host -> kernel queues keep the room
interrupt (`plans/mm_irq.md`).  Kernel -> kernel links use credits.

### D7. Several writers into one kernel

One routed channel per edge (the NVMe pattern: one queue per producer).  Every queue has one writer;
the receiving kernel round-robins over its input ports in its own code.  A shared multi-writer queue
is not supported -- it would need allocation (request/grant or an atomic tail), which is a different
problem.

### D8. Base addresses at run time

Per `bus_address_map.md` D6: each kernel's bus writer gets its peer's **layout** at compile time and
its peer's **base** at run time, from a config register the host sets once (`peer_base`).  The
producer needs the consumer's base (forward); the consumer needs the producer's base (credits).

## The gate: a two-state Markov chain simulator

Deliberately not useful hardware; every kernel is small so the example is about the links.

```
host -> k1.qcmd    : Cmd(tx_id, seed, x0, p01, p10, n, dstaddr)          [k1/k2 peer_base set once]
k1   -> k2  (routed CreditStreamIF) : Cmd + u[0..n-1]
k2   -> k1  (credits, routed)       : cumulative words consumed
k2   -> mem        : x[0..n-1] at dstaddr  (m_axi bursts)
k2   -> k2.qresp   : Resp(tx_id, n, status)  -> qresp interrupt -> host
host <- mem        : x
```

- **Kernel 1 (generator):** xorshift32 PRNG seeded per command; `u[k]` = top *b* bits (`b = 16`).
  Forwards the command header, then `u`.
- **Kernel 2 (chain):** `p01`, `p10` in Q*b*.  `t0 = u < p01`, `t1 = u >= p10`,
  `x' = x ? t1 : t0`.  Both compares depend only on `u`, so the carried dependency is a 2:1 mux:
  **II=1 is expected** -- a csynth that misses it is a finding.  `x` stored as one `Uint8` per sample
  (bit packing is a follow-on; it touches `plans/stream_array_alignment.md`).
- **Host:** at most `k` jobs in flight (end-to-end admission, bounds the response queue); waits on
  the response queue's interrupt; reads `x` from the shared memory.
- **Checks:** `x` bit-exact against the golden (same PRNG); the fraction of 1s near
  `p01 / (p01 + p10)` (a docs plot); zero stalled bus writes (counted); credit writes per job about
  `n / crd_every`.

## Stages

0. **Survey + D4.**  Read `CreditStreamIF`'s tests and docs page; add the blocking `write`, with
   tests (waits without polling, wakes on credit, wrap-safe).
1. **Routed transport in pysim (D1-D3, D5).**  `CreditIn` view, `MmStreamWriter` (forward and
   coalescing modes), `route_credit_stream`.  Test: one producer/consumer pair run direct and routed
   -- identical data, no bus stall, credit write count as predicted.  `layout_of` / address headers
   learn the new view.
2. **Markov example in pysim.**  Two kernels, host, shared memory, golden.  Close the gap that
   `bus_address_headers` refuses a plain memory slave (give it base + span, no layout).
3. **HLS.**  Both kernels csynth (II=1 check); the bus writer in HLS reproduces the queue-in packet
   protocol the XSI host uses today -- **the main risk**, checked first in this stage.
4. **XSI.**  Three bus masters on the RTL crossbar (`AxiXbarConfig.from_crossbar` has only been
   exercised with one); the gate's exact cycle count; `WANT_XSI_GATES` + 1.
5. **Docs.**  `guide/interface/derived/credit_stream.md`: an "over the bus" section, and its summary's
   "if the producer can simply block, a plain StreamIF is better" gains the routed case (over a bus a
   block is a bus stall, so credits are needed even for a producer that may wait).  Slave views page:
   `CreditIn`.  The Markov example pages.

## Open

- `crd_every` default, and whether "drained to empty" is needed in the RTL or only shortens latency.
- Whether `MmStreamWriter` lives inside the kernel's HLS (an `m_axi` port) or as a separate RTL block
  beside it.  The HLS route is simpler to generate; a separate block keeps the kernel streams-only.
  Decide at Stage 3.

## Built

- **Stage 0.**  `CreditStreamMasterIF.write` (blocking; sleeps on the credit channel) and
  `CreditStreamSlaveIF.crd_every`.  **D5 changed:** the "flush when the queue drains" rule is gone --
  right in pysim (burst-granular), but at RTL a queue fed one word every few cycles is momentarily
  empty after nearly every read, and the flush would send a credit per word.  Liveness comes instead
  from `max_write = depth - resp_words - (crd_every - 1)`: a producer writing no more than that always
  gets its room.  `FramedCreditStreamMasterIF`: the forward boundary port gets a TLAST pin, which the
  routed writer needs to frame a write.
- **Stage 1.**  `waveflow/hw/mm_credit.py`: `MemSlaveCreditIn` (latest-value, a write never waits),
  `MmStreamWriter` (queue / credit modes), `MmCreditStreamIF` (binds the same endpoints; `place` after
  addresses).  Kernels declare `CreditIn` / `QueueIn` on their credit ports; `build_mm_device` joins the
  right half.  `MemSlaveWStream.nstall` counts packets that stalled the bus (gate: 0; a producer told
  the wrong depth is the negative control).
- **Stage 2.**  `examples/markov`.  **D3 changed in detail:** the chain is a composite (core + the
  framework's in-band `MemWStream`), so `x` goes out through an existing component and the response is
  forwarded only once `x` is stored -- found by asking what already writes a stream to memory (the
  search-first rule).  **D8 changed:** a writer's peer base is a stable input wire the system top drives,
  not a host-set register -- still not compiled into the kernel.  Plain memories join the bases header
  (`bus_memory = True`).
- **Two framework defects found on the way**, both silent at one lane per word: the numpy array fast
  path put one element per word (the serializer packs densely), and an `m_axi` array read charged one
  word per element (up to 8x the words).  Fixed, with `tests/hw/test_dense_array_fast_path.py`.
- **Stage 3.**  All four tops at II=1 inside 10 ns: `markov_gen` 9.92 ns (17.4 ns before admission and
  the first draw were split into two firings), the chain core 6.8 ns (10.3 ns before the empty-job
  test moved off the header read), the writers 7.3 ns.  A scalar `hls::task` argument csynths.
- **Stage 4.**  Four masters on a generated 4x3 crossbar (`id_width=2`); the shared memory is a BRAM view
  rather than a memory BFM -- the BFM slaves echo no AXI IDs, which a multi-master crossbar routes
  responses by.  The top is wired from the csynth'd modules' own port lists.  Gates
  (`tests/examples/test_markov_xsi.py`): bit-exact, no polls, **2356 cycles**.  `WANT_XSI_GATES` 145.
- **Rewrite (2026-10-04, branch markov-timing):** both bodies straight-line loops per job (the
  command-response pattern) -- 6.8 / 6.6 ns, RTL 2356 -> **2246** cycles.
- **Timing gap closed (branch markov-timing):** found with `markov_xsi` handshake probes -- two design
  defects (no FIFO in front of the store-and-forward queue writer: `fwd_depth`; a credit window smaller
  than the link's bandwidth-delay product: queue 64 -> 128) and two model gaps (each kernel's measured
  per-chunk overhead).  RTL 2246 -> **1865**, pysim 1926 (+3.3%, gated within 5%).  WANT_XSI_GATES 146.  Hypothesis, not yet probed: the RTL queue writer
  gathers a whole write before bursting, and nothing buffers the generator while it bursts.
- **Stage 5.**  `credit_stream.md` (over a shared bus; `write`, `max_write`, `crd_every`), the slave views
  page (credit in), `docs/examples/markov/index.md`.

