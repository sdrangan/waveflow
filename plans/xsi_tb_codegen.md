# Plan: the XSI testbench — library first, then `sequential_xsi_tb`

## Context

Flow 2's DUT half now generates: a composite's `ap_ctrl_none` top comes from the component graph
(`composite_top_spec`), and as of `e833a13` its task **bodies** come from `run_iter`
(`task_files_to_str`). The TB half does not. `examples/interleaver/xsi/mem_copy_bfm_tb.cpp` is 290
lines, ~230 of them one `main()`; the four XSI TBs total 956 lines. They are hand-written, and asking
a user to write one is asking them to hand-implement an AXI4 slave.

**The measurement that decides the approach (2026-07-16).** The AXI read/write FSM in
`interleaver_canon_bfm_tb.cpp` is **line-for-line identical** to `mem_copy_bfm_tb.cpp`'s — same five
states (`AR_IDLE`/`R_SEND`, `AW_IDLE`/`W_RECV`/`B_RESP`), same `wstrb` read-modify-write, same `rlast`
arithmetic — differing only by a `g0_`/`g1_` prefix because that design has two bundles. It is
copy-paste-and-rename, four times. What `mem_copy_bfm_tb.cpp` is actually made of:

| Part | ~lines | Nature | Wants to be |
|---|---|---|---|
| AXI read/write FSMs, `driveAll`, cycle loop, sampling | ~90 | design-independent | **library** (duplicated x4) |
| XSI open, reset, timeout, zero-pinning of undriven inputs | ~40 | boilerplate | **library** |
| Port handles (`P_cmd_data` .. `P_bready`) | ~31 | derivable from the boundary | generated |
| `s_done` accumulate + `xfer_msg[0]` echo check | ~17 | schema-aware | generated |
| Scenario (`N`, `NUM_CMDS`, `SRC_W[]`, `DST_W[]`, `known_word`) | ~18 | per-test | generated |
| `cmd_words` packing (`src \| dst<<32`) | ~7 | **`CopyCmd.serialize()`, re-written by hand in C++** | generated |
| Golden check (dst region == src region) | ~22 | per-test | generated |

~45% library, ~35% generated, ~20% boilerplate.

**Why the host-activated TB was easy and this is not.** `SeqTB.main()` is a *sequential program that
calls the DUT through a driver*; Vitis csim supplies the protocol underneath. There is an abstraction
boundary, so lowering `main()` is a small job. For a free-running DUT under XSI **there is no
abstraction** — the TB *is* the protocol peer, cycle by cycle. The difficulty is not that XSI is
harder to generate; it is that we are generating into a missing layer.

**The move: do not generate the protocol. Build the abstraction, and the monster stops existing.**
Emitting ~90 lines of AXI FSM per design would produce the same code every time, in the language
where it is hardest to review, to solve what one library solves once. With a BFM library the residue
is a sequential program against an API — the shape we already generate well:

```cpp
XsiSim sim("xsim.dir/mem_copy/xsimk.dll");
FlatMemory mem(8192);
AxiMmReadSlave  gmem0(sim, "m_axi_gmem0", mem);
AxiMmWriteSlave gmem1(sim, "m_axi_gmem1", mem);
AxisMaster s_cmd(sim, "s_cmd");
AxisSlave  s_done(sim, "s_done");
for (auto& j : jobs) s_cmd.push(j.serialize());
sim.run_until([&]{ return s_done.count() == NUM_CMDS; });
```

## Stage 0 — measure (DONE 2026-07-16)

The table above. Recorded here because it is the evidence for "library, not codegen"; if a later
stage is tempted to emit protocol, re-read it.

## Stage 1 — extract the BFM library from the four working TBs — **DONE (`709bb1e`, 2026-07-16)**

All four TBs run on `xsi/bfm/xsi_bfm.h`; every gate held from a fully clean rebuild.
`956 -> 495` lines across the four, `+347` shared, and **zero** protocol constructs left in any TB.
Stage 2 (thin to scenario + golden) fell out with it — that is all the TBs now contain.

What the extraction taught, which designing the API would not have:
- `mem_w` drains on the **B response**, not the word count → `AxiMmWriteSlave::saw_b()`. "All the
  data went out" is not "the write completed"; a TB draining on `w_count` alone can stop before the
  last burst is acknowledged.
- `interleaver_canon` records **per-job done cycles** (that print *is* the throughput measurement),
  so the sink models words while the TB derives timing from its beat count.
- The `sample`/`update`/`drive` split is load-bearing, not stylistic: a beat is decided from values
  sampled *before* the rising edge and applied *after* it.



**Pure refactor. Zero behaviour change.** Factor `mem_r` / `mem_w` / `mem_copy` /
`interleaver_canon` TBs onto a shared BFM in `examples/interleaver/xsi/bfm/` (location revisited in
Stage 6):

- `XsiSim` — open/close, clock phases, reset, `run_until(pred, max_cycles)`, timeout reporting.
- `FlatMemory` — the `std::vector<uint64_t>` arena + `wstrb` RMW.
- `AxiMmReadSlave` / `AxiMmWriteSlave` — one per bundle, constructed with a port prefix
  (`m_axi_gmem0`), owning the FSM that is currently inlined and renamed per design.
- `AxisMaster` / `AxisSlave` — `push(words)` / `count()` / `words()`.
- Zero-pinning: drive every TB-driven input not otherwise claimed to 0, by enumeration.

**Why first, and why not "design the API".** The four TBs are working witnesses; extracting from them
*discovers* the API instead of guessing it. We guessed an API once on this codebase — `CodegenSource`,
designed against a presumed `HwParam` surface — and it was wrong and got reverted
(`plans/codegen_source_options.md`). Do not repeat that here. The API is whatever falls out of making
four real TBs share code.

**Gate — the recorded baseline.** Automated as `pytest -m xsi` (`06032cd`); numbers **re-recorded
2026-07-16** when the reporting bug below was fixed:

| TB | result | **cycles** (to last completion) | drain tail |
|---|---|---|---|
| `mem_r_stream` | PASSED, collected=128 | **158** | 256 |
| `mem_w_stream` | PASSED, w_count=128 | **176** | 256 |
| `mem_copy` | PASSED, done=16, w_count=2048, job_fails=0 | **2835** | 512 |
| `interleaver_canon` | PASSED, done=8/8, n=256 nj=8 | **3469** | 512 |

**The originally-recorded 414 / 432 / 3347 were wrong** — not the designs, the *measurement*. Three
of the four TBs printed `cyc` (work **+** a fixed drain tail) under the label `cycles=`, while
`interleaver_canon` printed time-to-last-done. So the two small kernels were reporting numbers that
were *majority tail*, and any of them would have shifted if someone touched a testbench constant.
All four now report time-to-last-completion with `(tail=N)` shown separately.

**What the numbers say, now that they say anything:** `mem_copy` is 2835/16 = **~177 cyc/job**
against **~176** for ONE write on its own. The reads hide *entirely* behind the writes — per-job cost
is `max(read, write) = 176`, not `read + write = 334`. That ~1.9x is the free-running pipeline, and
it is exactly what Stage 5 must not destroy.

A cycle count that moves is a real behaviour change — a regression or an improvement — and both want
a human look. Exact, never a bound.

**Watch:** `xsi/rtl_<top>.f` lists RTL files explicitly, is **hand-maintained** (nothing generates it —
only `run.bat` reads it), and a stale `.f` plus a cached `xsimk.dll` can fake a PASS
(`project-mem-stream-phase2-gate2`). It is also RTL-module-name-sensitive: swapping in the generated
`mem_seq_task.h` renames `..._s_r_xfer_msg_RAM_...` to `..._s_mr_xfer_msg_RAM_...` (the body's local
is `mr`, not `r`), which the `.f` names explicitly. Regenerate before believing any result here.

## Stage 2 — thin each TB to scenario + golden

With the library in place each TB should be ~20-30 lines: build the arena, push commands, run until
done, check. Anything that will not thin is a finding — it is either genuinely per-design (keep it
visible) or a gap in the library (fix the library).

**Gate:** same as Stage 1. Bit-exact, same cycle counts.

## Stage 3 — derive the port binding from `TopSpec` — **DONE (`5c64537`, 2026-07-16)**

`render_ports_h(spec)` emits `<top>_ports.h`; all four TBs bind through it, and not one hand-written
port name or pin-low list remains. Gates unchanged (then 414/432/3347/3469; re-recorded below). Pinned by
`tests/build/test_ports_header.py`, including the drift claim itself and the never-pin-what-you-drive
condition. The enabling fix was in the **spec**, not the renderer: `ExtPort` kept only rendered
strings, so `(name, kind, bundle)` was lost — `TopSpec` could be *rendered* but not *asked*. It now
carries the triple, and `xsi_prefix` encodes the asymmetry (AXIS keeps its own name; `m_axi` is named
after its **bundle**). `ZERO_PORTS` is derived as the complement of what the BFM drives.

One real difference the gate adjudicated: the derived set pins `m_axi_gmem1_BRESP`, which mem_copy's
hand list omitted while interleaver's had it — the two hand lists had drifted from *each other*.
`3347` unchanged, so it is inert, and the inconsistency is gone. Exactly the class of bug this stage
exists to make impossible.

**Still open — the other half of drift:** `rtl_<top>.f` remains hand-maintained. A `.f` generator was
validated this session (it reproduced the tracked file byte-for-byte before being trusted on a
renamed-RAM case) but is not wired into any build step. Until it is, a renamed RTL module plus a
cached `xsimk.dll` can still fake a PASS.

### Original notes


`P_cmd_data = d.port("s_cmd_TDATA")` is not information — it is `("s_cmd", axis_in)` from the
boundary spec plus Vitis's mechanical naming (`<port>_TDATA`, `m_axi_<bundle>_ARVALID`). The same
`TopSpec` that emitted the top's `#pragma HLS INTERFACE` lines determines every port name.

**Derive the TB's binding from that one spec** and the TB cannot drift from the kernel by
construction: one spec, two consumers. Cross-check each derived name through `get_port_number` at
startup and **fail loudly** if absent — which also converts the stale-`.f` false-PASS from a silent
wrong answer into an error.

**Not** by parsing RTL or reports: those are downstream artifacts, and reading them back would make
the TB depend on the thing it is supposed to test.

## Stage 4 — scenario and golden from the Python — **PARTLY DONE (`1f976b9`, 2026-07-16)**

**Done — the schema-drift half.** `mem_copy`'s command words are now the *output* of the real
`CopyCmd.serialize()`, emitted into `xsi/mem_copy_vectors.h` (`render_vectors_h`, plain C++ no HLS
types); `DONE_WORDS` is `MemComplete.nwords_per_inst(width)`. The scenario lives in `mem_copy.py` as
`XSI_*`. Two toolchain-free tests run in the fast loop: the committed header must match what the
schema produces now (verified to fail), and `CMD_WORDS` must *be* `serialize()`'s output (so a
hand-rolled packer cannot come back).

**The constraint that decided the design:** an XSI TB is host-compiled by mingw g++ against the xsim
headers only, and `copy_cmd.h` needs `ap_int.h`/`hls_stream.h`. So a TB *cannot call the schema* —
which is why the duplication existed. Emitting serialize()'s output removes the second
implementation; generating a plain-C++ packer would have re-created it.

**Not done — the pattern half.** `known_word` is still stated in both `mem_copy_sim.py` and the TB.
Unifying means emitting the whole arena image as data. Deliberately deferred: a drifted *pattern*
still tests a copy, whereas a drifted *packing rule* sends malformed commands and makes the test
meaningless — that was the one worth removing. `interleaver_canon`'s scenario (`Pidx`/`Xval`/`fbits`)
is untouched for the same reason.

### Original notes


- `cmd_words` packing is `CopyCmd.serialize(word_bw=64)` re-implemented by hand in C++. Use the
  schema. This is the same silent-drift class as hand-written task headers drifting from their schema.
- `DONE_WORDS = 5` is `MemComplete.nwords_per_inst(64)`.
- `SRC_W[]`/`DST_W[]`/`known_word()`/the golden check are the pysim harness's `run_copy(jobs=...)`
  restated in C++. One statement of a test, two backends (pysim, XSI) — the same relationship
  `mem_copy_sim.py` already has to the golden.

## Stage 5 — `sequential_xsi_tb`: emit the thin `main()`

Only now is there something small enough to be worth generating. `SEQUENTIAL_XSI_TB` is already a
declared target ("a cycle-based XSI BFM driving a free-running DUT"), and `extract_testbench` already
lowers a sequential `main()` in which blocking stream ops and DUT construction are legal — the exact
shape Stage 2 leaves behind.

**Open question, to verify not assume:** is `sequential_xsi_tb` the *same extraction* as
`sequential_vitis_tb` with a different emitter backend? If yes this stage is an emitter, not a path.
If no, the difference is the finding. Do not design Stage 5 until Stages 1-2 have shown what the thin
TB actually looks like.

## Stage 6 — homes (deferred until after Stages 1-2, deliberately)

**Decision (2026-07-16): `mem_copy` stays in `examples/interleaver/` until Stage 1-2 are done, then
the two get pulled apart** — at which point what each actually needs is *visible* rather than guessed.
Stage 1 wants the four TBs co-located and building against one `xsi/` workspace (shared
`xsi_loader.cpp/h`, `xsi_shared_lib.h`, `run.bat`, `xsim.dir`), because extraction from all four is
what discovers the API. Moving `mem_copy`'s TB out first would fight that and invent a directory
structure Stage 1 would then rebuild.

**Done:** `composite_gen` promoted to `waveflow/build/composite_gen.py` (`7b1c617`) — it was framework
parked in an example, and `waveflow/build/hwgen.py` + `waveflow/hw/hw_composite.py` referenced it from
their docstrings (a layering inversion). This was the genuine prerequisite and is independently
valuable.

**Still shared, surfaced by attempting the move — promote as Stage 6, informed by Stages 1-2:**

- The BFM library itself (Stage 1's output) is framework.
- `mem_stream_sim`'s `CmdDriver` / `WordSink`: `mem_copy_sim.py` imports them, so the pysim helpers
  are shared infrastructure too (~30 lines, easy).
- The `xsi/` workspace: `xsi_loader.cpp/h`, `xsi_shared_lib.h`, `run.bat`, and the common `xsim.dir`.
  All four TBs build from it. This is the one Stage 1 restructures.
- Hand-written hook `.cpp` bodies have no home: `gen/` is regenerated and would clobber them. This
  blocks *adopting* the generated task body, independent of anything in this plan.
- Rename scheme (`project-example-rename-scheme`): `mem_copy` vs `memcpy`.

## Depends on

- Nothing in Stages 1-2 — they are a refactor of code that already works.
- Stage 3 needs `TopSpec` reachable from the TB generator (Stage 6's promotion makes this clean, but
  is not required to prototype).
- Stage 5 needs Stages 1-4.

## Not in scope

- **Generating AXI protocol code.** The anti-goal of this plan. See Stage 0.
- Vitis C/RTL cosim: it refuses `ap_ctrl_none`. XSI is the only RTL path for a free-running DUT.
- SystemC / `concurrent_systemc_tb` (Flow 3). The BFM library is plausibly shared with it; do not
  design for that until Flow 2 works.
- `free_running_kernel` (a leaf as its own top). Different target; not needed for composites.

## Verification

- **Stages 1-2:** all four XSI TBs PASS with identical cycle counts, from a regenerated `.f`. This is
  the whole safety net — four working witnesses held bit-exact through a refactor.
- **Stage 3:** every derived port name resolves via `get_port_number`; a deliberately wrong name must
  fail loudly rather than silently skip (the current `if (p >= 0)` zero-pinning idiom hides typos).
- **Stages 4-5:** a generated TB reproduces the hand-written TB's result on `mem_copy` — same jobs,
  same cycle count, same PASS. `mem_copy` is the right first target: it has both an m_axi read and an
  m_axi write bundle plus two AXIS ports, so it exercises every BFM piece, and it already has a
  working TB to diff against.

## Notes carried in

- **csynth OK is not evidence of correctness.** Nested-struct-by-value silently DCEs a kernel while
  csynth reports success (`reference-hls-hook-csynth-gotchas`). Every claim in this plan is gated on
  XSI, not csynth.
- The generated task body (`e833a13`) is csynth-verified equivalent to the hand-written one — same
  RTL module set, identical latency/II/LUT/FF — but has **never been run**. XSI via this plan is what
  would prove it functionally.
