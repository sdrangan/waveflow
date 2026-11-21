# Plan: sharing a kernel -- arbiter, tags, credits, feedback -- and the guide, skill and example that teach it

**Status: ACCEPTED 2026-11-21 (decisions D1-D4 as proposed). Stage 0 DONE 2026-11-21; Stage 1 pysim and codegen DONE, RTL pending a machine with Vitis 2025.1 (records at the end).**

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

Record the cycle counts (Vitis 2025.1) here. Stage 2's pysim and HLS work may start before this (decision D5); only its RTL gate waits on it.

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

**1. Finish Stage 1: the stub at RTL.** Run, in this order (K and Post alone first, so a failure in the
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

**2. Start Stage 2 (allowed before step 1 is done, D5).** `StreamArbiter` and `StreamDemux` in
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

