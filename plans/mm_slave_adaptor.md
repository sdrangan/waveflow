# Plan: the memory-mapped slave adaptor — registers, queues and BRAM behind one AXI slave

> **Status (2026-10-01): proposed, nothing built.** Decisions D1 to D6 are the recommendations from
> the design discussion. Stage 0 (the crossbar witness) gates everything else. The `mm_fir` example
> can proceed in parallel from Stage 1 on, in pysim.

## Motivation

A kernel today can be a bus **master**: `MemRStream` / `MemWStream` own an `m_axi` port and turn a
command stream into AXI bursts. A kernel cannot be a bus **slave** in any useful way:

- Vitis HLS generates `m_axi` (master) and `s_axilite` (a register file), but **no AXI4-full slave**.
- `s_axilite` is unusable for a free-running kernel's input. The kernel cannot see a write strobe, so
  writing the same value twice is invisible, and there is no `ap_start` to sample on.
- `MMIFSlave` says so itself: it is "never a *kernel* boundary port", and `_boundary_port` refuses to
  lower it (`waveflow/hw/memif.py`, `MMIFSlave.boundary_kind`).

So another master (the host, or a second kernel through `MemWStream`) has no way to hand a
free-running kernel a queue, a set of registers or a memory window through the address map. The only
memory-mapped queue we have, `AXIMMQueue`, is a DRAM ring that both sides reach as *masters*, with the
consumer polling.

In pysim the crossbar already exists (`AXIMMCrossBarIF`), but **nothing synthesizes one**: no code in
`waveflow/` or `examples/` creates a crossbar, SmartConnect or block design. `plans/board_packaging.md`
plans a SmartConnect inside a block design (`BdGenStep`); it is not built.

## The rule

**Kernels stay stream-based `FreeRunMod`s. Every synchronization a kernel sees is a stream message.**
An adaptor outside the Vitis top translates bus semantics into those messages.

A kernel facing raw registers, a raw FIFO and a raw BRAM at once has no defined synchronization
between them. A stream message has one: it arrives, in order, once. The adaptor's job is to make each
memory-mapped access into such a message, with a meaning stated per view.

BRAM is the one place data does not travel as messages (see *View semantics*). Its **synchronization**
still does.

## Architecture

```
 AXI masters: host, MemWStream, MemRStream
        │
        ▼
 axi_crossbar (AMD IP)
        │  AXI4-full
        ▼
 ┌─────────────────────── adaptor (outside the Vitis top) ───────────────────────┐
 │ axi_slave_front ──in-order req bus──▶ generated decoder                       │
 │                                          ├─▶ mm_regbank   ──cfg msg / status──┼──┐
 │                                          ├─▶ mm_queue_in  ──axis─────────────▶┼──┤
 │                                          ├─▶ mm_queue_out ◀─axis──────────────┼──┤
 │                                          └─▶ mm_bram_port ──▶ bram_t2p ◀─bram─┼──┤
 └───────────────────────────────────────────────────────────────────────────────┘  │
                                                                                    ▼
                                                 kernel: FreeRunMod in the Vitis top
                                                 (streams + bram port + lock stream)
```

Three layers:

1. **The crossbar.** AMD's `axi_crossbar` IP (decision D1), configured from the Python address map.
2. **The adaptor.** One AXI4-full slave front end, an in-order internal request bus, a generated
   address decoder, and one hand-written leaf per view.
3. **The kernel.** Unchanged in kind: a `FreeRunMod` whose boundary is streams, plus `bram` ports
   where a BRAM view is used.

The **master side needs no new RTL.** `MemRStream` / `MemWStream` already make the kernel side a
command stream and generate a correct `m_axi` port. Several of them may share one `m_axi` bundle.
`MMIFMaster` remains the endpoint *type*: `MemWStream.m_mem` is an `MMIFWriteMaster`, and the host
process in pysim holds an `MMIFMaster` directly. **Kernels never hold one directly.**

## View semantics

Each view is a typed stream protocol on the kernel side.

| View | Bus side | Kernel side | Builds on |
|---|---|---|---|
| **Queue in** | burst writes anywhere in the window push words | `StreamIFSlave`, TLAST from the in-band length header | `MemWStream` (the producer side) |
| **Queue out** | reads anywhere in the window pop words, after an occupancy check | `StreamIFMaster` | mirror of the above |
| **Config registers** | write fields, then write `COMMIT` | **one message carrying the whole bank** (a `DataSchema`) per commit | `RegMap` layout, `RegMapMMIFSlave` |
| **Status registers** | reads, with no side effects | the kernel *pushes* status messages; the adaptor holds the latest | — |
| **Doorbell** | one word write | one command message | `MRCmd` / `MWCmd`-style schemas |
| **BRAM window** | read/write an address range | a `bram` port **plus** an ownership (lock) stream | `T2pBram`, `LockedT2pMemIF` |

### Queue windows

- **A queue occupies an address window, not one address.** HLS `m_axi` issues only INCR bursts, so
  the address advances every beat. Any write inside the window is a push. A producer issues every
  command at offset 0 and splits long transfers into commands no larger than the window. AMD's
  `axi_fifo_mm_s` decodes its data port the same way.
- **Framing is in-band:** each packet is `[len | data…]`. The leaf counts words and asserts TLAST
  itself, so burst splitting by the interconnect cannot move a packet boundary. (A length register
  would need a second write to a second address, ordered against the data. That is avoidable.)
- **Full FIFO = WREADY low.** pysim models the stall faithfully: `rx_write_proc` runs while the
  crossbar holds the slave's `write_channel`, and blocks on `m_out.write`. A producer whose `m_axi`
  port carries other traffic must not start a burst that cannot drain. It reads the vacancy status
  register or holds credits (`CreditStreamIF` is the model). The protocol requires this. The stall is
  a hazard to model, not something to design around.
- **Queue-out never blocks RVALID on an empty FIFO.** A read of an empty queue would hang the bus.
  The reader checks occupancy first. Reads that pop are confined to this view, never a register.
- **Accepted ≠ consumed.** The AXI B response (and `MemWStream`'s `emit_done`) means the adaptor took
  the words, not that the kernel consumed them. A producer that needs consumption gets a separate
  acknowledgement message.
- **Full words only.** A partial WSTRB or a narrow burst is an error (SLVERR). A stream cannot carry a
  partial word.

### Register banks

- **Config is shadow-and-commit.** Field writes land in a shadow bank. A write to `COMMIT` snapshots
  the bank and sends it as one message. Streaming single register writes would show the kernel a
  half-updated configuration. This is the contract `ap_start` gives a `HostActivated` kernel, made
  explicit for a free-running one.
- **Status is latest-value.** The kernel pushes status messages at whatever rate it likes. The leaf
  keeps the most recent and serves reads from it. Reads have no side effects, so a debugger read is
  harmless.

### BRAM windows

The memory lives beside the kernel as `bram_t2p`, exactly as today. One port faces the bus through
`mm_bram_port`; the other is the kernel's `bram` port. Ownership travels as stream messages
(`LockedT2pMemIF`'s claim/release). Data does not, because moving bulk data as messages would waste
the memory's random access.

## Ordering

Two guarantees, stated separately because they are different.

1. **The adaptor commits writes in arrival order across all views.** A single in-order request bus
   behind `axi_slave_front` gives this. It is what makes *write a BRAM window, then ring a doorbell*
   correct: the BRAM write has reached the memory before the doorbell message exists. **Gated** in
   Stage 3.
2. **Order across two kernel-side streams is NOT preserved.** Once a commit message and data words
   travel on separate streams, the kernel can read them in either order. Every protocol that needs
   cross-view order must state it in the messages. The `mm_fir` example states it as
   `apply_at_sample` (decision D5).

## Realization: hand-written leaves, generated wiring

The adaptor cannot be an HLS task (no AXI4-full slave), so it sits outside the Vitis top like
`T2pBram`. `rtl_module()` is "declared, never generated" (`waveflow/hw/hw_module.py`), and an adaptor
whose address map varies per kernel looks like a generator. The split keeps the principle:

- **Fixed Verilog leaves, each an `rtl_module()`**, each verified once:
  - `axi_slave_front`: AXI4-full slave → simple request bus (`addr, wdata, we, rdata, valid/ready`).
    Handles bursts, WSTRB rejection, and B/R responses.
  - `mm_queue_in`, `mm_queue_out`, `mm_regbank`, `mm_bram_port`.
  - Widths and depths ride on Verilog parameters at instantiation. The files are never rewritten.
- **Generated wiring**: the address decoder (a table) plus the leaf instances. These are emitted by the
  path that already joins kernel ports to `bram_t2p` (`add_rtl_if` → `waveflow/build/wrapper_gen.py`).
  The decoder is the only generated logic. It contains no datapath.

**One address map, from Python.** The adaptor's views are laid out by `RegMap`'s first-fit rules. The
same declaration produces:

- the pysim dispatch;
- the generated Verilog decoder;
- the crossbar's `CONFIG.M0x_A00_BASE_ADDR` properties (for XSI);
- `assign_bd_address` (for the board, later);
- the host driver header.

## The crossbar

Decision D1: **AMD's `axi_crossbar` IP for both XSI and the board.**

The crossbar is a contended resource. If XSI and the board run different crossbars, the measured
cycle counts stop transferring, and predicting the hardware is the point of XSI. The same IP can be
placed in the block design where `BdGenStep` would otherwise put SmartConnect. SmartConnect stays only
where it cannot be avoided, at the PS-to-fabric bridge.

Considered and not chosen:

- **SmartConnect in a block design for XSI.** Needs a block design project per configuration and
  simulates slowly.
- **verilog-axi `axi_crossbar`** (open source, plain Verilog, fits `rtl_module()` directly). Easiest
  to integrate, but it is not what the board runs. **Fallback for XSI only** if Stage 0 shows the IP
  step is too heavy. In that case the board's numbers must be measured separately, and the plan says
  so where it reports them.

**Unverified:** xsim ships precompiled XPM and AMD IP simulation models, so XSI should load the
crossbar once its simulation sources are generated. That has not been run here. Stage 0 exists to
run it.

## Decisions

- **D1 — Crossbar:** AMD `axi_crossbar` IP, in XSI and on the board. Fallback: verilog-axi for XSI
  only.
- **D2 — Queue framing:** in-band length header, TLAST generated by the leaf. Not a length register,
  not the burst boundary.
- **D3 — Back-pressure:** WREADY low on a full FIFO, modeled in pysim. Producers that share an `m_axi`
  port check vacancy or hold credits.
- **D4 — Registers:** shadow-and-commit for config (one bank message per commit), latest-value for
  status, no read side effects outside queue-out.
- **D5 — Cross-view order:** protocols carry it in the messages (for `mm_fir`, `apply_at_sample`). The
  adaptor guarantees only arrival order at its own request bus.
- **D6 — Realization:** fixed Verilog leaves as `rtl_module()`s, generated decoder and instances via
  `wrapper_gen`. No HLS task.

## Stages

Each stage before Stage 4 has **one** view kind, so Stage 4 is the only place cross-view ordering can
break.

### Stage 0 — crossbar witness

- A small script: `create_ip -name axi_crossbar`, set NUM_SI / NUM_MI / base addresses, then
  `generate_target simulation`.
- One AXI master BFM writes and reads one word through it into a trivial slave, under XSI.
- **New BFM:** an AXI *master* BFM. Today's BFMs drive streams and act as the memory behind an `m_axi`
  port. None issues AXI transactions as a master. The host in every later stage needs it.
- **Done when:** the transaction completes in XSI and the crossbar's latency is measured, or the IP
  route is shown impractical and D1 falls back.

### Stage 1 — queue in and queue out

- RTL: `axi_slave_front`, `mm_queue_in`, `mm_queue_out`.
- pysim: `MemSlaveWStream` / `MemSlaveRStream` (working names). Each is a `FreeRunMod` owning one
  `MMIFSlave` whose `rx_write_proc` / `rx_read_proc` push to or pop from a stream. No `run_iter`: it
  is purely reactive.
- Before relying on the stall: confirm `StreamIFMaster.write` blocks on a full stream on `main` (the
  pysim burst back-pressure arc changed this). Otherwise pysim absorbs a burst the hardware stalls.
- Witness: the Producer → `MemWStream` → crossbar → `mm_queue_in` → Consumer chain, and `mm_fir`
  rung 1.
- **Done when:** bit-exact data and the XSI cycle count matches pysim's prediction, including one run
  with a full FIFO.

### Stage 2 — register bank

- RTL: `mm_regbank` (shadow, commit, status). pysim builds on `RegMapMMIFSlave`.
- The commit message type is the bank's `DataSchema`, so the kernel reads it with `get_schema`.
- Witness: `mm_fir` rung 2 (tap switch at `apply_at_sample`).
- **Done when:** output is bit-exact across a mid-stream tap switch in pysim and XSI.

### Stage 3 — BRAM window

- RTL: `mm_bram_port`, joined to one port of `bram_t2p`. Ownership through the lock stream.
- **Ordering gate:** in XSI, a doorbell is never seen before the BRAM write that preceded it. Check
  this from the VCD (XSI discards `$display`), paired with a deliberately broken run that must fail.
- Witness: `mm_fir` with a long tap table in BRAM plus a doorbell.

### Stage 4 — generated multi-view address map

- `wrapper_gen` emits the decoder and instances for any set of views.
- The host header and the crossbar config come from the same layout.
- Witness: `mm_fir` with all of its views behind one slave port.
- **Done when:** the Stage 1–3 gates still pass with every view behind a single adaptor.

## Witness example: `examples/mm_fir`

Developed in parallel. Its pysim half needs only `AXIMMCrossBarIF` plus the Python adaptor model, so
it exists before the RTL does, and its pysim numbers are the predictions XSI must match.

- **Kernel:** `FreeRunMod` with
  - `s_cfg`: a `DataSchema` with `ntaps`, `coeffs[16]` int16 and `apply_at_sample`;
  - `s_in`: int16 samples;
  - `m_out`: int32 results.
- **Views:** register bank (cfg + commit; status = samples processed), `queue_in` for samples,
  `queue_out` for results.
- **Host (pysim):** an `MMIFMaster` on `AXIMMCrossBarIF`. It writes taps, commits, pushes samples in
  bursts with length headers, and reads results once status says they are ready.
- **Golden:** numpy `lfilter`, switching taps at `apply_at_sample`.
- **Rungs:**
  1. queue in and queue out, fixed taps (Stage 1);
  2. register bank and a mid-stream tap switch (Stage 2);
  3. bit-exact output and cycle counts in XSI;
  4. BRAM tap table plus doorbell (Stage 3);
  5. stretch: results go through `MemWStream` into a second kernel's `queue_in` (the Producer →
     Consumer case as the same example's second rung).

## `AXIMMQueue`: keep until VMAC is migrated

`AXIMMQueue` is a different mechanism (a DRAM ring with pointers in memory, both sides masters,
consumer polling), not an earlier version of this one.

| | `AXIMMQueue` | adaptor `queue_in` |
|---|---|---|
| Depth | as much DRAM as given | on-chip only |
| Consumer | polls memory: bus cost plus discovery latency | stalled by back-pressure |
| New RTL | none | adaptor + crossbar |
| Software peer | natural (shared memory) | through the windows |

For kernel-to-kernel and host-to-kernel command queues the adaptor is better, and VMAC (due to be
redone) should move to it. Decide on retirement after that migration. The DRAM ring still has a case:
a queue deeper than on-chip memory, or a software consumer. Retirement would take the most code with
it: `poll_until`, `aximm_queue_impl.tpp` / `poll_until_impl.tpp`, and part of the condition IR exist
mainly for it. 32 tracked files reference it today.

## Not in scope

- An AXI-Lite-only variant. AXI4-full covers it.
- Generating any leaf's datapath from Python. The leaves are hand-written and verified, like
  `bram_t2p.v`.
- The board block design. `plans/board_packaging.md` owns it. This plan only requires that it use the
  same crossbar IP and the same address map.
- Clock-domain crossing. Everything is one clock until a stage needs otherwise.

## Open questions

- Does the generated `axi_crossbar` simulate under XSI without a Vivado project? (Stage 0 answers
  this.)
- Window size for queues: a fixed 4 KB, or a per-view parameter?
- Should status pushes be rate-limited by the kernel, or should the leaf simply overwrite? (Overwrite
  is the current assumption.)
- `MemSlaveWStream` / `MemSlaveRStream` as names, or `MmQueueIn` / `MmQueueOut` to match the leaves?
