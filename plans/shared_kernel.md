# Plan: sharing a kernel -- arbiter, tags, credits, feedback -- and the guide, skill and example that teach it

**Status: ACCEPTED 2026-11-21. Stages 0-7 DONE (Stage 7 run 2026-11-22 from the planning session); the Stage 7 fixes DONE in pysim and codegen, their RTL gates pending (records at the end).**

## Why

Waveflow's guide and examples teach linear shapes: one kernel, a pipeline of stages, `StreamBroadcast` when
two stages need one stream. Nothing teaches a kernel **K shared by several clients**, or a kernel whose output
comes back to its own input. That is the shape of SALSA (`docs/overview/salsa.md`: PEs with a job queue and a
micro-scheduler, several flows on one fabric), and of any design that keeps one expensive block busy with
independent jobs.

The evidence is an A/B run on 2026-11-21. Two fresh agents, one with only Vitis HLS knowledge and one with the
Waveflow skills, were given a joint fractional delay / Doppler estimator. It uses one FFT for a coarse grid, the
grid's inverse pass, and gradient refinement, across antennas that finish out of order. Both reached the same
architecture: a scheduler, a frame-granular mux in front of the FFT, a tag FIFO beside it, credits, and a
feedback frame. They also shared a flaw: the feedback descriptor is written before its data, so the mux commits
early and starves the FFT. The Waveflow arm was better grounded (the measured `SsrFft` latency, the conj trick
for the fixed direction, requantizing the feedback frame), but it:

- found no merge or arbiter block (`waveflow.streams` has only `StreamBroadcast`);
- found no rule or example for a cycle inside a composite, and called it "untested territory";
- did not propose extending Waveflow (an arbiter, a run-time FFT direction). The rule "if Waveflow cannot
  express something, stop and say so" reads to an agent as "do not add blocks".

The delay / Doppler problem is the **held-out evaluation** for this plan (Stage 7). It must not appear in the
guide, the examples or the skills.

## The design space the guide page states

The four questions for any shared K are: how commands merge (policy), where data lives (inline or by address),
how results get back (tag or address), and what stops one client's full buffer from stalling K.

| option | data | buffering | main risk | best when |
|---|---|---|---|---|
| 1. shared memory | by address; stream arbiter on commands, memory arbiter on data | in memory | port contention, buffer ownership | large or reused data, several readers (SALSA) |
| 2. dedicated inline streams | per-client streams into K | deep FIFOs, or the client blocks | a full stream stalls K mid-frame | one request outstanding, nothing else to do |
| 3. inline with credits | per-client streams, sent only with a credit | one frame per credit | the credit round trip | as 2, without the deep FIFOs |
| 4. data resident at K | loaded once into slots; only commands with a slot id travel | slot storage at K | slot allocation, free-slot tokens | the same data used many times |
| 5. centralized requester | clients become states in one dispatcher, K's only client | the dispatcher's | a complex, serial dispatcher | the control is naturally one thing |
| 6. static schedule | fixed slots | minimal | wasted slots under irregular load | regular workloads |

Replicating K is the baseline whenever K is small (`systems/library.md`: write simple kernels dedicated).

The return path has its own rules:
- K never blocks on a result write. Reserve the response space before issuing (end-to-end credit), or
  guarantee the client always drains it.
- Requests and responses travel on separate channels, so a request backlog cannot block a response
  (message-dependent deadlock).
- A client's request-then-wait is a poll loop, never a blocking read after a write. Vitis schedules both into
  one state and the request is never sent (`rf/rfshotbuf/tx_internal.md`, measured 2025.1).
- A feedback frame is announced only once it is complete (store and forward), so the arbiter never commits to
  a frame that is still being produced.
- A streaming unit on a cycle must flush an isolated frame without the next one pushing it out. `SsrFft` does
  (`dsp/ssr_fft/synthesis.md`, the bubble group; `timing.md`, isolated frames measured).

## Where it lives

| what | where | new or changed |
|---|---|---|
| the rule: extend Waveflow, never work around it | `AGENTS.md:51`; `waveflow/skills/{waveflow-start,waveflow-kernel,waveflow-schema,waveflow-system}/SKILL.md`; `waveflow/mcp/server.py:26` (INSTRUCTIONS); `waveflow/mcp/frames/_common/process.md:75`; `frames/stream_inband/frame.md:37`; the `waveflow init` template (`waveflow/mcp/init_project.py`) | changed |
| `StreamArbiter`: n framed inputs to one, whole frames, priority or round-robin, the winner's index on a side `m_sel` stream | `waveflow/streams/arbiter.py`, `src/stream_arbiter_task.h`, its build step, its RTL gate in `waveflow/streams/rtl.py`; guide `docs/guide/streams/arbiter.md` | new |
| `StreamDemux`: one framed stream to n, routed by a side `s_sel` stream (the arbiter's, carried beside K) or by a field of the frame's header | `waveflow/streams/demux.py` and the same four parts; guide `docs/guide/streams/demux.md` | new |
| the stream blocks' index table | `docs/guide/streams/index.md` | changed |
| `SsrFft` direction chosen per frame | `waveflow/dsp/ssr_fft/` (an option such as `direction="runtime"` with a per-frame direction input; conjugation at the ends plus the `1/L` shift); guide `dsp/ssr_fft/parameters.md`, `interfaces.md`; `systems/library.md` row | changed |
| credit between children of a composite | `CreditStreamIF` exists (`interface/derived/credit_stream.md`) and is routed over the bus today; Stage 1 checks it inside a composite | checked, extended if needed |
| the concept page: "Sharing a kernel" (the table above, the return-path rules, the blocks, the example) | `docs/guide/systems/shared_kernels.md`, linked from `kernels.md`, `streams.md`, `command_response.md`, `glossary.md` (shared kernel, client, arbiter, tag, credit, slot, feedback frame) | new |
| deadlock on a cycle | a section in the shared-kernels page; cross-linked with `plans/deadlock_teaching.md` if that page is built | new |
| the curated example | `examples/os_filterbank/` and `docs/examples/os_filterbank/index.md` (`example_dir:` key) | new |
| the skill | `waveflow/skills/waveflow-shared-kernel/SKILL.md`, copied to `.claude/skills/` and `.agents/skills/` | new |
| hand-offs to the skill | the descriptions and bodies of `waveflow-architecture` (one line in the mapping steps) and `waveflow-pipeline` ("several clients, or a cycle: `waveflow-shared-kernel`") | changed |
| a frame | `waveflow/mcp/frames/stream_shared/` (frame.md, frame.toml, process.md, prompts), reference `os_filterbank`; or a variant of `stream_pipeline` (decision D2) | new |

## The example: a multi-channel overlap-save filter bank

C channels, each with its own filter. Per block and channel: `FFT(block)`, then `× H_c`, then `IFFT` on the
**same** `SsrFft`, then discard the overlap. It uses each part of the pattern once and nothing else:

- **one shared unit**: the FFT, used forward and inverse by every channel;
- **an arbiter** in front of it: new blocks from the input side, and feedback frames (the `× H_c` products)
  with priority;
- **tags beside it**: an `m_sel` / side tag stream carries (channel, pass) past the FFT, because `SsrFft`
  frames are delimited by count only;
- **resident data** (option 4): each `H_c` is loaded once into a slot beside the multiply;
- **credits**: on feedback frames in flight, so the post stage never blocks;
- **out-of-order results**: channels may arrive at different rates, and each answers its own command with a
  `Resp` and a `Status` echoing its `tx_id` (`systems/command_response.md`).

It has no data-dependent iteration. That stays the held-out test's novelty.

Gates, as in every curated example: a bit-exact NumPy golden; each leaf alone in its harness; the composite in
pysim with staggered channels, counts asserted (a deadlock looks like a clean finish); codegen and csynth; the
XSI testbench with an exact cycle count and the FFT's busy fraction read at its ports. Vitis 2025.1; the
version is named next to every number.

A second, memory-based example (option 1: shared on-chip memory, a job queue and a micro-scheduler, close to a
SALSA tile) is **out of scope** here and gets its own plan once this one is built.

## Stages

0. **The rule.** Reword it everywhere listed above: never work around a gap (no hand-packed words, no edits to
   generated code, no ad-hoc substitute); when the right design needs something Waveflow lacks, propose the
   extension: what, where, its parts (model, pysim, HLS body, tests, guide page), following an existing block.
   In a design folder outside this repository, ask the user before changing Waveflow. Rerun the original
   delay / Doppler chat prompt as a quick check that the agent now proposes the arbiter and the run-time
   direction. *Independent of the rest.*
1. **De-risk the cycle.** A stub composite: two clients, a mux, a pass-through K (then the real `SsrFft`), a
   post stage that feeds frames back. Take it through pysim, codegen, csynth and XSI. Record what the
   generator, the top, the testbench and the DAG do with a cycle, and fix what breaks. Check `CreditStreamIF`
   between children. Stage 2's RTL gate waits until the stub runs at RTL; its pysim and HLS work need not (D5).
2. **`StreamArbiter` and `StreamDemux`.** Each one: the model, the templated HLS body, the build step, the RTL
   gate in its own harness, and the guide page, in the shape of `StreamBroadcast`.
3. **`SsrFft` direction per frame.** Bit-exact against the fixed-direction builds in both directions; the
   timing unchanged (a frame every `L/R`); the resource delta measured.
4. **The concept page** `systems/shared_kernels.md` and its links and glossary terms.
5. **The example** `os_filterbank`, with its gates and its doc page.
6. **The skill and the frame.** The skill points to the page and the example and never restates them (house
   rule, `plans/agent_skills.md`). It is checked for triggering: a prompt with a shared unit or a cycle must
   load it, a plain pipeline prompt must not.
7. **Evaluation on the held-out problem.** The delay / Doppler prompt as in the 2026-11-21 run, to the
   Waveflow arm before and after (the baseline transcripts are the 2026-11-21 ones), on Sonnet 5.5 and
   Opus 5.5 (`plans/agent_skills.md`). Score: the architecture (the six options considered, the return-path
   rules kept, extensions proposed); then, in a design folder, whether the agent takes its design to pysim
   with counts asserted, and to XSI on a machine with Vivado. The prompt is kept **outside the repository**,
   so `waveflow kb` and grep cannot find it.

## Decisions

- **D1. Names.** `StreamArbiter` / `StreamDemux`, "shared kernel", "client", "feedback frame". **Accepted.**
- **D2. A new frame or a `stream_pipeline` variant.** A new frame `stream_shared` makes the agent choose it
  from the spec ("a unit used by several clients, or a stage fed back"). A variant keeps the frame list short.
  **Accepted: a new frame.**
- **D3. Where the tag travels.** Beside K on a side stream (works for any K, including count-framed units such
  as `SsrFft`), or in-band in a header (simpler, but K must pass it through). **Accepted:** the side stream is the
  block's default, in-band is an option of `StreamDemux`.
- **D4. Arbiter policy.** Strict priority and round-robin, fixed at build time; weighted later if a design
  needs it. **Accepted.**
- **D5. Stage 2 does not wait for Stage 1's RTL run** (2026-11-21). Its design does not depend on what
  that run shows; only `StreamArbiter`'s own RTL gate waits until the stub compares clean. **Accepted.**
- **D6. `StreamArbiter` is written in Vitis HLS**, not taken from AMD (2026-11-21). **Accepted.** The vendor
  options and why they do not fit inside a composite go on the shared-kernels page:
  - the **AXI4-Stream Switch / Interconnect** (PG085, RTL) arbitrates on TLAST with fixed priority or
    round-robin, a whole packet a grant. But it joins IPs in a block design: it cannot sit between the
    `hls::task`s of one HLS top (a `framed_word` is an HLS FIFO's struct, not a wire). Using it means
    splitting K and its clients into separate tops, the loop crossing IP boundaries on real AXIS, where
    the switch is used **unmodified**. That is the right tool between devices (SALSA's tiles), not inside a
    composite;
  - the **Vitis Utility Library's `streamNToOne`** (`xf::common::utils_hw`: round-robin, load balance,
    tag select) is HLS, but as far as is known moves an element at a time (frames interleave) and uses
    end-flag streams for a run that ends. Stage 2 checks both points; if it can keep frames whole, wrapping
    it under `waveflow.vitis_l1` is the alternative and the page says so.

## Not in this plan

- The memory-based variant and the SALSA job queue and micro-scheduler (next plan).
- Many-to-many fabrics: `CrossBarIF` exists in pysim; whether it synthesizes between kernels is not checked
  here.
- Replicating K automatically, or choosing between the six options automatically (DSE).

## Stage 0 record (2026-11-21)

The rule now reads, in `AGENTS.md` and `waveflow-start`: never work around a gap (no hand-made substitute, no
edit to a generated file, no word packed by hand); Waveflow is early and meant to grow, so when the right design
needs something it lacks, propose the extension (what, where, its parts, following the closest existing block),
build it that way inside the repository, and ask the user first in a design of their own.

- Changed: `AGENTS.md`; the skills `waveflow-start`, `waveflow-kernel`, `waveflow-schema`, `waveflow-system`
  (each points back to `waveflow-start`'s rules); `waveflow/mcp/server.py` (INSTRUCTIONS);
  `waveflow/mcp/frames/_common/process.md`; `frames/stream_inband/frame.md`. The copies in `.claude/skills/`
  and `.agents/skills/` refreshed with `waveflow skills install --dir . --force`.
- Added: `waveflow-architecture` step 2 and its checklist -- a block or option the best map needs is named as
  an extension, not designed around. That is where the reluctance showed in the A/B run.
- Not changed: `waveflow/mcp/resources/agents_block.md` (the `waveflow init` block) never carried the rule;
  it defers to `waveflow-start`.
- Tests: `pytest tests/mcp`, 213 passed. Two failures are pre-existing on `main` and unrelated
  (`test_generated_code_is_excluded_by_default`, `test_generated_files_are_excluded_from_usage_by_default`:
  they need generated build output, which a fresh checkout does not have).
- Check: the held-out prompt rerun once with the new wording (one agent, 2026-11-21). The agent quoted the new
  rule, and its design needed fewer extensions. It shared the FFT with **no arbiter**: the feeder is the FFT's
  only writer and takes the loopback frames itself (option 5 in the table), and the inverse pass is the conj
  trick on a forward FFT. The extensions it named were radix-2 stages (for n not a power of 4) and a longer
  `SsrFft` for a zero-padded grid. It also scheduled other frames between a forward pass and its inverse, which
  avoids the A/B run's arbiter-stall flaw. The check is weak evidence for the rule: the architecture changed
  as well, and one run is one sample.
- Found and fixed: `dsp/ssr_fft/parameters.md` ended "The inverse transform is not built yet", which
  contradicts the page's own `inverse=True` section (built and tested: `tests/dsp/ssr_fft/test_hw.py`). The
  agent read it and concluded the inverse did not exist. The sentence is removed.

## Stage 1 record (2026-11-21): pysim and codegen; RTL pending

The stub is `examples/ring_stub/` (not curated, no doc page): `Mux -> Inc (K) -> Post`, Post feeding each
job's first pass back to Mux (`fb`), a credit token back on a second backward edge (`crd`), the tag beside K
(`tag`). `FMAX = 4` jobs in the loop, frames of 1 to 64 words; K adds 1, so each answer is the input plus 2.
Tests: `tests/examples/test_ring_stub.py`.

What it showed, here (no Vitis):

- **The generator needs nothing for a cycle.** `composite_top_spec` / `render_top` emit `fb` and `crd` as
  ordinary `hls_thread_local` channels at their stated depths; the mux task takes them as inputs, Post
  writes them. Asserted on the generated top.
- **pysim runs the loop**: the composite against the golden on 10 and 40 jobs, more than the credit, so the
  mux waits on credit; K and Post alone against the golden. All bit for bit, TLAST included.
- **Deadlock looks like success, confirmed.** With no effective credit and shallow loop links, the same
  composite answers 0 of 40 jobs and `run()` returns cleanly. The test asserts the counts.
- **The HLS bodies compute the golden.** A g++ run of the three bodies on mock streams (the open-source
  `ap_int` headers from `Xilinx/HLS_arbitrary_Precision_Types`, a minimal `hls::stream`, the tasks called
  round-robin until none moves): both scenarios bit for bit with the golden, every internal FIFO within its
  declared depth (peaks: `k_in`/`k_out` 64 of 64, `fb` 65 of 260, `tag` and `crd` 1 of 5). Not Vitis csim
  and not RTL: the scheduling is sequential. Not committed as a test, since it needs headers from outside
  the repository.
- **The DAG runs through `codegen` and every `golden` step** (`--through golden`, `long_golden`,
  `inc_golden`, `post_golden`); it stops at `csynth` for want of Vitis.

Gaps found, for Stage 2:

- **A polling kernel cannot be harnessed in pysim as written.** The mux's HLS body polls (`empty()`,
  feedback first). Modelled the same way in pysim it never runs out of events, and `harness()` takes no
  `run_until`. So the pysim `Mux` is three processes sharing its output under a priority lock, a second
  description beside the HLS one. The extension: a pysim primitive that blocks until any of several
  inputs holds a burst (for example `yield from self.wait_any(self.s_fb, self.s_in)`), so a polling
  body's model is one `run_iter` in the order the HLS checks. `StreamArbiter` needs it.
- **A mux alone has no comparable RTL recording**: the order it interleaves two inputs depends on when the
  stand-ins deliver them. The composite is comparable because its output order does not depend on timing.
  `StreamArbiter`'s RTL gate needs scenarios whose order is fixed (one source at a time, or both sources
  queued before it starts, with strict priority), or a compare per source.
- **`CreditStreamIF` is not the loop's credit.** It is a window on one link (fwd + cumulative counts). A
  loop's credit is end to end: a token stream from the stage where a job leaves back to the admission
  point. A plain framed stream does that; nothing to extend.
- **The real `SsrFft` on the ring is left to Stage 3 / 5.** Its self-flush, the property a loop needs, is
  already measured (`dsp/ssr_fft/synthesis.md`, `timing.md`); putting it in the stub needs the U64-to-
  `RadixWord` packing the example will have anyway.

Left for a machine with Vitis 2025.1 and Vivado:

```bash
python -m examples.ring_stub.ring_stub_build                          # the composite through compare
python -m examples.ring_stub.ring_stub_build --through long_compare   # 40 jobs
python -m examples.ring_stub.ring_stub_build --through inc_compare    # K alone
python -m examples.ring_stub.ring_stub_build --through post_compare   # Post alone
pytest tests/examples/test_ring_stub.py -m xsi                        # the four gates
```

### At RTL (2026-11-21, Vitis HLS / Vivado 2025.1, the developer laptop)

Every top built and every `compare` is clean: the RTL recordings equal pysim's, word for word, TLAST
included, and they are not empty (the composite: 206 `m_out` words and 10 `m_status` words). The part is
the generator's default, **xc7z020-clg484-1 at 100 MHz** (`composite_gen.DEFAULT_PART`), not the RFSoC.
The gates in `tests/examples/test_ring_stub.py -m xsi`: 6 pass, 0 skip.

- **Vitis accepts the cycle.** Its only remark is `HLS 200-657 Generating channel fb (crd) that flows
  backwards in the dataflow region`, a warning. Every FIFO is synthesized at its declared depth (`k_in`,
  `k_out` 64; `fb` 260; `tag`, `crd` 5). Vitis adds one internal FIFO itself: `start_for_ring_post_task`,
  depth 3 (`HLS 200-1020`).
- **Every loop is II = 1** (achieved = target): `COPY` twice in the mux (the feedback and the input copy),
  `INC` in K (flushable), `READ`, `OUT` and `FB` in Post.
- **The `empty()` polls draw no warning, and the loop does not deadlock at RTL.** The `static` credit gives
  `RTGEN 206-101 Register 'credit' is power-on initialization`; the credit behaves at RTL (40 jobs through a
  credit of 4), so the warning misleads, as it does for the other write-first tasks.
  K gives `HLS 200-656` (a flushable pipeline in an `ap_ctrl_none` region); harmless in every run here.
- **Timing**, the cycle the last `m_status` word leaves:

  | top | scenario | jobs | last `m_status` | run bound |
  |---|---|---|---|---|
  | `ring_stub_top` | `test` | 10 | 934 | 20000 |
  | `ring_stub_top` | `long` | 40 | 6282 | 60000 |
  | `ring_post_top` | `test` | 10 | 956 | 4000 |
  | `ring_inc_top` | `test` | (196 `m_out` words) | 199 (last `m_out`) | 4000 |

  The composite's two are pinned in `test_rtl_loop_timing_is_exact`.
- **Resources** (csynth estimates; BRAM_18K / DSP / FF / LUT):

  | top | BRAM | DSP | FF | LUT |
  |---|---|---|---|---|
  | `ring_stub` (composite) | 14 | 0 | 1172 | 1764 |
  | `ring_post` | 2 | 0 | 367 | 701 |
  | `ring_inc` | 0 | 0 | 12 | 177 |

  12 of the composite's 14 BRAM are the loop FIFOs (`fb`, `k_in`, `k_out`, 4 each).
- `tests/conftest.py::WANT_XSI_GATES` 259 -> 265: the four ring-stub gates the pysim commit added, which it
  did not count, and the two timing gates.

## Stage 2 record (2026-11-21): `wait_any`, `StreamArbiter`, `StreamDemux` -- pysim, HLS and RTL

Done on the developer laptop, Vitis HLS / Vivado 2025.1. Every RTL number below is the generator's default
part, **xc7z020-clg484-1 at 100 MHz**, 32-bit ports.

**1. The polling primitive.** `wait_any(*slaves)` in `waveflow/hw/interface.py` (and `FreeRunMod.wait_any`):
block until one slave holds a *delivered* burst, return the first such index in argument order, take
nothing. It borrows each slave's existing arrival event (`_arrival`, which every put already fires), so it
needed no new hook. A body waits only on the inputs it can act on -- otherwise it returns at once forever.
Tests: `tests/hw/test_wait_any.py` (7). The stub's `Mux` is now one `run_iter` in the HLS body's order
(`wait_any(s_crd, s_fb, s_in if credit)`, then `get_schema_nb` feedback first); the three-process model and
its priority lock are gone. The ring stub's 7 pysim tests and 6 RTL gates still pass, cycle counts unchanged.

**2-3. The blocks**, each in `StreamBroadcast`'s four parts (model, templated body in `src/`, a step, a gate):

- `StreamArbiter` (`waveflow/streams/arbiter.py`, `src/stream_arbiter_task.h`): `n_in` 2-16 framed inputs to
  `m_out`, a frame = `frame_bursts` (1-255) TLAST bursts, never interleaved; `policy` `"priority"` or
  `"round_robin"` (D4); the winner's index on `m_sel`, one word before each frame (D3). Signature
  `(m_out, m_sel, s_in_0, ...)`: the inputs are the pack, last.
- `StreamDemux` (`waveflow/streams/demux.py`, `src/stream_demux_task.h`): one input to `n_out` 2-16, by an
  `s_sel` word a frame (`route="sel"`, the arbiter's `m_sel`) or by bits of the frame's first word
  (`route="header"`, D3's in-band option). An index naming no output drops the frame; pysim counts it
  (`dropped`).
- `StreamBodyStep` (`waveflow/streams/body_step.py`): the copy-a-body step the three blocks' steps now share.

**4. Gates.** `tests/streams/test_arbiter_demux.py`: 26 pysim tests and 4 RTL gates, all pass. The arbiter's
gate is the **pair** `ArbiterDemuxPair` (`waveflow/streams/rtl.py`): `arb.m_out -> dmx.s_in`, `arb.m_sel ->
dmx.s_sel`, each input back on its own output whatever the interleave -- the answer to Stage 1's second gap.
The policies themselves are checked in pysim on inputs held backlogged (the output's reader holds off, then
releases): priority drains input 0, then 1, then 2; round-robin alternates exactly. `waveflow/streams/rtl.py`
now builds any block (`--block broadcast|demux|demux_hdr|pair`); the broadcast's API is unchanged.
`WANT_XSI_GATES` 265 -> 269.

| gate | estimated clock | FF | LUT |
|---|---|---|---|
| demux by `s_sel`, 3 outputs, 2-burst frames | 5.73 ns | 253 | 492 |
| demux by header field, 3 outputs | 3.18 ns | 261 | 422 |
| pair, 3 inputs, round-robin, 1-burst frames | 7.26 ns | 494 | 1000 |
| pair, 3 inputs, priority, 2-burst frames | **7.82 ns** (budget 7.3) | 581 | 995 |

Every loop II = 1. No BRAM, no DSP.

**Found on the way:**

- **A build input that is not an `HwParam` must be listed in `build_fields`**, or the harness's fresh copy
  drops it silently (`harness._fresh_module` -> `build_params`). `policy` and `route` are strings, so not
  `HwParam`s (those are wrapped as `int`). Before the fix the round-robin pair ran as priority and the
  header demux as `s_sel` -- and their pysim tests still passed, since the goldens do not depend on the
  setting. The tests now assert the DUT kept it. Worth a check in the framework: a dataclass field of a
  `FreeRunMod` neither an `HwParam`, in `build_fields`, nor a known runtime field.
- **The frame-copy loop's shape is load-bearing.** A 32-bit burst count was the critical path (10.08 ns in
  the pair); an 8-bit one brought it to 7.82. Two rewrites meant to shorten it further passed pysim and
  csynth (one reached 7.18 ns) but **hung at RTL** after an input's last frame: a `for (;;)` with the exit
  as a `break` after the read, and a `while (!done)` on a flag computed one burst ahead. The mechanism is
  not established (a read issued past the frame's end fits both); the bodies keep `while (b < NB)` with a
  comment saying why. The 7.82 ns case is **open**: it is an estimate on the slowest part at 100 MHz, and
  the real target (250 MHz, xczu48dr) has not been tried.
- **A csynth failed twice on unchanged code, then passed once its project directory was deleted.** The
  priority pair, after the two failed rewrites: `HLS 214-208 ... 'm_out_2' is not an AXI-Stream and/or is
  not a port in the interface`. The generated TCL opens the project with `-reset`, so the cause is not
  known. Recorded in case it recurs.
- **D6 checked.** The Vitis Utility Library's `streamNToOne` (read on `main`,
  `utils/L1/include/xf_utils_hw/stream_n_to_one/`): every policy moves one element an input a turn (frames
  interleave), nothing knows a packet or TLAST, and it is a function that returns once every input raises
  its end flag, not a free-running task. So it cannot be the arbiter inside a composite; written up on
  `docs/guide/streams/arbiter.md`.

Guide: `docs/guide/streams/arbiter.md`, `demux.md`, two rows in `streams/index.md`; `wait_any` on
`interface/primitive/index.md` beside `get_nb`.

Left for later stages: putting the arbiter and demux into the ring stub in place of its hand-written mux
(the stub's mux carries a credit and a design tag, which stay in a design); the 250 MHz check.

## Stage 3 record (2026-11-21): `SsrFft` direction per frame -- pysim and RTL

Done on the developer laptop, Vitis HLS / Vivado 2025.1, **xczu48dr at 250 MHz** (the `SsrFft` gates' part).

**The design departs from the plan's sketch, deliberately.** The plan said "conjugation at the ends plus the
`1/L` shift". That is the inverse in exact arithmetic, but not bit for bit: truncation does not commute
with negation, and `-(-2^15)` does not fit the input (`test_conjugating_at_the_ends_is_not_bit_exact`).
What is exact: the inverse tables are the forward ones conjugated, exactly, at every `L` 16-4096 and twiddle
width tried (`test_inverse_tables_are_the_forward_ones_conjugated`). So each stage turns the forward
arithmetic into the inverse's with two moves and the forward ROMs: butterfly outputs 1 and 3 swap
(`W_4^{-qj} = W_4^{(4-q)j}`, the same products), and the rotation's twiddle has its imaginary part negated
(exact in `tw_t`). Every product and truncation is then the inverse build's. The `1/L` stays free: an
inverse frame's words hold the inverse build's bits, read through `inv_out_fmt`.

- `SsrFft(direction="runtime")` (`waveflow/dsp/ssr_fft/hw.py`): a third boundary port `s_dir`, 8 bits, one
  word a frame (bit 0 = inverse); RadixWord boundary only, `inverse=False`. `Geometry(runtime=True)`, its
  configuration key `_rt`, `Geometry.direction(inv)` for the model of one frame.
- HLS (`src/ssr_fft_tasks.h`): `ssr_fft_dir_in_task`, the input register that reads a frame's direction at
  its first word and writes a copy to each stage's FIFO; `ssr_fft_stage_dir_task` / `ssr_butterfly_dir`.
  The fixed bodies are untouched. Each direction FIFO's depth is the frames the chain up to its stage
  spans, plus two (L = 64: 4, 6, 7).
- **`SsrFft.build_fields` = `reorder, lanes, inverse, direction`.** It had none, so the system top's fresh
  copy (`system_top._rtl_of` -> `build_params`) built every `SsrFft` with the default `reorder`, `lanes` and
  `inverse`. Harmless until now, because those keep the port names; `direction` adds a port.

| L = 64, ping-pong | DSP | BRAM | FF | LUT | worst slack | frame interval (XSI) | first frame done |
|---|---|---|---|---|---|---|---|
| forward | 24 | 6 | 8306 | 10402 | +0.06 ns | 16 | 111 |
| inverse | 24 | 6 | 8303 | 10402 | +0.06 ns | 16 | 111 |
| runtime, F I I F I | 24 | 6 | 9168 (+10%) | 11537 (+11%) | +0.06 ns | **16** | 112 |

csynth estimates; the interval and the done cycle from the XSI run (8 frames back to back, every frame
bit-exact with its direction's golden). The extra cycle of latency has not been traced to a task.

Tests: `tests/dsp/ssr_fft/test_direction.py` (40: the table identity, the hardware's stage formula against
the fixed stage at L 16-1024 including the most negative input, the end-conjugation counter-example, pysim
in both reorders and both pysim modes against the golden and against the two fixed builds' pysim,
structure, refusals); `tests/dsp/ssr_fft/test_xsi.py` gains `pingpong-runtime` (3 gates).
`WANT_XSI_GATES` 269 -> 272. Guide: `dsp/ssr_fft/parameters.md` (a section and a row), `interfaces.md`
(`s_dir`), `synthesis.md` (the stage body, the resource table), `timing.md`, `systems/library.md`.

Not done: only L = 64 at RTL; lengths 256-4096 are pysim-only for the runtime build.

## Stage 4 record (2026-11-21): the concept page

`docs/guide/systems/shared_kernels.md` (*Sharing a kernel*, under *Waveflow Architecture*, between
*Command-response* and *Library blocks*): replicate first; the four questions; the six options; the five
return-path rules; the blocks (`StreamArbiter`, `StreamDemux`, `SsrFft(direction="runtime")`, `wait_any`) and
the tag beside K; a cycle inside a composite, from the ring stub's findings (the generator needs nothing,
end-to-end credit vs `CreditStreamIF`, depths from the credit, feedback first, output order independent of
timing, a deadlock looks like success, its pinned RTL numbers); modelling a polling stage with `wait_any`;
why not AMD's switch or `streamNToOne` (D6); and the extension rule. Linked from `kernels.md` (a composite
need not be a line), `streams.md` (*Between kernels*), `command_response.md` (one kernel, several clients),
`dsp/ssr_fft/parameters.md`. Glossary: a *Sharing a kernel* table -- shared kernel, client, arbiter, demux,
tag, credit (end to end), slot, feedback frame. `waveflow kb search` returns the page first for "one FFT
shared by several channels with feedback". The example section waits for Stage 5.

## Stage 5 record (2026-11-21): the example `os_filterbank` -- pysim and RTL

`examples/os_filterbank/` (curated: `docs/examples/os_filterbank/index.md`, *One FFT shared by every
channel*, in the examples index after markov). Three channels, overlap-save at `L = 64`, `M = 17` taps
(`P = 16` kept, `B = 48` new a block), on one `SsrFft(direction="runtime")`:

- `front_c` (one a channel, `ch` a template argument so each has its own statics): a credit (`CR = 2`), the
  block read whole, then `Tag` + overlap + new words to the arbiter;
- `StreamArbiter` (4 inputs, priority, input 0 the feedback, 2-burst frames);
- `pre`: the arbiter's index sets `s_dir` (input 0 = inverse), the tag goes beside the FFT, the words in;
- `post`: the three filters loaded once into slots (`s_coef`), then pass 0 multiplies (`ssr_cmul` into the
  FFT's input format) and sends the product back whole, pass 1 answers the block, a `Status` after a job's
  last block, and a credit;
- three `StreamDemux(route="header")` on the `ch` field: answers, statuses, credits. Each channel has its own
  output and status port, so every RTL recording is comparable although the channels finish out of order.

The golden is the FFT's model forward, the `complexMultiply` model, the model inverse; it is within 0.35% of
`np.convolve` in floating point (`float_check`).

| gate | result |
|---|---|
| pysim: the bank on `test` / `long`, a front alone, `post` alone | bit-exact; every block and status counted |
| pysim: credit made ineffective, loop links shortened | some jobs never answered; the run returns cleanly |
| RTL, `test`: 13 blocks, 26 FFT frames | clean compare; last status per channel 821 / 1067 / 888; FFT busy 39% |
| RTL, `long`: 36 blocks, 72 FFT frames | clean compare; 2349 / 2371 / 2424; FFT busy 48% |
| RTL: a front alone, `post` alone | clean compare |
| csynth, the bank (xczu48dr at 250 MHz) | 40 DSP, 80 BRAM, 16193 FF, 19243 LUT; worst slack 0.00 ns |

Vitis 2025.1 throughout. `post` takes 43 cycles a frame against the FFT's 16 (read and multiply, then write
the product back): it, not the FFT, sets the rate. Splitting it is noted on the page as the next step, not
done.

**Found and fixed on the way (framework):**

- **`StreamDemux(route="header")` read a word wider than 64 bits wrong in pysim** (`index_of` took a chunk
  row as a scalar). It now joins the chunks; `test_the_header_demux_reads_its_field_in_a_word_wider_than_64_bits`.
- **A system top with ports of several widths left a narrow output's upper bits undriven.** The stream nets
  are the widest port's width (`HwSystem.data_width`); a 32-bit status port beside 184-bit answers drove only
  its low 32 bits, and the XSI testbench refused the X in the first accepted beat. `system_top._instance` now
  connects a narrower output to the low bits and drives the rest 0
  (`test_the_system_top_drives_a_narrow_output_in_full`). The same applies to every class with
  `endpoint_port_widths` (`SystolicEngine`, `VecUnit`): their RTL gates re-run after the change,
  `tests/examples/test_systolic.py` (4) and `tests/vecunit/test_unit_xsi.py` (7), all pass.
- **A leaf with ports of several widths must set `endpoint_port_widths`**, or the system flow describes its
  ports at the system's one width while its own top (built with `port_widths`) has the real ones: `post`
  alone failed the same way until it was set. Nothing compares the two; a check that the csynth'd ports
  match the spec the system top is built from would catch it.
- **A system top must not be named as its kernel** (`os_filterbank` around `os_filterbank`: "illegal
  recursive instantiation" at elaboration). Named `*_top`, as the ring stub's; the generator could refuse it.

Tests: `tests/examples/test_os_filterbank.py` (10 pysim, 6 RTL gates); `WANT_XSI_GATES` 272 -> 278.
`CLAUDE.md`'s curated count 28 -> 29.

## Stage 6 record (2026-11-21): the skill and the frame

- **The skill** `waveflow/skills/waveflow-shared-kernel/SKILL.md` (copied to `.claude/skills/` and
  `.agents/skills/` with `waveflow skills install --dir . --force`). It points to the page and the example
  and does not restate them: a table of which page to read before which step, the reference design
  (`os_filterbank`, with `ring_stub` for the bare mechanics), the extension rule, and a checklist.
- **Hand-offs**: `waveflow-architecture` (step 2: one expensive kernel used by several channels, jobs or
  passes, or fed back -> `waveflow-shared-kernel`; step 6's list), `waveflow-pipeline` (its description and
  body). Step 2's example of a missing block ("a stream arbiter, an FFT whose direction changes per frame")
  is gone: both exist now.
- **The frame** `waveflow/mcp/frames/stream_shared/` (D2): `frame.toml` (choose_when against
  `stream_pipeline`: does any stage take input from more than one producer, or feed a stage before it?),
  `frame.md`, `process.md`, one prompt (`01_autocorrelation_bank.md`: FFT, `|X|^2`, inverse FFT per channel
  on one FFT -- a practice spec, not the held-out problem). References `os_filterbank`, `block_eq`.
- **Triggers**: `evaluation/skill_tests/shared_kernel/trigger_prompts.md`, four should-load and four
  should-not, run with every shipped skill installed so `waveflow-pipeline` competes.
  Run 2026-11-21 (`waveflow blind-test`, Claude Code's default model, Opus 5.5): **8 of 8 right.** Each
  should-load prompt loaded `waveflow-shared-kernel` at its first tool call; the near misses loaded
  `waveflow-start` then `waveflow-pipeline` (split a FIR), `waveflow-pipeline` (a copy each), `waveflow-schema`
  (a schema), and nothing (a NoC arbiter). One run each: a first check, not a rate.

## Hand-off: continuing on the lab machine (2026-11-21)

Written for the next agent, on a machine with Vitis HLS / Vivado 2025.1. Everything is on branch
`claude/waveflow-gradient-descent-test-3itqxa` (pushed; not merged to `main`). Read this plan from the top
first: the design space, D1-D6, and the Stage 0 and 1 records.

**0. Set up.** The venv, the Vitis / Vivado paths and any machine setup are in that machine's
`CLAUDE.local.md`, not here.

```bash
git fetch origin claude/waveflow-gradient-descent-test-3itqxa
git checkout claude/waveflow-gradient-descent-test-3itqxa
pip install -e ".[dev]"
test_amd_tools                                                       # Vitis found?  else set WAVEFLOW_VITIS_PATH
pytest tests/examples/test_ring_stub.py -m "not xsi"                 # 7 pass: pysim + codegen
```

**1. Finish Stage 1: the stub at RTL.** *Done 2026-11-21 on the developer laptop (Vitis / Vivado 2025.1);
see "At RTL" in the Stage 1 record. Kept for reference.* Run, in this order (K and Post alone first, so a failure in the
composite is known to be the loop's):

```bash
python -m examples.ring_stub.ring_stub_build --through inc_compare
python -m examples.ring_stub.ring_stub_build --through post_compare
python -m examples.ring_stub.ring_stub_build                          # the composite, 10 jobs
python -m examples.ring_stub.ring_stub_build --through long_compare   # 40 jobs
pytest tests/examples/test_ring_stub.py -m xsi                        # the four gates; a skip is a failure
```

Pass = every `compare` clean (RTL recordings equal to pysim's, word for word, TLAST included). Then:

- record in the Stage 1 record, with "Vitis 2025.1" beside every number: the csynth result of each top
  (II of the copy and inc loops, any warnings about the `empty()` checks or the `static` credit), the cycle
  each scenario's last `m_status` word appeared, and each top's resources from `kernel_resources`;
- add an exact cycle-count assertion for the composite to `tests/examples/test_ring_stub.py`, in the
  pattern of the other XSI gates (CLAUDE.md lists them), so a change in the loop's timing is seen;
- set the Status line: Stage 1 DONE.

If something fails, in order of likelihood:

- **The composite's run ends with words missing**: a deadlock at RTL. Compare against pysim's counts
  first. Look for Vitis putting a stream read and a write in one state (`docs/guide/rf/rfshotbuf/
  tx_internal.md`, the `read_nb` poll-loop finding), for a FIFO Vitis sized differently from `DEPTH`
  (`ring_stub.py`), and for the credit's reset (`#pragma HLS reset variable=credit` in
  `src/ring_mux_task.h`; `CLAUDE.md` notes `config_rtl -reset state`). If the bound is simply too short, raise
  `n_cycles` in `ring_stub_build.py` (20000 / 60000) and say so in the record.
- **K or Post alone differ from pysim**: a body bug. Their g++ run matched the golden here; csim the body
  if needed.
- **The generator or csynth refuses the cycle**: that is the finding Stage 1 exists for. Record the exact
  error, then fix it in the generator as an extension (AGENTS.md: propose, then build it in this repo with a
  test), not around it.

**2. Start Stage 2 (allowed before step 1 is done, D5).** *Done 2026-11-21 on the developer laptop (Vitis /
Vivado 2025.1); see the Stage 2 record. Kept for reference.* `StreamArbiter` and `StreamDemux` in
`waveflow/streams/`, each in the four parts of `StreamBroadcast`: the model, a templated HLS body in `src/`,
a build step, an RTL gate in `waveflow/streams/rtl.py`; then a page under `docs/guide/streams/` and a row in
its index. Start from `examples/ring_stub/src/ring_mux_task.h`: its feedback-first frame copy is the
arbiter's core, without the credit and the tag format (those stay in a design). In order:

1. the pysim primitive a polling body needs: block until any of several inputs holds a burst (Stage 1
   record, first gap). Put it on the stream slave or `FreeRunMod`; test it; then rewrite the stub's `Mux` as
   one `run_iter` in the HLS body's order, and keep its tests green;
2. `StreamArbiter`: n framed inputs to one, whole frames, strict priority or round-robin (D4), the winner's
   index on an `m_sel` side stream (D3);
3. `StreamDemux`: one framed stream to n by an `s_sel` side stream, or by a header field (D3);
4. their harness scenarios fixed in order (the second gap); their RTL gates;
5. the vendor check of D6 (`streamNToOne`'s granularity), recorded on the arbiter's page.

Then Stage 3 (`SsrFft` direction per frame). Keep this plan's records current as each stage lands; open a
pull request only when the user asks.

## Stage 7 record (2026-11-22): the held-out evaluation

Run from the planning session at ead8011 (Stages 0-6), from outside the repository; the prompt, the
rubric and every run's report are kept there, not here. Four chat runs (two Opus 5.5, two Sonnet 5.5)
and one build run (Opus 5.5), fresh agents with the Waveflow skills, told not to read `plans/`.

- **Every chat run reached the same right architecture**, `os_filterbank`'s shape: per-client fronts
  holding a credit, a `StreamArbiter` in priority with the feedback on input 0, one
  `SsrFft(direction="runtime")`, the clients' data resident beside the FFT, a controller with one state
  machine per client (polling with `wait_any`), and `StreamDemux` by header to a port per client so RTL
  and pysim compare. The maths was right in all four. None announced a feedback frame early (the
  baseline's flaw); none used a hand-written mux or the conj trick. **None considered replicating the
  FFT**, which the skill's checklist asks for.
- **The build run took 14 minutes** from an empty folder (`waveflow init`) to 11 passing pysim tests:
  the golden, each stage alone, the composite on staggered clients finishing out of order with every
  count and `tx_id` asserted and all frames through the one FFT, a deadlock witness, and the generated
  top. The FFT was 85% busy while four clients were in the loop. HLS bodies for two of its three stages
  and any RTL were left undone (no tools).
- **Against the baselines (2026-11-21):** before Stage 0 the Waveflow agent hand-wrote the FFT's mux,
  called a cycle in a composite "untested territory" and proposed no extension; the Vitis-only agent
  had the architecture on paper and "csim, then cosim" as its verification.
- Caveats: two runs a model is a sample, not a rate; the chat runs adapted `os_filterbank` closely,
  which is what a reference design is for, so this measures adaptation more than invention.

## The Stage 7 fixes (2026-11-22)

What the build run found, fixed in the framework:

1. **pysim could pass a loop that RTL deadlocks** (`waveflow/hw/loop_check.py`). A blocking write of a
   burst larger than its link's depth is admitted whole once the link is empty, so the writer finishes
   with nobody reading; RTL stalls it after `depth` words (shown directly: a 16-word write into a
   depth-4 link, no reader, completes). On a cycle that hides deadlocks. Every link now records its
   largest burst, and after a pysim run each composite warns (`LoopDepthWarning`, and
   `loop_depth_hazards`) for a link on one of its cycles whose burst exceeded its depth. A reader that
   drains whenever its output has room (`drains_inputs = True`: `SsrFft`, `StreamFramer`) is exempt.
   The burst model itself is unchanged: making it word-granular would move every pinned timing.
   The whole non-vendor suite runs clean with the warning as an error, except the two deadlock-witness
   tests, which now expect it; the ring stub with its feedback link cut to 8 words answers every job in
   pysim and is now flagged.
2. **A schema's dependency header could be silently missing** (`DataSchemaStep.resolve_deps`). A
   dependency with no step (an array field's `Int32Array`) is now warned
   (`MissingSchemaHeaderWarning`) instead of found at csynth. A warning, not an error: a step resolved
   alone, or a header another build writes, is legitimate.
3. **An input's first burst could not arrive late.** `StreamBurst(words, gap_before=n)` states a
   burst's arrival in a workload; the harness turns a port's gaps into its driver's `start_gap_cycles`
   (new, before the first burst) and `burst_gaps`. Both backends: the pysim `StreamDriver` and the XSI
   `AxisMaster` (`xsi_bfm.h`, and the two committed copies in `examples/interleaver/xsi/` and
   `examples/mem_copy/xsi/`). Not compiled here; a test checks the C++ declares the member.
4. **Nothing could route by frame after `SsrFft`**, whose output carries no TLAST, so its post stage
   had to do everything serially and became the bottleneck. New block `StreamFramer`
   (`waveflow/streams/framer.py`, `src/stream_framer_task.h`, `StreamFramerStep`, `rtl.make("framer")`,
   `docs/guide/streams/framer.md`): TLAST every `frame_words` words. 10 pysim tests; its XSI gate is
   written and counted (`WANT_XSI_GATES` 278 -> 279) but not run here.
5. **Docs:** the shared-kernels page now states that serial store and forward halves the shared
   unit's throughput (overlap receive and send; a framer and a demux split the feedback from the
   answers), and the loop-depth rule with its check; the harness page states `gap_before`.

Pending a machine with Vitis 2025.1 / Vivado: `pytest tests/streams/test_framer.py -m xsi`, and an XSI
run with a first-burst gap set (`start_gap_cycles`) to check the `AxisMaster` change, for example the
ring stub with a delayed first job.

