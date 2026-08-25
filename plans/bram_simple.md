# `bram_simple` — the standalone shared-memory example

**Status: DESIGNED HERE, NOTHING BUILT.** Started 2026-08-25. This file owns `examples/bram_simple`
and its documentation — the domain-free worked example for `BramIF`.

Separate from `plans/rtl_module.md`, which owns the **mechanism** (`rtl_module()`, `add_rtl_if`, the
wrapper) and whose S4 is blocked on an unrelated `RxCmd` question. This is an **example and docs**
deliverable with learning objectives, for a reader learning shared memory rather than one extending
the wrapper generator.

---

## Next session starts here — Stage 1

```
claude "Read plans/bram_simple.md, section 'Stage 1 — the design', and build it.
        Scenario zero is the WITNESS and its numbers are not negotiable — see
        'What must not be lost'."
```

---

## Why it exists

`BramIF` is useful well beyond RF, and today the only way to see it used is `examples/bram_toy` — a
minimal reproduction with no documentation page and no pysim test — or `examples/rf_shot_buf`, which
couples it to a converter. Someone who wants "shared memory between two tasks" for a non-RF design
should not have to read an RF example.

**The duplication with `rf_shot_buf` is deliberate and a feature**: seeing the same primitive reused
in two unrelated designs is the point, not a cost. Say so on the page rather than pretending each is
unique.

### And the toy proved too weak to be the witness it was meant to be

`bram_toy` fills 256 of 1024 words at **16 bits** — byte addresses 0…510, no wrap — so it stayed
green through a defect that had every BRAM design in the repo mis-addressed (see
`fix(build): the BRAM wrapper fed a BYTE address to a word-addressed memory`, 2026-08-24). Vitis
addresses a `mode=bram` port in bytes; the wrapper had wired that straight to a word-addressed
memory; and **the scaling is consistent, so a design round-trips perfectly right up to the point
where its memory wraps.**

The lesson is not "make the toy bigger". It is that the *primitive's own example* should exercise the
regime where the primitive's own convention can fail — which at 16 bits it never does.

## What must not be lost

**Scenario zero is the witness, and its numbers are not negotiable.**
`plans/witness/t2p_bram/` is four hand-written files that were csynthed and simulated **before any of
this infrastructure existed**: write `buf[i] = i + 100` for 256 samples, then read addresses
`0, 1, 7, 255, 128` and get back `100, 101, 107, 355, 228`.

That is the only gate in this repo checking Waveflow against something built independently of
Waveflow, and it must survive this rewrite. **The richer design subsumes it**: with
`write(wp, nwords, …)` and `read(rp, nwords)`, the witness is `wp=0, nwords=256` followed by five
one-word reads. Keep the gate exactly as it is and add the new scenarios beside it.

**A ramp, not a constant, and that is also not negotiable.** The likeliest failure is a read-latency
mismatch between the kernel's pragma and the memory, which shifts every value by one and would sail
through a constant check.

## The design

Two free-running tasks over one true-dual-port memory, now **command-driven**:

```
  cmd_w ──▶ ┌───────────┐ ──buf_w──▶ ┌──────────┐
  data  ──▶ │ WriteTask │            │ T2pBram  │   hand-written Verilog,
            └───────────┘ ──resp_w─▶ │          │   BESIDE the kernel
  cmd_r ──▶ ┌───────────┐ ──buf_r──▶ │          │
            │ ReadTask  │ ◀──────────└──────────┘
            └───────────┘ ──data_r─▶
```

- **`WriteTask`** takes `(wp, nwords)` plus `nwords` payload words, writes them from `wp`, and
  answers **one `WriteResp`** per command. The response is the whole reason it exists: a write has no
  return path, so a **short payload otherwise completes silently** and leaves the memory
  half-written.
- **`ReadTask`** takes `(rp, nwords)`, streams the words back, and answers **one `ReadResp`**.

### Both commands answer, and `ReadResp` is not there for symmetry

A `WriteResp` is obvious — a write has no return path, so a **short payload otherwise completes
silently** and leaves the memory half-written.

`ReadResp` needs its own argument, and it has one: **a refused read returns zero words, and zero
words is indistinguishable from "not yet" on a stream.** A consumer waiting for `nwords` that will
never arrive does not see an error; it sees a stream that has gone quiet. So the only way to learn
that a command was rejected is a channel that answers whether or not there is data — which is
precisely what the data stream cannot be.

**Status carries the range check.** A command whose range leaves the memory —
`wp + nwords > depth`, or `rp + nwords > depth` — is **refused**, not wrapped, and the response says
so. Refusing is the right choice for this example: a range that overruns is a caller error, and a
silent wrap would hand back plausible data from the wrong place. (Contrast `RfShotBuf` and the RF
buffers, where a *circular* pointer is the whole point — that difference is worth a sentence on the
page.)

> **The bounds check would NOT have caught the addressing bug**, and the page should say so rather
> than let a reader assume the guard is broader than it is. The check is in **word** units — the
> design's units — while the byte/word scaling defect lived *below* it, in the wrapper. A command
> reading words 0…255 of 1024 passes the range check and still aliased. Two different failures, two
> different guards: the range check is the caller's, and
> `test_the_wrapper_undoes_the_shift_vitis_actually_emits` is the convention's.

### Overlap is the point, and it is deliberate

The scenario runs in two phases, and the second is where the teaching is:

1. **Phase 1 — no overlap.** The witness. Load, then read. Nothing is live at the same time.
2. **Phase 2 — deliberate overlap on disjoint ranges.** Write 64…127 while a read of 0…63 is
   outstanding.

Phase 2 is what a **true-dual-port** memory is for, and it is also where "no hazard" stops being
structural and becomes **conventional** — the design permits overlap, so keeping the ranges disjoint
is the caller's job. `bram_t2p.v` is what catches a mistake:

```verilog
$error("bram_t2p: read-during-write collision at addr %0d", a_addr[AW-1:0]);
```

Say that on the page. It is exactly the line `docs/guide/rf/choosing.md` draws between shot and
streaming semantics, made concrete in the simplest possible design — and a reader meets it *labelled*
rather than stumbling into it.

### The geometry has to wrap

**At least one gated configuration at 64-bit words**, so the byte scaling is 8× and a wrong wrapper
aliases immediately. `bitwidth` and `depth` are already parameters on `bram_toy`, so this is a
configuration plus a gate, not a rewrite. This is the example's own guard for the example's own
convention, and it is what `bram_toy` could not do.

## Learning objectives

For `docs/examples/bram_simple/index.md`. Keep them one line each; the detail belongs on the pages
that walk the code.

1. Create a BRAM and connect `HwModule`s through a `BramIF`.
2. Read and write concurrently from two modules, and see what guards the hazard.
3. Run a Python simulation of the design and extract timing.
4. **Add timing for the BRAM read path in the Python model** — see below.
5. Run an RTL simulation in which the memory is the real hand-written Verilog.
6. Verify throughput and overlap with a timing diagram from the RTL trace.
7. Compare the two backends' timing.

### Two of these were stated wrongly first, and the corrections are the content

**There is no BRAM XSI object, and that is the stronger story.** No model in `waveflow/build/xsi/`
mentions BRAM. In XSI the memory is **`bram_t2p.v` itself, compiled into the simulation** beside the
synthesized kernel — visible in `rtl_<top>.f`. So there is no second implementation that could
disagree with the first, which is `docs/guide/interface/bram.md`'s point that a hand-written memory is
*more* verifiable than an emulated one. Expect "where is the BRAM BFM?" to be a reader's first
question, and answer it on the page.

**"Compare the timing" needs care, because a `BramIF` access costs nothing in pysim** — by design:

> *The access is untimed in pysim on purpose: a BRAM answer is deterministic, unarbitrated and one
> cycle, so a discrete-event model of it would add a timestep and no fidelity.*

`mem_read` / `mem_write` are plain methods rather than generators, and **the absence of the `yield` is
the interface stating that no time passes.** So the comparison is between the **tasks'** timing, not
the memory's — with the memory free in one backend and a published latency in the other.

### Objective 4, which is the interesting one

The two directions are **not symmetric**, and the example should show why:

- **Writes are already right.** `run_iter` spends its time in `yield from ...get()` — one stream word
  per firing — and the write lands in that same firing. At RTL the task is II=1 and the word arrives
  once a cycle. The memory genuinely contributes nothing.
- **Reads carry a latency.** `bram_t2p.v` publishes `localparam READ_LATENCY = 1`, and pysim's
  `mem_read` returns instantly, so a task that reads and immediately uses the value sees it a cycle
  earlier than RTL does.

> **The read latency is invisible in throughput and visible in latency.** At II=1 a pipelined reader
> still answers one word per cycle — the pipeline hides it. What moves is *when the first answer
> appears*. A model that omits it matches RTL on rate and is off by `READ_LATENCY` on the first word.

That is the latency-versus-throughput distinction, falling out of a two-line change, and it gives the
timing-diagram objective something concrete to show: two lanes at the same slope, offset at the start.

**And the number is not to be invented.** `BramIFMaster.read_latency` **raises when unbound**,
precisely so a latency that cannot be traced to a memory's published value never reaches a model. A
student writing `yield self.timeout(1)` with a hard-coded `1` is doing the thing the framework
refuses to do. Read it from `self.buf_r.read_latency`.

*(Open: should `BramIFMaster` grow a timed `mem_read_timed()` generator so the number cannot be
hand-copied at all? For a teaching example, explicit is probably better — the student should see the
cost being paid. Decide in Stage 3.)*

*(A third response status is worth considering once the design exists: a read or write whose range is
legal but whose payload arrives short. Stage 1 needs only OK and OUT_OF_RANGE; do not invent more
before a scenario needs them.)*

## Stages

**Stage 1 — the design.** `examples/bram_simple`, command-driven, with `WriteResp` and `ReadResp`.
Scenario zero is the witness, unchanged. **Gate:** pysim byte-identical, and the witness's five
numbers — **plus the two refusals**, which are the responses earning their keep: a short write payload
is reported rather than silently half-applied, and an out-of-range read is reported rather than
leaving the consumer waiting on a stream that has gone quiet.

**Stage 2 — the wrapping geometry and the overlap phase.** A 64-bit configuration that wraps, and
phase 2's disjoint-range overlap. **Gate:** csynth, XSI, an exact cycle count, and a deliberate
negative — a *non*-disjoint overlap must trip `bram_t2p.v`'s `$error`, asserted rather than assumed.

**Stage 3 — timing in the Python model.** Objective 4. **Gate:** the first-word offset between
backends is exactly `read_latency`, and the throughputs match.

**Stage 4 — the docs.** `docs/examples/bram_simple/`, as a **page set**, on the shape
`docs/examples/shared_mem/` already uses — it is the closest analogue, being the other memory
example:

| # | page | what it covers |
|---|---|---|
| 1 | `index.md` | the learning objectives, and what the example is for |
| 2 | `overview.md` | what a BRAM is here, and the topology — the `aximm.md` role in `shared_mem` |
| 3 | `python.md` | the Python model, and **how** the read-path delay is added (objective 4's code) |
| 4 | `pysim.md` | running it, generating test vectors, recording the timing |
| 5 | `codegen.md` | producing the RTL — the kernel, `bram_t2p.v`, and the wrapper that joins them |
| 6 | `rtlsim.md` | running XSI and producing the trace |
| 7 | `timing.md` | **reading** the trace: the activity diagram, and the comparison back to pysim |

The `python.md` / `timing.md` split follows what `shared_mem` and `memcpy` already do — the model
page shows the code, the timing page reads the run. Both of those `timing.md`s also cover **how the
figures are committed and refreshed**, which this one needs too, since the activity diagram is a
generated artifact.

**`timing.md` has a sharper job here than in the examples it copies.** `memcpy`'s ends with a section
titled *"And the pysim matches — for free."* This one cannot say that, and the reason is objective 4:
the throughputs match for free, and the **first word does not** — it is off by exactly
`READ_LATENCY` unless the model pays it. Draw the contrast explicitly; a reader arriving from
`memcpy` will expect the free match and should be told why a memory is different from a bus.

**Mermaid for the topology.** It is enabled site-wide (`_config.yml`: `mermaid: version: 10.9.1`,
client-side from a CDN, no build plugin) and already used on ten pages — `memcpy`, `interleaver`,
`firblock`. The repo's division is worth keeping: **Mermaid for topology** (who talks to whom) and
**committed TikZ→SVG for claims** (the structural assertions in `guide/rf/figures/`, whose README
argues that a generated artifact cannot drift from what it describes). A topology sketch is the first
kind.

**And the addressing convention into `docs/guide/interface/bram.md`**, which is written down nowhere
today: Vitis byte-addresses a `mode=bram` port, the wrapper undoes it, the WEN is a byte-enable
vector, and a design that never wraps will not notice if any of that is wrong. That belongs in the
*interface* guide rather than this example, because it binds anyone using `BramIF` at all.

**Retire `bram_toy` into this**, rather than keeping two. Two examples would mean maintaining the
witness inside a design nobody reads.

## Traps, carried forward

- **The venv is a sibling: `../pysilicon-venv`.**
- **Baselines:** dev loop 6 failures (`test_dataschema_poly` + 5 in `tests/poly/test_timing_analysis.py`);
  `-m xsi` has its own pre-existing failure, `test_fir_block_xsi`
  (`block 0 word 0: 0x00000000 != golden 0x0dab0666`). **0 skipped is the number to check.** Do not
  pipe pytest through `tail` — you get `tail`'s exit code.
- **`add_rtl_mod` not `add_comp`; `add_rtl_if` not `add_if`.** A `BramIF` in the `add_if` registry
  makes the kernel's memory ports disappear into a FIFO.
- **`mode=bram` on an unsized pointer degrades to an `ap_vld` scalar silently.** Assert the port list.
- **Vitis alternates `_Pipeline_VITIS_LOOP_<line>_<n>` and a bare `_Pipeline_<n>`.** A glob matching
  one spelling skips silently; it has cost time twice.
