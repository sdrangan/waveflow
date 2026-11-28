# Plan: a full-rate, timing-closed read port for a BRAM window on the bus

**Status: READY for the lab machine, 2026-11-28.** Written in the cloud planning session for
`docs/examples/aoa_detect` (its [timing page](../docs/examples/aoa_detect/timing.md), *The ring's read
port*). Nothing here is built yet.

**For the agent picking this up:** read this whole file, then the files of *Read first*. Work on a new
branch from `origin/main`, commit after each stage, push, and do not merge or open a PR unless asked.
Run every Waveflow, pytest or Vitis command inside the project's venv (`CLAUDE.local.md`). Write the
tool version, the part and the clock next to every measured number (Vitis / Vivado / xsim 2025.1,
`xczu48dr`, 250 MHz unless a step says otherwise). When something fails, test at most two hypotheses on
the root cause, then stop and report; never loosen a test's tolerance to make it pass.

## Why

The emitter detection example keeps its samples in a ring held in a true dual-port BRAM: a capture
kernel writes it on port B, and every downstream job reads one channel's block over the AXI crossbar on
port A, through the BRAM window's bus leaf. At the proposed sizes a job reads 1,024 words of 128 bits,
and about 1.5 jobs arrive every 2,083-cycle block.

**Today that read runs at half rate.** `waveflow/build/rtl/mm_bram_port.v` registers the memory's output
once more before answering, so a read costs `LAT + 1 = 2` cycles a word, and `axi_slave_front.v` keeps
at most one read request outstanding, so nothing overlaps the two cycles. One block's read is then
about 2,180 cycles: a whole block period, and one read port is about 150% busy. With one word a cycle it
is about 1,100 cycles and the port about 80% busy.

**What exists is not enough.** `mm_bram_port_p1.v` already reads a word a cycle, but by taking the BRAM's
output register straight to the AXI read data: its own header says it is *for a memory that is
simulated, not synthesized*, because a timing-closed design would register that path. It is selected by
`PipelinedMemoryMod` / `BramViewP1` and used in `examples/ltbf_cg_mem_dse`. Registering the response
adds a cycle of latency; with one request outstanding that is back to two cycles a word. A
synthesizable full-rate port therefore needs **both**:

1. **a leaf with a registered response and room for the reads in flight**, and
2. **a front that keeps more than one read request outstanding**, in order.

## What to build

### 1. The front: `RD_OUTST` outstanding reads (`waveflow/build/rtl/axi_slave_front.v`)

- A parameter `RD_OUTST` (default **1**: today's behaviour, and every existing generated adaptor's RTL
  **byte-identical**). At `RD_OUTST = N`, the front issues a read beat's request whenever fewer than `N`
  are outstanding and the leaf is ready, and counts responses back. Still one AXI transaction in service
  at a time, so the plan's ordering guarantee 1 (`plans/mm_slave_adaptor.md`) holds unchanged.
- **Order across leaves.** Responses must come back in request order. The adaptor's decoder (generated
  by `waveflow/build/mm_adaptor_gen.py`) muxes the leaves' responses: at `RD_OUTST > 1` it keeps a FIFO of
  the leaf each outstanding request went to (depth `RD_OUTST`) and takes responses from that leaf only.
  Decide in Stage 1 whether that FIFO lives in the decoder or the front, and say why.
- Refused reads (SLVERR beats synthesized in the front) must stay correct with requests outstanding.

### 2. The leaf: `mm_bram_port_pipe.v` (new, beside `mm_bram_port.v`)

- Port A of `bram_t2p` (`READ_LATENCY` 1), the same request-bus interface as `mm_bram_port.v`.
- **A registered response**: the BRAM's output register, then one response register, so the path from
  the memory to the AXI read data starts at a flop. Total read latency `LAT + 1 = 2` cycles, **one read
  accepted a cycle**.
- **A response FIFO** sized for what is in flight (`LAT + 1`, plus whatever the front's `RD_OUTST`
  needs; size it from the cycle diagram and say so in the header), so that `rsp_ready` low never loses a
  word: `req_ready` for reads is low when the FIFO could not take every read in flight.
- Writes as today: done at the request handshake.
- Out-of-memory reads answer 0 with `rsp_err`, as `mm_bram_port.v` does.
- `LAT` other than 1 refused at elaboration, as `mm_bram_port_p1.v` does.
- A header in the style of the other leaves: what it is, the cycle diagram of a burst, why each
  register is there.

### 3. Codegen and the Python views

- `waveflow/build/mm_adaptor_gen.py`: `BramViewPipe` beside `BramView` and `BramViewP1`
  (`module = "mm_bram_port_pipe"`), entered in `BRAM_VIEWS`; an adaptor holding one sets the front's
  `RD_OUTST` (the number the leaf needs; one place decides it).
- `waveflow/hw/mm_bram.py`, `MemSlaveBramWindow`: a field choosing its leaf (`"mm_bram_port"`, the
  default, or `"mm_bram_port_pipe"`). It is the window the ring uses (port A on the bus, port B for the
  kernel).
- `waveflow/hw/memory.py`: a `MemoryMod` subclass naming the new leaf, as `PipelinedMemoryMod` names
  `mm_bram_port_p1`, for a memory with no kernel port.
- `PipelinedMemoryMod` and `mm_bram_port_p1.v` stay as they are (`ltbf_cg_mem_dse` depends on them).
  Add one line to the p1 header pointing at the new leaf as the synthesizable one.

### 4. The window's timing in pysim

Today `MemSlaveBramWindow`'s bus side costs nothing (`_on_read` / `_on_write` are `timeout(0)`), so a
read's time comes only from the crossbar's settings. Give it a calibrated timing, as `MemoryMod` has:

- the same record type and loading as `MemoryMod` (`waveflow/calib/mem_timing.py`:
  `stored_mem_timing`, `stored_mem_turnaround`; `calib_dir`), so a read or write costs
  `init + per_word * n` cycles, `serialize` makes the slave serve one transaction at a time, and
  `turnaround` the cycles between back-to-back transactions;
- different records for the two leaves (their identities differ), so pysim sees 2 cycles a word with
  `mm_bram_port` and 1 with `mm_bram_port_pipe`;
- refactor the loading so the two classes share it rather than copy it.

Port B (the kernel's side) stays untimed: the kernel's model charges its accesses
(`mm_bram.py`'s docstring).

## Tests and gates

| Gate | What it shows | Marker |
|---|---|---|
| **Existing adaptors unchanged** | every generated adaptor of the repository's tests and examples is byte-identical at `RD_OUTST = 1` | fast |
| **The leaf alone, in XSI** | bursts of 1, 2, 16, 64, 256 beats; back-to-back bursts; `rsp_ready` held low at random; writes interleaved with reads (the front's AW/AR alternation); FIXED bursts; reads outside the memory (SLVERR); every word checked; **an N-beat read in exactly `init + N` cycles**, `init` stated | `-m xsi` |
| **Order across leaves** | a window and a queue in one adaptor, reads alternating between them with `RD_OUTST > 1`: every response from the right leaf, in order | `-m xsi` |
| **Ordering guarantee 1** | `tests/build/test_mm_bram_order_xsi.py` repeated with the new leaf: a doorbell written after data still reaches the kernel after it | `-m xsi` |
| **Timing closure** | Vivado synthesis and implementation of an adaptor with one window, at 4 ns on `xczu48dr`, at 128 and 512 bits, for each of `mm_bram_port`, `mm_bram_port_p1` and `mm_bram_port_pipe`: WNS, LUT, FF, BRAM. The new leaf must close (WNS ≥ 0); report whether p1 does | `-m vitis` |
| **pysim against RTL, one master** | the memory harness (`waveflow.hw.harness.memory_harness`, `MmScript`) on the window with each leaf: every op's start and end within 1 cycle of RTL after the fit | `-m xsi` |
| **pysim against RTL, contention** | 2, 3 and 4 `MemRStream` masters reading 1,024-word blocks of one window over the crossbar, at 128 bits: each block's read time within 2% of RTL | `-m xsi` |

Add every new XSI gate to the session's expected count (`WANT_XSI_GATES`), as earlier stages did.

## Platform files

Under `waveflow/calib/platforms/xczu48dr_250mhz_vitis2025_1/`:

- `modules/<window identity>/timing/records.jsonl`: the memory timing (`init`, `per_word`, `serialize`
  per direction) and the turnaround, for `MemSlaveBramWindow` with **each** leaf, at 128 and 512 bits,
  from the harness fits above (`mem_timing_record`, `mem_turnaround_record`).
- `modules/<window identity>/resource/records.jsonl`: LUT, FF and BRAM from the implementation runs.
- The same two for the new `MemoryMod` subclass, if its identity differs from the window's.
- **The crossbar's settings at 250 MHz on this part** (`latency_init`, `latency_travel`, measured with
  the contention gate's masters): if a record type exists for them, file them; if not, say so and write
  the numbers into `docs/guide/interface/axi_mm/crossbar.md`, and propose the record type (do not invent
  one in this plan).

Files under `modules/` are outside the manifest audit (`tests/linalg/test_no_example_imports.py`,
`NOT_OWNED`); anything filed elsewhere under the platform is not, and needs that test's list updated in
the same commit.

## Docs to update

| Page | What changes |
|---|---|
| `docs/guide/interface/axi_mm/slave_views.md` | the BRAM window's leaves: `mm_bram_port` (two cycles a word), `mm_bram_port_p1` (one, simulation only), `mm_bram_port_pipe` (one, timing-closed); when to choose which |
| `docs/guide/interface/axi_mm/slave_howitworks.md` | the front's `RD_OUTST`: outstanding reads, in order, and the decoder's order FIFO |
| `docs/guide/interface/axi_mm/modeling.md` | the window's bus side timed from its record, as `MemoryMod` is |
| `docs/guide/interface/axi_mm/crossbar.md` | the settings measured at 250 MHz on `xczu48dr` |
| `docs/guide/memory/memorymod.md` | the new `MemoryMod` subclass beside `PipelinedMemoryMod` |
| `docs/guide/interface/primitive/bram.md` | a line where port A is discussed, pointing at the full-rate leaf |
| `docs/examples/aoa_detect/timing.md` | the read port's row: measured, and the half-rate problem resolved; the read and search cycles recomputed |
| `plans/mm_slave_adaptor.md` | a line in its record: the full-rate leaf and `RD_OUTST`, pointing here |
| `docs/evaluation/timing_fidelity/` | a ledger row if pysim and RTL disagreed anywhere on the way, with the cause |

## Stages

| Stage | What | Done when |
|---|---|---|
| 0 | Baseline: run `tests/hw/test_mm_bram.py`, `tests/hw/test_mm_adaptor.py`, `tests/build/test_mm_adaptor_gen.py` and the `-m xsi` gates of `tests/build/test_mm_*_xsi.py`; record pass counts and the time of each | the numbers recorded here |
| 1 | The front's `RD_OUTST` and the decoder's order FIFO; the byte-identical gate | existing gates pass unchanged; generated adaptors byte-identical at 1 |
| 2 | `mm_bram_port_pipe.v`, `BramViewPipe`, the window's leaf field, the `MemoryMod` subclass | the leaf-alone and order gates pass at RTL, exact cycles |
| 3 | Timing closure and resources at 4 ns | the table filled; the new leaf closes |
| 4 | The window's pysim timing from records; the harness fits; the platform files | both pysim gates pass; records filed |
| 5 | The docs of the table above | every page updated; `pytest tests/docs` passes |
| 6 | The record: a section *Record* at the end of this file with every number, the gates' counts, what is left | committed and pushed |

## Read first

- `waveflow/build/rtl/axi_slave_front.v`, `mm_bram_port.v`, `mm_bram_port_p1.v`, `bram_t2p.v`
- `waveflow/build/mm_adaptor_gen.py` (`BramView`, `BramViewP1`, `BRAM_VIEWS`, the decoder)
- `waveflow/hw/mm_bram.py`, `waveflow/hw/memory.py` (`MemoryMod`, `PipelinedMemoryMod`), `waveflow/hw/memif.py`
  (the serialized slave: `_serialized_reads`, `turnaround`)
- `waveflow/calib/mem_timing.py`, `waveflow/hw/harness.py` (`memory_harness`)
- `plans/mm_slave_adaptor.md` (the request bus, ordering guarantee 1), `plans/unit_harness.md` Stage 4c
- `docs/guide/interface/axi_mm/slave_howitworks.md`, `slave_views.md`, `modeling.md`, `crossbar.md`
- `tests/build/test_mm_bram_order_xsi.py`, `tests/hw/test_mm_bram.py`
- `docs/examples/aoa_detect/timing.md` and `arch.md` (why this is needed)

## Out of scope, next

- **The kernel's port B as a declared endpoint.** `mm_bram.py` says the window is not yet joined to
  `T2pBram` / `BramIF`, so a generated kernel cannot bind port B. The emitter detection example's capture
  kernel writes the ring through port B, so it needs this next; it is its own plan.
- A wider read port (256 bits) for the window: the same leaf at a wider `DW`; measured in Stage 3 at 512
  bits for the capture side, but not wired into the example here.

## Record

### Stage 0 (2026-11-28): the baseline

On the lab machine (Ubuntu 24.04, 8 cores), branch `lab/bram-fullrate` from `origin/main` `d5500d76`,
the venv's Python 3.12; Vivado xsim 2025.1, system g++ 13.3.

| Run | Passed | Skipped | Time |
|---|---|---|---|
| `tests/hw/test_mm_bram.py` | 3 | 0 | |
| `tests/hw/test_mm_adaptor.py` | 4 | 0 | |
| `tests/build/test_mm_adaptor_gen.py` | 7 | 0 | |
| the three together | 14 | 0 | 0.27 s |
| `-m xsi` `test_mm_bram_order_xsi.py`, `test_mm_queue_xsi.py`, `test_mm_regbank_xsi.py` | 6 | 0 | 142 s (each file's fixture ~35 s: xvlog / xelab) |
| `-m xsi` `test_mm_queue_memw_xsi.py` | 2 | 0 | 36 s |

The two `test_mm_queue_memw_xsi.py` gates first skipped (no csynth of `examples/interleaver`'s
`mem_w_stream` in a fresh worktree, which is gitignored output). Built out of band as the test's
docstring says: `mem_stream_gen.generate()`, then `mem_w_stream.tcl` (Vitis HLS 2025.1, the example's own
`xc7z020clg484-1` at 10 ns, 14 s), then `render_rtl_f` (the post-csynth hook that writes the source
stamp). Both then passed.

The cycle numbers the gates pin, all xsim 2025.1, unchanged from their recorded values:

- ordering, one front: `got 256, stale 0, data_end 263, bell_end 267, first_read 269`; per view:
  `got 256, stale 63, data_end 263, bell_end 12, first_read 14`;
- queue (durations / ends): `w4 612` / `695`, the sink's `first 601, last 787`;
- register bank: `c2 87`, the packets at `p1 104, p2 109`;
- MemWStream into the queue: `first 25, p1 32, p2 103, p3 128`.

### Stage 1 (2026-11-28): the front's `RD_OUTST`, the decoder's order FIFO

**The front** (`axi_slave_front.v`): a parameter `RD_OUTST` (default 1). Within a read transaction it
issues a beat's request whenever fewer than `RD_OUTST` would remain outstanding after this cycle's take,
`(outst - take) < RD_OUTST`, i.e. `outst < RD_OUTST || take`, and counts the responses back; RLAST is the
response taken with nothing left to issue and one outstanding, and the transaction ends there. At 1
that is, term by term, the old `!outst_q || r_take` with `outst_q` one bit, so the old behaviour is the
default by construction. Refused reads never reach a leaf (unchanged), so outstanding requests do not
touch them. A leaf whose response comes `L` cycles after its request needs `RD_OUTST = L` for a beat a
cycle.

**The generator** (`mm_adaptor_gen.py`): each view declares `rd_outst` (1 for every existing view;
`mm_bram_port` too, since it keeps one read in flight itself), and `rd_outst_of(views)` -- the most any
view needs -- is the one place the front's `RD_OUTST` is decided, asked by both renderers and by the
instance inventory (`SystemTopSpec.instances`). `front_params` emits `RD_OUTST` only when it is not 1.

**The order FIFO lives in the decoder**, not the front:

- the leaf index is the decoder's (its `sel`, and the hole); the front sees only local addresses and
  would have to learn the window layout to tag a request;
- a single-view slot has no decoder and needs no FIFO (one leaf answers in order), so in the front it
  would cost flops in every slot;
- the front's ports and the request-bus contract stay as they are, and no leaf changes;
- at `RD_OUTST = 1` the decoder renders byte for byte as before: the latched select is the depth-1
  case;
- it is the hand-written `mm_sync_fifo` (`W` = the select bits + a hole bit, `DEPTH` = `RD_OUTST`
  rounded up to a power of two), instanced by the decoder, so the generated logic is still a table and
  instances. The inventory lists it (`u_<adaptor>_order`).

Why a FIFO at all, when legal AXI keeps one transaction's beats in one view (a burst may not cross 4 KB,
and each view is a 4 KB window)? Two cases: **the hole** -- with two reads of the span's unused tail
outstanding and RREADY low, the old latched `hole_rsp` answers one and the front waits forever for the
second; and a burst that crosses a window boundary anyway (illegal, but it must not hang or take a
response from the wrong leaf). Both are in Stage 2's order gate.

**Gates.** The byte-identical gate (`tests/build/test_adaptor_rtl_unchanged.py`): 69 renders --
`render_view_slot` and `render_adaptor_slot` over every view type at 32, 64, 128 and 512 bits and over
adaptors of 1 to 5 views (holes at 3 and 5), the tops of the four mm XSI gates, and the system tops and
inventory parameters of mm_fir (both shapes), markov, and the memory harness with `mm_bram_port` and
`mm_bram_port_p1` -- digested on the unchanged generator (committed first, `96ce7e6c`) and unchanged
after. New fast tests of the `RD_OUTST > 1` rendering in `test_mm_adaptor_gen.py` (10 pass).

| Run (xsim 2025.1) | Result |
|---|---|
| the 8 mm XSI gates of Stage 0 | 8 passed, every pinned cycle number unchanged (38.6 s; xvlog and xelab re-ran on the new front, only the unchanged testbench binaries were reused) |
| `tests/examples/test_mm_fir_xsi.py` (csynth first: `mm_fir_build --through csynth`, Vitis HLS 2025.1) | 10 passed, 611 cycles (one front) and 618 (per view), as pinned |
| `tests/build` + `tests/hw`, `-m "not vitis and not xsi and not gem5 and not sionna"` | 2,670 passed, 45 skipped, 1 failed |

The one failure is `test_xsi_workspace_copies.py::test_there_are_workspaces_to_check`, which wants at
least three example `xsi/` directories holding a BFM copy; those are build outputs, and this fresh
worktree has two (`interleaver`, `mem_copy`). Not this change's.
