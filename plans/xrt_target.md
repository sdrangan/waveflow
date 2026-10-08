# Plan: XRT as a target — lower the Waveflow system to kernels, a `.cfg` and host code

> **Status (2026-10-04): DRAFT, nothing built.** For review. Comes out of a discussion on whether
> the IPI / XSI system flow duplicates what AMD's XRT flow (`v++` link + `hw_emu` + XRT host code)
> already provides. AMD's teaching board (AUD-Z3) and the wireless line (Versal-RF) are both XRT
> platforms, so XRT must be a system target. This plan makes it one: **a new target, not a
> rewrite.**
>
> **Decided 2026-10-07: two targets, not a pivot.** The Waveflow vocabulary is the general one. XRT
> realizes the subset its rules allow; IPI / XSI realizes all of it and is **no longer frozen** — see
> [Two targets](#two-targets-xrt-and-ipi--xsi).

## Why

Waveflow's system vocabulary was built for a custom Vivado IPI system, where the host pushes words
into memory-mapped slaves (`MemSlaveAdaptor`) and the RTL is verified by an XSI harness Waveflow
generates. That vocabulary turns out to be close to XRT's, arrived at independently, but XRT
already provides the system layer:

| Layer | IPI / XSI flow (today) | XRT flow |
|---|---|---|
| one kernel | HLS cosim, or XSI where cosim refuses `hls::task` + `m_axi` | HLS cosim (stream-only free-running kernels and host-launched kernels both cosim) |
| the system | Waveflow-generated XSI harness + BFMs | `hw_emu` (from AMD) |
| host | XSI C++ host on generated endpoints | XRT host code, run as is |
| timing before building | pysim | **pysim** (nothing in XRT does this) |

Under XRT's rules XSI has no layer left. Pysim does, and it's the part worth investing in. (A design
that stays within XRT's rules needs no XSI; one that uses the rest of the vocabulary still does — see
[Two targets](#two-targets-xrt-and-ipi--xsi).)

## Positioning: Waveflow's value next to XRT

XRT is a runtime plus a link configuration. It says **what is connected**, not **when anything
happens**, and treats data as untyped bytes. Waveflow supplies what sits around it:

1. **Timing before linking.** A `v++` link plus `hw_emu` costs minutes to tens of minutes and
   answers one configuration. Pysim answers many in seconds: where the kernel boundaries go, stream
   vs memory between kernels, DDR vs HBM pseudo-channel vs PLRAM, chunk size and credit window,
   host launch overhead vs batching. That's design-space exploration XRT doesn't offer.
   *Claim to measure*: pysim vs `hw_emu` wall-clock, and pysim vs board accuracy, on the same system.
2. **One type contract across model, kernel, host and docs.** `xrt::bo` is untyped bytes and
   streams are bare words. DataSchema generates the Python value class, the HLS struct and
   lane packing, **and the host-side `bo` pack/unpack** from one declaration. That closes off the
   host ↔ kernel layout mismatch, which fails silently (cf. `plans/stream_array_alignment.md`).
3. **The concurrency laws XRT doesn't remove.** The `hls::task` reset trap, un-paced free-run
   deadlock, request/response deadlock, doorbell-after-write-response, credit window ≥
   bandwidth-delay product. All of them live in the kernels, so they apply under XRT. Waveflow
   encodes them as checks and as interfaces that are correct by construction.
4. **Generated plumbing.** XRT leaves the `.cfg`, the data movers and the host buffer code to the
   user. Waveflow generates them from the system graph.
5. **One host script, two backends.** A pysim backend of the pyxrt API subset lets the same Python
   host script run against the model and, unchanged, against the board.

**One-line pitch:** *Waveflow predicts what your XRT system will do before you link it, and keeps
the model, the kernels and the host buffers in agreement.*

What Waveflow is **not**: a replacement for XRT, `hw_emu` or HLS cosim. Those become backends
Waveflow drives and checks itself against.

## The model: intent vs realization

Kernels declare **transactional intent**; the target chooses the **realization**; pysim models the
**realization** (push and pull have different timing, so the realization is what gets timed).

| Intent (what the kernel or host declares) | IPI realization | XRT realization |
|---|---|---|
| kernel → kernel stream (`StreamIF`) | `axis` port / `hls::stream` | `stream_connect` (`sc=`) between top-level kernels; `hls::stream` inside a kernel |
| credit / acked stream | `axis` forward + reverse | two `sc=` lines |
| kernel reads/writes a memory region (transactional `mem.read(addr, n)` / `mem.write`) | `MemRCmd` + `MemRStream` / `MemWStream` | Waveflow-generated **launched-once, command-driven mover kernel** (today's MemRStream emitted as an XRT kernel) + `sp=` to a bank |
| kernel random access to memory | m_axi endpoint (`MemAddr`) | the kernel's own `m_axi` (host-launched kernel only) + `sp=` |
| host feeds a kernel stream | adaptor queue-in | `bo` + host-launched mm2s + `sc=` |
| kernel stream to host | adaptor queue-out | s2mm + `bo` + `run.wait()` |
| host sets config | adaptor reg bank | scalar args of a host-launched kernel, or a host-launched mover feeding the cfg stream |
| completion / interrupt (`IrqIF`) | `IrqIF` line | `run.wait()` |
| kernel pushes into another module's slave (`MmCreditStreamIF`, `LatestValueIF`, `LockedT2pMemIF` across kernels) | adaptor | **not legal in XRT kernels**: extensible platform only (see Stage 8) |

Kernel-to-kernel shapes under XRT, in order of preference: direct stream; memory + stream doorbell
(+ credit stream if pipelined in chunks); memory + host ordering. Fine-grained exchange is usually a
sign the two belong **in one kernel** (DATAFLOW / `hls::task` inside), so where the top-level
kernel boundaries go is a design decision, made alongside the design cut.

## Legality rules (XRT target)

- A **free-running** (`ap_ctrl_none`) top-level kernel is **stream-only**: no `s_axilite`, no
  `m_axi` (UG1399; cf. `reference-hls-task-no-maxi`). A stream-only `FreeRunMod` lowers as today.
- A `FreeRunMod` containing a memory intent becomes a **launched-once** kernel (`ap_ctrl_hs` +
  persistent DATAFLOW + stop token), or is split so the memory part is its own mover kernel.
- No kernel is a bus slave for data. The only data slaves are the platform's memory banks.
- `stream_connect` joins top-level kernel ports only; host ↔ stream gets movers inserted; widths
  match.
- Topology is fixed at link time.

## Stages

### Stage 0 — Experiment first (no Waveflow code)

On a Linux machine with Vitis and an XRT platform that supports `hw_emu`:

- **Blind test, baseline arm** (`waveflow blind-test --no-waveflow`): spec = the markov behavior
  (two kernels, one writing memory, a host job sequence, measured completion time, contention
  observable as "together > max(alone)"). Run ≥ 3 times. Score against a rubric written in advance:
  both kernels in one `hw_emu`, protocol-detected completion, every job checked, one number
  confirmed from the waveform.
- Keep the best run as the **XRT reference design** for Stages 2–6.
- Record, for XSI and `hw_emu` on the same design: build time (`xelab` vs `v++ -c` + `v++ -l`),
  per-run start-up time, and **simulated cycles per wall-clock second**. Also kernel launch
  overhead and `bo.sync` cost.
- **Verify the assumptions** under *Unverified* below.

Exit: the hypothesis "AI struggles with multi-kernel HLS systems" is either dropped or has
evidence; real XRT numbers exist to calibrate against.

### Stage 1 — Target and legality check

- Add XRT targets to `waveflow/hw/codegen_targets.py` (e.g. a DUT target `xrt_kernel`, a system
  target `xclbin`, a host target `pyxrt_host`), with the docs table in `guide/flows/index.md`
  updated in the same commit.
- An elaboration-time check in the `check(subject, target)` family that refuses the illegal rows
  above with a message naming the XRT alternative.

### Stage 2 — Kernel lowering to `.xo`

- Stream-only `FreeRunMod` → `ap_ctrl_none` kernel (existing `composite_top_spec` + task bodies).
- Memory-bearing module → launched-once kernel.
- `HostActivated` → host-launched kernel; regmap = `v++`'s register layout (already checked by
  `test_regmap_vitis_layout`).
- `v++ -c` step in the build DAG.

### Stage 3 — Connectivity: generate the `.cfg`

From the system graph: `nk=` (instances), `sp=` (bundle → bank), `sc=` (stream connections, with
depth if supported), `slr=` where relevant. `v++ -l` step. Witness: markov (gen → chain).

### Stage 4 — The transactional memory interface

- Kernel-facing `mem.read(addr, n)` / `mem.write(addr, data)` (names TBD), so kernels stop
  hand-writing `MemRCmd`. Lowers to `MemRCmd` + `MemRStream` on IPI and to the command-driven
  mover kernel on XRT. The message still exists in both; kernels just don't write it.
- Memory-backed credit stream as **one interface** (ring buffer of S slots × B words, doorbell after
  write response, credit return), carrying over the markov laws (window ≥ bandwidth-delay
  product; `max_write = depth − resp_words − (crd_every−1)`).

### Stage 5 — Host: a pyxrt backend for pysim

- Pysim implements the pyxrt subset (device, xclbin, kernel, `bo` write/read/sync/map,
  run start/wait). Same script on pysim and on the board.
- DataSchema emits host-side `bo` pack/unpack (Python and C++).
- Design question to settle first: pysim hosts are SimPy coroutines and pyxrt blocks. Options are
  generator-style host code (`yield from run.wait()`) with a thin board-side wrapper, or greenlets.
- Optional: generate C++ host code from the Python host model.

### Stage 6 — `hw_emu` backend and the compare gate

Generate → `v++ -l -t hw_emu` → run → compare pysim timing within a stated tolerance (as the XSI
gates do today). This replaces XSI as the system-level reference.

### Stage 7 — Platform model, calibrated on the board

Launch overhead, `bo.sync` bandwidth, DDR / HBM pseudo-channel / PLRAM read-write cost and
contention. This is the XRT version of "bus = platform property" (`plans/interconnect_platform_model.md`,
`plans/harmonize_calib.md`). Compute stays calibrated per kernel.

### Stage 8 — Extensible platform: where the adaptor and the RF converters live

`MemSlaveAdaptor` is the correct realization of slave-push (mailboxes, doorbells, local buffers),
which is routine SoC practice with no XRT-kernel equivalent. It moves into the **platform** (built
in IPI), with XRT kernels linked into it. Host-only access to a single block can use a
user-managed RTL kernel via `xrt::ip`. The RF converters are expected in the platform too; Stage 0
should confirm this for the target boards.

### Stage 9 — Docs

A prominent mapping page ("Waveflow in XRT terms": the intent / realization table and the legality
rules), XRT terminology in docstrings, and process text in the MCP server pointing agents at it.
**No renames**: names stay intent-level (`StreamIF`, not `StreamConnectIF`), so one name keeps
lowering differently by position and target.

## Two targets: XRT and IPI / XSI

*Decided 2026-10-07; this section replaces "The IPI / XSI flow: frozen".*

The Waveflow vocabulary is the general one, and each target realizes part or all of it:

- **XRT** realizes the subset its [legality rules](#legality-rules-xrt-target) allow: streams,
  movers, host-launched kernels, `run.wait()` for completion. A design inside that subset should use
  it — `hw_emu` is AMD's, and nothing in Waveflow needs to re-verify it.
- **IPI / XSI** realizes all of it, including what XRT kernels cannot express: slave-push adaptors
  (`MemSlaveAdaptor`, `MmCreditStreamIF`, `LatestValueIF`), interrupts raised by an adaptor, and the RF
  converters. Its gates remain the cycle-exact reference.

Why not freeze IPI / XSI, as this plan first proposed:

- **IPI is needed under XRT anyway.** Stage 8 puts the adaptor and the RF converters in an
  extensible platform built in IPI, so an IPI system flow exists either way. The question was only
  whether it can be verified without per-example scripts.
- **The lab deliverables are IPI** (RFSoC 4x2 under PYNQ), and they need exactly the multi-kernel,
  adaptor-facing verification that is missing today.
- **`hw_emu` is Linux-only**, and its accounting of host software time is unverified (see below).

**The unfreeze is scoped.** XSI gets the generated system top and the host as a hooked module
(`plans/xsi_system_top.md`); any further XSI feature needs its own plan and reason. The cost being
bounded is two system flows to keep green.

The two plans share a design point: a host is **one intent with several realizations** — pysim,
the XSI C++ host (`xsi_system_top.md`), the pyxrt backend (Stage 5) — and the per-endpoint
transaction-trace gate from `xsi_system_top.md` is the check that ties any two of them together.

## Decisions for review

1. **Witness order**: markov first (kernel → kernel, memory) or mm_fir first (host-facing)?
   Proposed: markov for Stage 3, mm_fir for Stages 4–5.
2. **`mm_views`** (`waveflow/hw/mm_device.py`): reinterpret as the host-facing *intent* declaration
   (QueueIn → mm2s, QueueOut → s2mm, RegBank → config path), or replace it with a new declaration?
   Reuse is cheaper, but a queue with back-pressure and a buffer per launch are different host
   semantics, so the rename/reinterpretation must say so.
3. **Host coroutine style** (Stage 5): generator-style or greenlets.
4. **Kernel boundary declaration**: is every top-level module a kernel, or is the boundary an
   explicit build choice like the design cut?

## Unverified (check in Stage 0)

- `sc=` accepts a FIFO depth in the installed Vitis version.
- PLRAM bank count and resizing on the target platforms.
- Auto-restart support for `ap_ctrl_chain` kernels in current XRT.
- How the RF converters appear on Versal-RF platforms, and whether the DSP in the wireless arc goes
  to AIE rather than PL HLS. **This decides what a "kernel" is in the RF arc.**
- `hw_emu` contention fidelity: how much of the interconnect is RTL vs TLM per platform.
- How `hw_emu` accounts for **host software time**. Expected: an x86 host's compute between XRT
  calls costs no simulated time; an embedded host in QEMU gets approximate time from the
  QEMU/simulator co-simulation. If so, modeling software delays needs pysim (or an XSI host with
  explicit delays), which belongs in the positioning.
- `hw_emu` is Linux-only, so a Linux development machine (or VM) is needed.

## Not in scope

AIE graphs (separate question, possibly larger); PCIe P2P between devices; deleting the IPI flow.

## Related

`plans/mm_slave_adaptor.md`, `plans/mm_credit_stream.md`, `plans/mm_adaptor_host_endpoints.md`,
`plans/interconnect_platform_model.md`, `plans/design_cut.md`, the xsi_tb_codegen plan ([commit 3052952](https://github.com/sdrangan/waveflow/commit/3052952)),
`plans/board_packaging.md`, `docs/guide/ai_tooling/blind.md`.
