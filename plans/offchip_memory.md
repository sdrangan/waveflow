# Plan: off-chip memory in a system -- simulated at RTL, never synthesized

**Status:** drafted 2026-10-09.  Follows `plans/system_dag.md` (merged in PR #242), whose system top
refuses a memory outside the cut ("an off-chip memory (a FlatMemory beside the top) is not wired by
system_top yet").  This plan wires it: a memory such as the PS's DDR takes part in the XSI simulation
the way the software host does -- beside the top, as a C++ model -- and is never synthesized.

## Motivation

A real accelerator's kernels read and write DRAM: on a Zynq, the PS's DDR, reached from the PL through
an AXI port of the PS.  Today a system top can hold only **on-chip** memory -- a `MemoryMod` inside the
cut becomes a BRAM window behind a framework front (markov's shared memory).  A memory on the crossbar
but outside the cut is refused, so a design whose data lives in DRAM cannot run at RTL as a system.

The host already shows the shape of the answer.  It is a pysim object that **participates in the XSI
simulation but is not hardware**: its bus master becomes a top port (`s0_axi`), and the harness binds
its C++ twin to that port.  Off-chip memory is the mirror image:

| | software host (done) | off-chip memory (this plan) |
|---|---|---|
| in pysim | `SwHost`, on a crossbar **master** port | `MemoryMod`, on a crossbar **slave** port, outside the cut |
| in the top | its master becomes a top AXI4 *slave* port, `s0_axi` | its MI slot becomes a top AXI4 *master* port, `m<k>_axi` |
| in the harness | its C++ twin, bound to `s0_axi` | a `FlatMemory` and its read / write slave BFMs, bound to `m<k>_axi` |
| synthesized | no | no |
| on a board | the PS's CPU | the PS's DDR, through a PS slave port (HP / ACP) |

The last row is the reason to do it this way and not with a BRAM stand-in: the top port is the
**hardware boundary** a board build connects to the PS (`plans/board_packaging.md`, the Vivado IPI
system flow), so the cut that makes the testbench also makes the integration point.

## What exists

* **pysim:** `MemoryMod` (`waveflow/hw/memory.py`) on a crossbar slave port, with an access-latency
  model (`latency_init + nwords * latency_per_word` cycles on the `s_mm` path) that composes with the
  crossbar's bus latency, and `load_segs` / `dump_segs` (`DynParam`s) for seeding and dumping regions.
* **XSI:** `FlatMemory` (`waveflow/build/xsi/xsi_bfm.h`) -- a word arena with file-backed
  `load_segs` / `dump_segs` -- and `AxiMmReadSlave` / `AxiMmWriteSlave`, which serve a kernel's `m_axi`
  bundles out of it.  `MemoryMod.bfm_model()` already names `FlatMemory` (`shared="mem"`), and the
  single-kernel harnesses (`composite_gen.tb_top_spec`: mem_copy, mem_r_stream, mem_w_stream) bind it.
* **The refusal:** `system_top_spec` raises on a `MemoryMod` slave outside the cut
  (`waveflow/build/system_top.py`, the MI loop), and `system_xsi.discover` puts **every** memory on
  the crossbar into the default cut.

## What is missing

1. **A declaration.**  Nothing says a memory is off-chip.  `MemoryMod.inline` looks like it ("True =
   local BRAM; False = external DDR") but means something else in practice -- whether the owner
   allocates regions with `alloc()` / `free()` -- and markov's memory is `inline=False` yet is, and should
   stay, a BRAM window.  So `inline` must not be overloaded (see D1).
2. **The top port.**  An MI slot whose slave is outside the cut must leave the top as an AXI4 master
   port group, IDs included.
3. **The harness binding.**  `system_tb_spec` binds a participant's bus master (to `s<k>_axi`) and its
   interrupt sinks (to `irq_<view>`); it must also bind a memory's `s_mm` slave to `m<k>_axi`.
4. **BFMs fit for a crossbar.**  `AxiMmReadSlave` / `AxiMmWriteSlave` were built for one kernel's
   bundle and fall short behind a multi-master crossbar:
   * **no AXI IDs.**  AMD's crossbar routes each response back to its master by ID; a slave that does
     not echo `ARID` -> `RID` and `AWID` -> `BID` misroutes it.  (This is why markov's memory sits behind
     a framework front today: the front echoes IDs.)
   * **absolute addresses.**  `FlatMemory::word_index(byte_addr)` assumes the arena starts at 0; behind
     the crossbar the address is the bus address, so the slave needs the window's base.
   * **no latency.**  Every access is served at once, while pysim's `MemoryMod` charges
     `latency_init` / `latency_per_word`.  With non-zero latencies the two would disagree, and no cycle
     gate could be calibrated against DRAM.
   * one transaction at a time per channel.  Legal (it back-pressures), and kept for now.
5. **The arena's size.**  `FlatMemory` allocates `nwords_tot` words.  Fine for a test window (MBs); a
   DDR-sized declaration (hundreds of MB) is not.  The arena should be sized by the crossbar window the
   memory is mapped at, or be sparse.

## Decisions

* **D1 -- the declaration is a new field, `MemoryMod.offchip: bool = False`**, not `inline`.  An
  off-chip memory is left out of `discover`'s default cut; `inside=` still overrides either way (the
  cut stays the one source of truth for what is synthesized).  Default False keeps every existing
  system -- markov included -- exactly as it is.
* **D2 -- the top port is `m<k>_axi`**, `<k>` the MI slot, with the crossbar's MI signal set (IDs at
  the crossbar's ID width).  The spec answers it: `MiSlot("external", name)` and `spec.external`
  (`(port prefix, memory name, base, size)` per off-chip memory), as `spec.si` answers the host's ports.
* **D3 -- the memory is a harness participant**, like the host: `system_tb_spec(spec, xbar,
  participants)` binds a `MemoryMod`'s `s_mm` to the port its MI slot became.  Its model is the
  existing `MemoryMod.bfm_model()` (`FlatMemory`) plus one read and one write slave on the same port
  prefix; `discover` returns the off-chip memories so `add_system_steps` passes them in.
* **D4 -- latency parity is a field pair, defined once.**  The BFMs take `latency_init` and
  `latency_per_word` from the `MemoryMod` (cycles; `latency_init` before the first R beat or the B
  response, `latency_per_word` between beats), so pysim and RTL charge the same.  Both default to 0,
  so the existing gates (mem_copy 2908, mem_r_stream 158, mem_w_stream 176) do not move, and the
  calibrated platform `zynq7020_bfm_100mhz` -- fitted to zero-latency BFMs -- stays valid.  Real DDR
  numbers are a calibration question, not this plan's (Open questions).
* **D5 -- no change to the DAG's shape.**  An off-chip memory is not an HLS top (not in
  `spec.modules`, so not in csynth) and has no RTL (not in `rtl.json`); only `system_top` (the port)
  and `harness` (the binding) change.

## Stages

Each stage keeps `pytest -m "not vitis and not xsi"` green; stages that touch the BFMs or the harness
also run the `-m xsi` gates named in their gate.  **The gates are the oracle**: markov 1870, mm_fir
618 / 611, mem_copy 2908, mem_r_stream 158, mem_w_stream 176 must not move.

**Stage 1 -- the declaration.**  `MemoryMod.offchip`; `discover` leaves off-chip memories out of the
default cut and returns them; `system_top_spec` still refuses them (the next stage removes that), with
the refusal's message naming the field.  Fast tests: markov's cut unchanged; an `offchip=True` memory
is outside it.

**Stage 2 -- the BFMs.**  `AxiMmReadSlave` / `AxiMmWriteSlave`: echo `ARID` / `AWID` when the port has
them (as `AxiMmMaster` already drives them); a `base` (byte) subtracted from every address; the two
latencies.  `FlatMemory` sized from a given word count (the window), not only `nwords_tot`.  Gate:
`-m xsi` for mem_copy, mem_r_stream, mem_w_stream (defaults: unchanged counts), plus a new BFM test --
a two-master crossbar in front of one `FlatMemory`, interleaved reads and writes from both masters,
data and IDs checked, and a latency setting that moves the count by exactly the cycles it should.

**Stage 3 -- the top port.**  `MiSlot("external")`, `spec.external`, and `render_system_top` emitting
`m<k>_axi` (with the crossbar's ID width).  Fast tests on the spec and the text: a system with an
off-chip memory renders, the port group is complete, a probe on its channels resolves (`beat` on the
crossbar master port's `AR`, say).

**Stage 4 -- the harness.**  `system_tb_spec` binds a memory participant to its `m<k>_axi`;
`system_xsi`'s harness step passes the off-chip memories; `run_system_xsi` and `add_system_steps`
need no new arguments (the memories come from `discover`).  `FlatMemory`'s `base` and size come from
the memory's crossbar window.  Fast test: the generated harness for a system with an off-chip memory
is well-formed C++ (syntax-only, as `test_sw_host_gen.py` does for the host).

**Stage 5 -- the worked example: markov with its memory off-chip.**  `MarkovSystem(..., mem="ddr")`
(the memory `offchip=True`, same base, same size), added to `markov_build`'s DAG as a second system
with `prefix="ddr_"` -- sharing `codegen` and `csynth` with the on-chip one, as mm_fir's two topologies
do; the same kernels, so no new csynth.  Gates (a new gate test, or a parametrized markov gate):
bit-exact, the host never polls, traces identical to pysim's, and the RTL cycle count **recorded**
(a new `EXPECTED_CYCLES` entry; the on-chip 1870 must not move).  With zero memory latency the count
should be near 1870 -- the framework front's latency against the BFM's none -- and the difference is
to be explained, not just recorded.  Then a non-zero `latency_init` on both sides, and pysim within 5%
of RTL at it.  `WANT_XSI_GATES` grows by the gates added (recorded here).

**Stage 6 -- memory contents as conformance (optional).**  Dump the memory's regions after both runs
(`dump_segs`: the C++ `FlatMemory` already dumps; `MemoryMod` would dump in pysim's `post_sim` too)
and compare them in the `compare` step, byte for byte, as the host's endpoint traces are.  Catches a
kernel write the host never reads back.

**Stage 7 -- docs.**  `docs/guide/build/xsi_system.md` (off-chip memory: the declaration, the port, the
binding, latency parity; drop the "not wired yet" bullet); the markov Synthesis and XSI testbench pages
(Fig. 2 with the memory outside the box as `m2_axi`; Fig. 3 with `FlatMemory` beside the host); the
`MemoryMod` docstring (`offchip` vs `inline`).  Name steps and link them; never "Step N".

## Open questions

* **Realistic DDR timing.**  This plan gives pysim and RTL the *same* latency; it does not say what a
  PS DDR's latency is.  Measuring it -- a calibration run on a board (`plans/rfsoc_4x2_bringup.md`) or
  vendor numbers for the HP port -- and fitting `latency_init` / `latency_per_word` is calibration work,
  in the style of the existing two-level calibration.
* **The PS's topology.**  On a Zynq the CPU reaches DDR directly, not through the PL crossbar, and the
  PL reaches it through HP ports that are themselves crossbars with their own arbitration.  This plan
  hangs host and memory off one crossbar, which is right for simulating the PL's view; a system that
  models the CPU's own path to DDR (a host bound to the memory without the crossbar) is a later step.
* **Outstanding transactions and reordering.**  One transaction per channel at a time is legal and
  conservative; a DDR controller keeps many in flight and may reorder across IDs.  Add when a design's
  cycle count depends on it (a gate will show it as a pysim / RTL disagreement).
* **Contention.**  `MemoryMod.bfm_model`'s docstring already notes that the XSI slaves are un-arbitrated
  while pysim's crossbar models contention.  Behind the real AMD crossbar the RTL now arbitrates too;
  whether pysim's model then matches is the Stage 5 measurement.
* **Board flow.**  Mapping `m<k>_axi` onto a PS slave port in the IPI flow belongs to
  `plans/board_packaging.md`; this plan only makes the port exist.

## Progress log

(empty)
