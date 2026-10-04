# Plan: the rest of "no polling in the examples" -- ap_done as an interrupt, and kernel-to-kernel

> **Status (2026-10-03): Part A BUILT** (branch `ap-done-irq`); Part B waits on the user's choice of
> example.  Follows `plans/mm_irq.md` (built: `IrqIF`, queue interrupts, mm_fir with no polling).
> Departures from Part A are in **Built (Part A)** at the end.

## What still polls

| where | how | used by |
|---|---|---|
| `BoundRegMap.poll_end` (`waveflow/hw/regmap.py`) | reads a register-map field until it reaches a value (`ap_done`) | the host-activated examples' hosts: `regmap` (`simp_fun.py`), `shared_mem` (`hist.py`), `stream_inband` (`poly.py`), `interface/regmap_demo.py` |
| `examples/interface/poll_demo.py` | `MMIFMaster.poll_until` | a demo **of the polling cost model** -- about polling, not an example that teaches it; keep, and say so in its docstring |

## Part A -- `ap_done` as an interrupt

A Vitis kernel with `ap_ctrl_hs` and an `s_axilite` control port **already has** an interrupt: the
`interrupt` output pin, gated by the `GIER` (global enable, `0x04`), `IER` (per-source enable, `0x08`:
bit 0 = `ap_done`, bit 1 = `ap_ready`) and `ISR` (status, `0x0C`, toggle-on-write to clear) registers
that `VitisRegMap` already lays out -- as storage only, today.

1. **`VitisRegMap` drives an `IrqIFSource`** (`m_irq` on the host-activated module):
   `interrupt = GIER && (ISR & IER) != 0`; `ISR` bits set on `ap_done` / `ap_ready` when enabled;
   `ISR` toggle-on-write.  Exactly the Vitis behavior, so the generated RTL and the pysim twin agree.
2. **`BoundRegMap.wait_done(irq)`** -- the host's end of the line: enable once (`GIER`, `IER`), then per
   launch: `start()`, `irq.wait_high()`, clear `ISR`, read the outputs.  No `ap_done` reads.
   `poll_end` stays, documented as the fallback.
3. **XSI:** the Vitis-generated `interrupt` port is already on the RTL; the testbench host samples it
   (`IrqPin`), as mm_fir does.  Check each host-activated example's XSI gate keeps its cycle count when
   its host switches (it will not, since the polls go -- re-record, with the before/after).
4. **Examples:** `regmap`, `shared_mem`, `stream_inband` and `regmap_demo` hosts use `wait_done`.  Docs:
   the regmap page's "poll `ap_done` (or wait for an interrupt)" becomes "wait for the interrupt".
5. **Gates:** a host-activated launch issues no `ap_done` read (counted, as mm_fir's); `ISR` clear and
   re-arm across two launches; the `IER` mask (an `ap_ready`-only enable does not fire on `ap_done`).

Open: whether `ap_ready` gets its own line or shares `interrupt` (Vitis: one pin, `ISR` says which).
Follow Vitis: one pin.

## Part B -- the two-kernel command/response example

Two free-running kernels talk over the bus with **no polling and no host in the loop**:

```text
 kernel A (requester)                                     kernel B (server)
   m_cmd ─▶ MemWStream ─AXI─▶ crossbar ─▶ B's adaptor: queue in  ─▶ s_cmd
   s_resp ◀── A's adaptor: queue in ◀─ crossbar ◀─AXI─ MemWStream ◀─ m_resp
```

- Each kernel is a bus **master** (a `MemWStream` writing `[len | message]` into the other's queue-in
  window) and a **slave** (its own adaptor's queue in, which it reads as a stream -- an event wait).
- Commands carry `tx_id`; responses echo it (mm_fir's `FirRespHdr` pattern).
- **Flow control by credits, not by asking:** A may have at most `C` commands outstanding, where `C`
  fits B's queue; each response returns one credit.  A never reads B's free space, and B's queue can
  never fill, so `MemWStream`'s write can never stall the bus (it would, on a full queue in).
- Responses go into A's queue the same way, sized for `C` responses.
- B's service: something small and checkable (a running sum, or mm_fir's filter as a service), so the
  example is about the pattern.

Pieces that exist: a synthesized `MemWStream` writing an adaptor's queue in is gated at RTL
(`test_mm_queue_memw_xsi`).  New: two kernels each with an `m_axi` port and an adaptor, a 2-master
crossbar, the credit protocol, and a host only to start and check (config via a register bank).

Decisions for the user: what B computes; `C` and the queue depths; whether credits are explicit
(a credit stream) or implicit (count responses).  Recommendation: implicit -- a response *is* the
credit, which is the point of the pattern.

## Order

A first is smaller and retires the last polling hosts; B is a new example built on `mm_irq` and the
credit rule.

## Built (Part A)

- **The survey was wider than the polling.**  Only two hosts actually polled or guessed:
  `simp_fun.py` (`poll_end`) and `regmap_demo.py` (a fixed `timeout` after launch).  `hist.py` and
  `poly.py` already wait on their response streams and read status once; no C++ / XSI testbench reads
  `ap_done` (C-sim and cosim call the kernel directly), so step 3 had nothing to re-record.
- **The line lives on the slave, not the module:** `VitisRegMapMMIFSlave.interrupt()` returns the
  `IrqIFSource` (made on first use), so any `VitisRegMap` kernel -- `HwModule` or raw `SimObj` -- has it.
  `gier` / `ier` re-evaluate the line on write; `isr` is toggle-on-write; at `on_start`'s return
  `isr |= ier & 3`.  Mirrors `simp_fun_control_s_axi.v`.
- **Host:** `BoundRegMap.enable_irq()` (`ier = 1`, `gier = 1`), `wait_done(irq)` (wait high, write 1 to
  `isr`; raises without `enable_irq`), and `run(irq)` = enable once + `start` + `wait_done` -- the call
  the examples make.  `poll_end` stays, documented as a debugging fallback.
- **The `IER` mask gate is weaker than planned:** `ap_ready` and `ap_done` coincide in the model (both
  at `on_start`'s return), so "`ap_ready`-only does not fire on `ap_done`" cannot be told apart.  Gated
  instead: no reads during `run` (counted), two launches = two rises (re-arm), `gier` gates the line
  but not `isr`, `ier = 0` sets no status.
- simp_fun's trace: `host_done` moves from 60 ns (second poll) to 40 ns -- the instant the kernel
  finishes.

