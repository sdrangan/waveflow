# `bram_access` — the standalone shared-memory example

**Status: DESIGNED HERE, NOTHING BUILT.** Started 2026-08-25. This file owns `examples/bram_access`
and its documentation — the domain-free worked example for `BramIF`.

Separate from `plans/rtl_module.md`, which owns the **mechanism** (`rtl_module()`, `add_rtl_if`, the
wrapper) and whose S4 is blocked on an unrelated `RxCmd` question. This is an **example and docs**
deliverable with learning objectives, for a reader learning shared memory rather than one extending
the wrapper generator.

---

## Next session starts here — Stage 1

```
claude "Read plans/bram_access.md, section 'Stage 1 — the design', and build it.
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

### The four messages, and they are `DataList`s

**Confirmed 2026-08-26.** Four messages, each a `DataList` with an `include_filename` so the Python
and the generated C++ header share one field layout:

| message | fields |
|---|---|
| `WriteCmd`  | `tid`, `nsamp`, `waddr` |
| `WriteResp` | `tid`, `status` |
| `ReadCmd`   | `tid`, `nsamp`, `raddr` |
| `ReadResp`  | `tid`, `status` |

**`status` is an `EnumField`, never a hand-coded integer.** An `IntEnum` plus
`EnumField.specialize(enum_type=..., bitwidth=...)`, exactly as `examples/fir_block/fir_block.py`
does with `FirOp` / `FirOpField`. A bare `1` in a response is a number nothing can name; a member is
one the header, the model and the test all spell the same way.

`tid` on all four is what makes a response usable from a second thread — a host correlates a reply to
the command it issued instead of inferring from ordering. It is the same reason `rf_tx_stream.TxResp`
carries one.

#### Read them with `get(Schema)` — this is not a style note

> **A command is read in ONE call.** `cmd = yield from self.cmd_w.get(WriteCmd)`.
> **Never** `wp = yield from _word(...)` then `n = yield from _word(...)`.

This is a **recurring** failure, called out by the user on 2026-08-25: *"Claude always ignores
DataLists and always ignores in-built serialization. If you tell it to use a DataList it may use it.
But it will always manually unpack it."* Stage 1 of this very plan did exactly that, because the plan
said "takes `(wp, nwords)`" in prose and never named the mechanism.

Two things that make it worse than untidy:

- The schema's `include_filename` **generates the C++ header**. Hand-unpacking authors the field
  layout a second time, in a place nothing checks against the first — the same defect as hand-rolled
  element packing, one level up.
- **Declaring the `DataList` is not enough.** The reported pattern is declaring it and then unpacking
  by hand anyway. Check the read side.

**Watch the vectors, because they can force the anti-pattern.** Stage 1 wrote scenario vectors *one
word per burst*, and since a pysim slave dequeues a whole burst per `get` and discards the remainder,
that framing made word-at-a-time the only thing that worked. **A command is one burst.** If the
vectors disagree, fix the vectors.

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

For `docs/examples/bram_access/index.md`. Keep them one line each; the detail belongs on the pages
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

**Stage 1 — the design.** `examples/bram_access`, command-driven, with `WriteResp` and `ReadResp`.
Scenario zero is the witness, unchanged. **Gate:** pysim byte-identical, and the witness's five
numbers — **plus the two refusals**, which are the responses earning their keep: a short write payload
is reported rather than silently half-applied, and an out-of-range read is reported rather than
leaving the consumer waiting on a stream that has gone quiet.

**Stage 2 — the wrapping geometry and the overlap phase.** A 64-bit configuration that wraps, and
phase 2's disjoint-range overlap. **Gate:** csynth, XSI, an exact cycle count, and a deliberate
negative — a *non*-disjoint overlap must be **detected in the VCD trace**, asserted rather than
assumed. (The gate was first written against `bram_t2p.v`'s `$error`; that is unobservable in this
flow — see *DECIDED 2026-08-25*.)

**Stage 3 — timing in the Python model.** Objective 4. **Gate:** the first-word offset between
backends is exactly `read_latency`, and the throughputs match. **DONE 2026-08-25** — see
*Stages 2 and 3 are closed*.

**Stage 4 — the docs.** `docs/examples/bram_access/`, as a **page set**, on the shape
`docs/examples/shared_mem/` already uses — it is the closest analogue, being the other memory
example:

* `index.md` — the learning objectives, and what the example is for
* ~~`overview.md`~~ — DELETED by S5e: five of its seven sections were already in
  `docs/guide/interface/bram.md` with the context that made them make sense, and restating them
  in the example is what made them read as arcana. Its two example-specific sections moved to
  `python.md` (the transactions) and `timing.md` (the overlap).
* `python.md` — the Python model, and **how** the read-path delay is added (objective 4's code)
* `pysim.md` — running it, generating test vectors, recording the timing
* `codegen.md` — producing the RTL: the kernel, `bram_t2p.v`, and the wrapper that joins them
* `rtlsim.md` — running XSI and producing the trace
* `timing.md` — **reading** the trace: the activity diagram, and the comparison back to pysim

(Bulleted, not numbered: `rfdc/index.md`'s numbered table went stale twice because adding a page
meant renumbering.)

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

## Stage 2 WAS blocked on its negative gate — measured 2026-08-25

Stage 1 passed and is committed.  Stage 2's **positive** gates all pass and are measured:
csynth clean, `write_payload` and `read_payload` both **II=1** read from the csynth XML, the witness's
five values bit-exact through real Verilog at 64-bit words, last read word at cycle **386**, the
overlap proved from arrival cycles (the phase-2 write's response lands at 366, inside the reader's
313…376 window), and the reader's 64-word burst arriving one word per cycle with no gap.

Its **negative** gate cannot be met as written, and the reason is not the design.

### The collision happens; the `$error` cannot be seen

`collision_scenario()` was built and **measured with a temporary `$fwrite` probe inside a scratch
copy of `bram_t2p.v`**: 24 events where `a_en && |a_we && b_en && a_addr == b_addr`, with the
relative address offset swept from −8 to +9 across the run.  So `bram_t2p.v`'s `$error` fired
24 times.  Nothing observed it.

**In this XSI flow (Vivado 2025.1, `xelab -dll` + the C++ loader) RTL text output is discarded.**
Measured four ways:

* `$display` in an `always` block — ~900 lines' worth — reaches neither stdout nor any file;
* an `initial $display` at time 0 — the same;
* `s_xsi_setup_info::logFileName`, relative and absolute, produces **no file**.  It does change the
  kernel's invocation (`xsim.dir/<top>/xsimkernel.log` shows `-nolog` become `-log <name>`), and
  still nothing is written;
* an `$fwrite` to a file the Verilog opens itself **does** work — which is how the collision above
  was counted, and which is what proves the RTL is executing the code that would have printed.

### What this costs elsewhere

Two existing gates assert a string that **cannot appear**, and both read as positive evidence today:

```python
assert "read-during-write collision" not in out          # test_bram_toy_xsi.py
assert "read-during-write collision" not in out          # test_rf_shot_buf_xsi.py
```

`docs/guide/interface/bram.md` also presents the memory's `$error` as the guard that makes a
hand-written memory *more* verifiable than an emulated one.  That argument survives — the assertion
is real and it fires — but the sentence should say where the firing can be read, and today the answer
is "nowhere".

### DECIDED 2026-08-25 — gate the condition from the VCD, and correct what the guide claims

**Option 2, plus a correction that is larger than the gate.** Two facts found while deciding change
the shape of the choice:

**It is FIVE vacuous asserts, not two.** `assert "read-during-write collision" not in out` appears in
`test_bram_toy_xsi.py`, `test_rf_blk_delay_xsi.py`, `test_rf_samp_buf_rx_xsi.py`,
`test_rf_samp_buf_tx_xsi.py` and `test_rf_shot_buf_xsi.py` — five shipped XSI gates asserting the
absence of a string that **cannot appear**. That is this repo's own *"a check that silently stops
checking is worse than no check, because the green tick is then evidence of nothing"*, five times, and
it is live on `main`.

**And the byte-for-byte-from-the-witness property already does not hold.**
`waveflow/build/rtl/bram_t2p.v` is 43 lines to `plans/witness/t2p_bram/bram_t2p.v`'s 35 — it gained
the `localparam READ_LATENCY = 1` block that is the single source for the kernel's pragma. The
property was already traded once, for a good reason. So that objection to option 1 is weaker than it
looked; option 2 is chosen on its own merits rather than by default.

**What to build:**

1. **Gate the CONDITION from the trace.** `run.bat` already dumps `<top>_trace.vcd`,
   `waveflow/utils/vcd.py` exists, and there is committed-VCD precedent
   (`examples/shared_mem/vcd/dump.vcd`). Detect `a_en && |a_we && b_en && a_addr == b_addr` in Python.
   It checks the same fact the `$error` checks, touches no RTL, and **composes with Stage 3**, which
   needs the trace anyway.
2. ~~Delete all five vacuous asserts.~~ **DONE on `main`, 2026-08-25** —
   `test(xsi): remove five checks that could never fail`. Three of them were whole test functions
   whose entire body was the dead assertion; two were embedded and lost only the assertion. `-m xsi`
   went 57 → 54, 0 skipped. **Do not look for them; they are gone.**
3. **Correct `docs/guide/interface/bram.md`.** It presents the `$error` as *the* guard. The honest
   statement: the assertion is real and it fires, but **in the XSI flow nothing can read it**, so a
   user whose design collides gets no warning from that path. That is the finding here, and it is
   bigger than a test — the protection the guide promises does not exist in the flow this repo runs.

**Not option 3.** Deleting the asserts without replacing the check leaves both the gap and the
overclaiming guide.

### Deferred, deliberately: making the guard observable by construction

If user-facing protection matters — and it probably does — the durable fix is neither a print nor a
trace scan but a **sticky `collision` output on the memory**, exposed through the wrapper and readable
in *both* backends by construction. That is a real interface change with a blast radius across
`BramIF`, `wrapper_gen` and every existing wrapper, so it belongs to **`plans/rtl_module.md`** as its
own decision. Do not smuggle it into this stage.

Recorded here because the reasoning is fresh: what makes the current guard weak is not that it is an
assertion, it is that its only channel is text output — and one of the two backends discards text.

---

## Stages 2 and 3 are closed — measured 2026-08-25

**Stage 2's negative gate is built and fires**, by the route *DECIDED* below: the condition is read
out of the VCD instead of the `$error` being heard.

| | measured |
|---|---|
| `collision_scenario` | **24** read-during-write collisions, all on words 128…136 |
| scenario zero | **0** — the deliberate overlap really is disjoint in every cycle |
| tracing's cost | **none**: the traced run still ends at cycle **386**, the untraced number |

The 24 is the same count a temporary `$fwrite` probe inside `bram_t2p.v` counted before any of this
existed, which is the cross-check that the scan detects the events the `$error` fires on.

**It is a PAIR, not a check.** An empty scan is what a correct design, a renamed net, a dump that
never ran and a wrong scope all look like. So the clean run means something only because the dirty
run is asserted dirty in the same file, through the same scan.

**Stage 3 is done, and the number is measured twice from opposite directions.**

| | measured |
|---|---|
| RTL, off the memory's own pins | the answer appears at **exactly one** offset — 1 cycle — fitting all 77 reads; no other offset fits more than 4 |
| pysim, model off → on | the first returned word moves by **1** cycle, and by nothing else |
| cadence, both backends | **1** word per cycle, in the 64-word read, with and without the model |

The RTL half is not "the pragma agrees with the Verilog" — that is two files agreeing. It asks the
waveform at what distance from the address the answer appears, and requires the answer to be a
*single* offset. Single is only decidable because the payload is a **ramp**; a constant would make
every offset fit, which is the same failure the ramp prevents in the value check.

**What the two backends teach, and it is the sharp end of objective 4:** the throughputs match for
free and the **first word does not**. `docs/examples/memcpy`'s timing page can end with *"And the
pysim matches — for free"*; this one cannot, and the reason is one cycle of pipeline fill that a
memory charges and a bus does not.

### What was built

* `bram_hazard_manifest()` in `waveflow/build/wrapper_gen.py` — which wrapper wire carries each term
  of the memory's predicate, named by the emitter that made them rather than matched by substring.
* `waveflow/utils/bram_trace.py` — `find_read_during_write()`, `port_samples()`,
  `measured_read_latency()`. The last returns a **set** of offsets on purpose: one is the number,
  several means the scenario cannot tell them apart, none is a defect.
* `AddVcdTopStep` grew an optional `top`, because a wrapped design elaborates its **wrapper** and
  both the dumper's file name and the scope it names have to follow.
* `docs/guide/interface/bram.md` — the correction. The `$error` is real and fires and **cannot be
  heard**; the page said the opposite by implication.
* `docs/guide/comp_codegen/rtl_module.md` — *"nothing traces or times a wrapped design yet"* is no
  longer true. The first one did **not** need the scope prefix: what it reads are the wrapper's own
  wires.

---

## Stage 4 is closed — 2026-08-25

`docs/examples/bram_access/` ships as seven pages, `bram_toy` is retired, and the addressing
convention is written down in the interface guide where it binds everyone using `BramIF`.

### Two things the plan had wrong, corrected rather than absorbed

**The collision lands on words 128…135, not 128…136.** `collision_scenario()` writes `(128, 8)`, so
the range is `[128, 136)` — eight distinct words, and the measured hazards are on exactly those. The
count, 24, is right.

**The page list was a numbered table**, which is the shape this repo has already been bitten by
twice. It is a bullet list now, and so is the one on `index.md`.

### What is measured, and where each number came from

| claim | measured |
|---|---|
| kernel `BRAM_18K` | **0** — from `csynth.xml`, while the memory beside it is **4** RAMB18 by geometry |
| payload beats in vs memory writes | **332** vs **324** — the eight missing writes are the refused command, visible in the waveform |
| read enables vs words returned | **80** vs **73** — seven commands return data, each presenting one address past its range as the pipeline drains |
| VCD cycle index vs the sinks' | **+15**, on every one of the 73 words: the harness holds 16 reset edges, and the sinks count 1-based from the first post-reset cycle |
| collision scenario | **24** hazards on words 128…135 |
| scenario zero | **0** hazards |
| measured read latency | **{1}** — the unique offset explaining every read |
| pysim first word, model off → on | **+1** cycle; cadence **1** word/cycle either way, matching RTL |

The `+15` is worth carrying forward: a reader comparing the figure's cycle 401 against the gate's 386
would otherwise think something was wrong.

### The retirement, and what it cost

`examples/bram_toy` and `tests/examples/test_bram_toy_xsi.py` are gone. Nothing it asserted was lost:

* the witness's five values at RTL — now gated at 64 bits, where the addressing convention can
  actually fail, rather than at 16 where it cannot;
* the `mode=bram` port list, and the no-PIPO-gating check — both present in
  `test_bram_access_xsi.py`;
* `test_the_expected_values_are_the_witness_s` — present in `test_bram_access.py`.

`tests/build/test_wrapper_gen.py` was repointed from `BramToy` to `BramAccess`. Every assertion kept
its shape; the numbers moved with the geometry, and two got **stronger** — the WEN wire is checked at
8 bits rather than the 2 that happened to be right only at 16, and the address shift at `>> 3` rather
than `>> 1`.

Test counts move accordingly, and all of it is the retirement:

* dev loop **3085 → 3082**: −1 for `bram_toy`'s one unmarked test, −2 for its two parametrized cases
  in `test_xsi_workspace_copies.py` (which enumerates `examples/*/xsi`).
* `-m xsi` **65 → 62**: `bram_toy`'s three xsi-marked tests.

### How the pages are checked

Every fenced block on the seven pages is either **executed** and its output compared against the
block below it, or verified to be a **verbatim excerpt** of a named source file (modulo comment
elision). No block on any page is prose about code.

---

## The four messages are BUILT — 2026-08-26

All four are `DataList`s with an `include_filename`, `status` is an `EnumField` over `BramStatus`,
and **neither backend unpacks by hand**. The read side of both:

```python
cmd = yield from self.cmd_w.get(WriteCmd)      # pysim
```
```cpp
WriteCmd c;  c.read_stream<W>(cmd);            // the kernel
```

`tests/examples/test_bram_access.py::test_neither_backend_takes_a_message_apart_by_hand` asserts both
idioms are present and that `_word(self.cmd_w)` / `cmd.read()` are **not** — because declaring the
schema and then unpacking it anyway is the reported failure, and a test that only checks the
declaration would not have caught it.

### The field width was not specified, and 64 is what makes the stated shapes true

The plan gave field names, not widths. `Word64 = IntField.specialize(bitwidth=WORD_BW)` — one field
per stream word — is what produces a **3-word command and a 2-word response**, which is what the
brief stated twice. The named precedent (`FirOp` / `Word32`) was followed for the *mechanism*, not
for the width.

### What it cost, and it is a real cost

**The 16-bit configuration is gone.** An `EnumField` may not straddle a word, so a 64-bit `status`
cannot be carried on a narrower stream — the schema raises. `test_the_design_is_width_parametric_
and_the_witness_survives_it` had no subject left and was **replaced** by
`test_the_messages_pin_the_stream_width`, which pins what is now true (3 words / 2 words at 64) and
records the loss by asserting the raise. Any field width above 16 would have done the same; only
16-bit fields could have kept it, and those contradict both stated shapes.

### Measured, before and after

| | before | after |
|---|---|---|
| witness's five values | `100, 101, 107, 355, 228` | **unchanged**, both backends |
| command on the wire | 2 words, hand-unpacked | **3 words**, `get(WriteCmd)` |
| response on the wire | 1 word, a bare integer | **2 words**, `(tid, status)` |
| `resp_w` / `resp_r` capture | 4 / 8 words | **8 / 16** words |
| write / read payload loop II | 1 / 1 | **1 / 1** |
| kernel LUT / FF | 954 / 535 | 993 / 537 |
| XSI last read word | cycle 386 | **cycle 394** |

**The +8 is accounted for**: eight read commands, one extra command word each. The returned values
did not move; only when the last one arrived did. `WANT_CYCLES` was re-recorded with that arithmetic
written down beside it.

### Two shapes that had to move with it

* **`write_scenario` reframes the commands.** A command is now **one burst** (`serialize` decides its
  length), because `get(Schema)` asks for the whole message in one call and a pysim slave dequeues a
  whole burst per call. The old one-word-per-burst framing would have forced word-at-a-time reads —
  the vectors can force the anti-pattern, exactly as the plan warned.
* **The payload is untouched: one word per burst.** It is a data stream, not a structured message,
  and per-word framing is what keeps one pysim firing equal to one RTL firing.

### An index that had to be converted rather than re-typed

A sink timestamps every *word*, so `Scenario.overlap_write_resp` (a **response** index) now needs
`resp_words(WriteResp, i + 1) - 1` to reach the arrival cycle. That helper goes through
`nwords_per_inst`; a literal `2` would not have noticed the response growing a `tid`, and the index
would have pointed into the middle of a message. It caught exactly that, once, in the XSI overlap
gate.

---

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
- **A task's submodules are named for the TASK FUNCTION, not the top.** `bram_access`'s report
  entries are `bram_write_cmd_task_64_1024_Pipeline_write_payload`, with no `bram_access_` prefix —
  even though the RTL *file* on disk carries one.  A `_require` on a name with the prefix skips, and
  a skip on a gate this expensive reads as a pass.
- **Address overlap is NOT a collision.**  Two II=1 sweeps over the same range are parallel lines in
  (cycle, address) and never meet unless they start in the same cycle.  Making them meet needs a
  relative phase that *moves* — which is why `collision_scenario()` gives the writer and the reader
  command lengths that differ by one word.
- **A wrapped design's VCD dumper must be named for the WRAPPER.**  `run.bat` picks
  `vcd_dumper_%TOP%.v` and `$dumpvars` naming a scope outside this elaboration is a hard error, so
  `AddVcdTopStep(comp_class=...)` alone emits a dumper for the *kernel* — wrong file name, wrong
  scope, and the run produces no trace at all.  Pass `top=`.
- **A traced XSI run costs no cycles.**  The dumper is a second elaborated top, so the XSI top and
  every BFM port number are untouched; the traced run ends at the same 386.  Gate on the trace
  freely.
- **An empty hazard scan proves nothing on its own.**  It is what a correct design, a renamed net, a
  dump that never ran and a wrong `$dumpvars` scope all produce.  Always pair it with a scenario
  asserted to be dirty.
- **Reading never-written memory is not a check.**  pysim returns 0 from a zeroed numpy array; the
  RTL returns `X` (`0xFFFF_FFFF_FFFF_FFFF` once packed), because `bram_t2p.v`'s `mem` has no initial
  value.  Write a sentinel first.
- **A message's word count is the schema's, and an index into a per-word array must be converted.**
  A sink stamps every word; a response is `nwords_per_inst` of them.  Indexing a cycles array by
  response number silently lands mid-message the moment the message grows a field.
- **An `EnumField` may not straddle a word.**  A 64-bit status makes the design 64-bit-only, and the
  schema raises rather than mis-framing — which is the right failure and still a lost configuration.
- **The vectors can force the anti-pattern.**  One-word-per-burst framing makes word-at-a-time the
  only thing that works, so a `DataList` declared over such vectors gets hand-unpacked no matter what
  the plan says.  A command is ONE burst.
