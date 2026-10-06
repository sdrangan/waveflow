# `stream_inband`: teach the command-response pattern cleanly

Status: **revision 2, for review.**  No code has changed.  Revision 2 follows the review of
revision 1:

- the coefficients travel in the `DATA` command header; there is no `CONFIG` command;
- error testing is light: one kernel call per scenario, with no recovery activations;
- the frame drops its footer.

The questions still open are in section 11.

## 1. What changes and why

`examples/stream_inband` (the streaming polynomial) is the reference design that the course's AI
agents are pointed at through the MCP server.  It is `DEFAULT_FRAME`, and the scaffold copies it.
Today it teaches **configuration over AXI-Lite while commands stream in-band**: the host writes
`coeffs` into a `VitisRegMap` field, then sends `DATA` commands.  The two paths have no ordering
between them, so changing the coefficients while commands are in flight is a race.

The new design puts **everything the kernel computes with on the stream**: each `DATA` command carries
its own coefficients.  AXI-Lite is left with control (`ap_start` / `ap_done`) and status (`halted`,
`error`, `tx_id`).

**Scope of the host-activated pattern.**  A host-activated kernel runs a bounded batch of commands
under a host program: `ap_start`, commands until `END`, `ap_done`.  An error is **fatal to the run**:
the kernel stops cleanly, and the host resets and starts over.  Long-running, continuous operation
with recovery inside the hardware belongs to the free-running (`FreeRunMod`) flow.  The docs say this
and link to that flow.  It is also why error testing here is deliberately light (section 6).

### Relation to `plans/poly_regmap_migration.md`

That plan (2026-05-18) made "coefficients in the regmap" its decision 2.  Commit `31c2790` ("Cleaned
up plans", 2026-05-22) deleted it.  This plan **restores it** from `31c2790^`, unchanged except for a
banner at the top:

> *Superseded on decision 2 by `stream_inband_pattern.md` (2026-10): the coefficients travel in each
> `DATA` command header, which is where they were before this plan.  Decisions 1, 3, 4, 5, 7, 9 and
> 10 still stand.  Decisions 6, 8 and 11 were already overtaken by the scenario-based testbench.*

Its stale `examples/poly` and `pysilicon` paths stay as they are: it is a historical record.  Its
other decisions:

| # | Old decision | Here |
|---|---|---|
| 1 | an `END` command, so csim can return | kept |
| 2 | coeffs in the regmap | **overridden**: in the `DATA` header |
| 3 | no `PolyRespFtr`; errors go through the regmap | kept; now rule 6 |
| 4 | `PolyRespHdr` echoes `tx_id` | kept |
| 5 | `END` emits no response | kept |
| 7 | the body runs on `ap_start` | kept (through `cpp_body`, as now) |
| 9 | `ap_ctrl_hs` | kept |
| 10 | no status-clear bit; reset comes from the platform | kept; the kernel also clears its status at every start (rule 5) |

`docs/future/ai_planning.md:20` links that plan, so the link works again once it is restored.

## 2. The contract

This block is quoted verbatim in the docs (index page and MCP frame).  The rule numbers are stable
from here on, so docs and agents can cite them.

> **The `stream_inband` contract**
>
> 1. **Two streams.**  `in_stream` carries `CmdHdr | samples`, repeated.  `out_stream` carries
>    `RespHdr | results`, one per `DATA` command, plus a `RespFtr` if the design needs one: a
>    fixed-length schema, sent as its own burst, holding only values that are known after the data
>    (statistics, a checksum).  A footer is response data and is sent only when the command succeeds.
>    Poly has none.
> 2. **Everything the kernel computes with travels on `in_stream`.**  Each `DATA` command carries its
>    own coefficients.  The host never writes configuration over AXI-Lite.
> 3. **AXI-Lite carries only control and status.**  The host starts the kernel with `ap_start`.  The
>    kernel processes commands until `END` or an error, then returns (`ap_done`).  The host then reads
>    `halted`, `error` and `tx_id`.
> 4. **Nothing carries over.**  A `DATA` command depends only on its own header and samples: not on an
>    earlier command, and not on an earlier activation.
> 5. **The kernel clears its status at the start of every activation.**
> 6. **On an error, the kernel:**
>    1. sets `halted = 1`, `error` and `tx_id`;
>    2. puts TLAST on the last word it wrote, if it had started an output burst;
>    3. returns at once, reading nothing more from `in_stream`.
>
>    It writes no footer and does not try to recover.
> 7. **An error ends the run.**  After an error the contents of `in_stream` are undefined: commands
>    the host queued behind the failed one may still be there.  The host resets the accelerator and
>    its stream path before the next `ap_start`.  The kernel never drains to the next TLAST.

The reasons, one per rule, go in the host/kernel table (section 8).  The "Why not..." page argues
them against the alternatives.

## 3. Commands and wire layouts

### Schemas (`poly.py`)

```python
class PolyCmdType(IntEnum):
    DATA = 0
    END  = 1

class PolyCmdHdr(DataList):
    elements = {
        "cmd_type": {"schema": PolyCmdTypeField, "description": "DATA or END"},
        "tx_id":    {"schema": TxIdField,        "description": "Command ID: echoed, or reported on error"},
        "nsamp":    {"schema": NsampField,       "description": "Sample count (0 for END)"},
        "coeffs":   {"schema": CoeffArray,       "description": "c0..c3, constant term first (ignored on END)"},
    }

class PolyRespHdr(DataList):          # unchanged
    elements = {"tx_id": {"schema": TxIdField, "description": "Echo of the DATA command's tx_id"}}
```

`SCHEMA_CLASSES` orders `CoeffArray` before `PolyCmdHdr`, which now contains it.
`docs/guide/build/codegen.md:93` already describes this case.

### Bursts on `in_stream`

| Command | Bursts |
|---|---|
| `DATA`, `nsamp > 0` | `PolyCmdHdr` (TLAST) · `nsamp` float32 samples (TLAST on the last word) |
| `DATA`, `nsamp = 0` | `PolyCmdHdr` (TLAST) only; not an error |
| `END` | `PolyCmdHdr` (TLAST); its `coeffs` words are sent as zeros and ignored |

**TLAST is checked only on sample bursts.**  The header is fixed-length and framed by its schema: the
generated `read_axi4_stream` reads exactly the schema's words and ignores TLAST.  The sample burst is
the only variable-length part, and TLAST is what frames it.

### Word layouts

Taken from serializing an instance with `cmd_type = DATA`, `tx_id = 0xABCD`, `nsamp = 0x1234` and
`coeffs = [1, -2, -3, 4]`:

| | `WORD_BW = 32` | `WORD_BW = 64` |
|---|---|---|
| `PolyCmdHdr` | 6 words: `w0 = tx_id<<1 \| cmd_type` (`0x0001579a`); `w1 = nsamp` (`0x1234`; it does not fit the 15 bits left in `w0`); `w2..w5` = one coefficient each (`0x3f800000 0xc0000000 0xc0400000 0x40800000`) | 3 words: `w0 = nsamp<<17 \| tx_id<<1 \| cmd_type` (`0x2469579a`); `w1, w2` = two coefficients each, element 0 in the low half (`0xc00000003f800000 0x40800000c0400000`) |
| `PolyRespHdr` | 1 word | 1 word |
| samples | 1 per word | 2 per word, element 0 low; an odd `nsamp` leaves the high half of the last word zero |

The docs reproduce this table from a step that serializes the instances, not by hand.

**Cost of coefficients in every header:** 4 extra words per `DATA` at 32 bits (2 at 64).  That is
negligible against a 100-sample burst.  The "Decisions" page says when it stops being negligible: a
large or rarely changing configuration (a 256-tap FIR), short bursts.  That is when a design adds a
separate `CONFIG` command, at the price of state that carries between commands, and a `NO_CONFIG`
error.

## 4. Error codes

```python
class PolyError(IntEnum):
    NO_ERROR            = 0
    TLAST_EARLY_SAMP_IN = 1   # TLAST before the last sample word
    NO_TLAST_SAMP_IN    = 2   # no TLAST on the last sample word
```

Three of today's codes are removed:

- `TLAST_EARLY_CMD_HDR` and `NO_TLAST_CMD_HDR` were reserved but never detected.  A reference design
  should not list errors it cannot raise.
- `WRONG_NSAMP` cannot be reached in the C++.  The sample loop stops only on TLAST or on `nsamp`,
  and the two TLAST codes cover both.  pysim used it for a short burst, which is now
  `TLAST_EARLY_SAMP_IN`.

**No `BAD_CMD`.**  With two commands, `cmd_type` is a 1-bit field (checked).  Every value on the wire
is a valid command, so there is no unknown opcode to detect.  The "Decisions" page carries the
general point: if your opcode field has unused codes, decide what they do.  Making the field wider
than its values would also not make `BAD_CMD` testable: the schema refuses to build or parse an
out-of-range value (`ValueError`, checked).

**Precedence:** errors are detected in stream order, and the first one ends the run.  The two codes
are mutually exclusive within one sample burst.

**`tx_id` in the status** is the `tx_id` of the `DATA` command that failed.  After a successful run,
`halted`, `error` and `tx_id` are all 0.

## 5. The kernel

### C++ body (`poly_body_impl.tpp`)

```cpp
template <int in_bw, int out_bw>
void body(s_in, m_out, ap_uint<1>& halted, ap_uint<8>& error, ap_uint<16>& tx_id) {
#pragma HLS INLINE
    halted = 0; error = NO_ERROR; tx_id = 0;                // rule 5
    while (true) {
        PolyCmdHdr hdr;  hdr.read_axi4_stream<in_bw>(s_in);
        if (hdr.cmd_type == PolyCmdType::END) return;
        ap_uint<8> err = transaction<in_bw, out_bw>(hdr, s_in, m_out);   // hdr.coeffs inside
        if (err != NO_ERROR) { error = err; tx_id = hdr.tx_id; halted = 1; return; }
    }
}
```

`transaction()` changes in three ways:

- **It takes the coefficients from the header.**
- **An early TLAST closes the output burst** (rule 6, step 2).  Today `out_tlast = (nrem <= pf)`, so
  an early TLAST leaves the output burst open and a DMA receiving it waits forever.  The condition
  becomes `(nrem <= pf) || lane_tlast_at_end`.  At 64 bits an early TLAST on word `j` means word `j`
  is full (`nrem > pf`), so no half sample is ever emitted.
- **The `WRONG_NSAMP` check goes.**

The generated top loses `float coeffs[4]` and its `s_axilite` pragma.  The regmap keeps only
`halted`, `error` and `tx_id`, all `R`.

### Two widths

`PolyAccel.param_supports = {"bw64": {"in_bw": 64, "out_bw": 64}}` is the mechanism `process.md`
already tells agents to use.  The generator emits `poly` (32 bits) and `poly_bw64` (64 bits), both
calling the same templated body.  Vitis builds each in its own project, one directory deep:
`waveflow_poly_w32/` and `waveflow_poly_w64/`.  `run.tcl` reads `WAVEFLOW_POLY_WIDTH` (32 or 64) from
the environment and uses it to set the top, the project and `-DPOLY_WORD_BW=<w>` on the testbench.

### Python

- **`poly_stream_model(bursts, word_bw)`** loses its `coeffs` argument, takes them from each header,
  and covers both widths.  It packs words with `read_array` / `write_array`, never by hand.
- **`PolyAccel.body()`** (pysim) uses `cmd_hdr.coeffs`, clears the status at entry, and treats a short
  burst as `TLAST_EARLY_SAMP_IN`.
- **`PolyTB`** loses `coeffs` and the `rm.set("coeffs")` write.

## 6. Testing: what is checked, and how much

The kernel's own behavior on an error is cheap to check, and a fault there is real.  A kernel that
never closes its output burst hangs the receiving DMA, and one that keeps reading consumes the next
command as samples.  So **the error scenarios stay in C-sim**, one kernel call each, exactly as today.

What goes is the **multi-activation recovery** from revision 1.  Recovery is "the host resets and
starts again" (rule 7), which is a property of the platform, not of the kernel.  A fresh stream per
scenario in the testbench is already "a reset"; simulating restarts adds nothing a student needs.

- **One kernel call per scenario.**  The testbench gives each scenario fresh streams, as now.  What a
  halted kernel left in `s_in` is discarded with the stream: that is the host's reset.
- **Poisoned status.**  Before each call, the testbench sets `halted = 1`, `error = 0xFF` and
  `tx_id = 0xFFFF`.  A kernel that does not clear its status (rule 5) then fails every well-formed
  scenario.  It is one line in each testbench (C++ and pysim).
- **Queued commands after the error.**  The error scenarios keep sending commands after the bad burst,
  as a host with commands in flight would.  The expected response shows the kernel did not touch
  them: they produce no output, and the status names the failed command.
- **TLAST closed.**  For each error scenario, the checker asserts with its own failure message that
  the last output burst ends with TLAST.  An exact compare against the expected response implies it
  already, but it is the rule a student is checking.
- **Expected responses are still computed from intent**, never from the model.

### Scenarios (each one kernel call)

| Scenario | Commands | Expected |
|---|---|---|
| `multi_data` | DATA(11, 100, A) · DATA(12, 7, A) · DATA(13, 1, A) · END | 3 × (resp, results); status clear |
| `coeff_change` | DATA(21, x, A) · DATA(22, x, B) · DATA(23, x, A) · END | the same 16 samples under A, B, A.  Checks rule 4: nothing carries over |
| `zero_len` | DATA(31, 0) · DATA(32, 5) · DATA(33, 0) · END | resp only; resp + 5; resp only |
| `early_tlast` | DATA(41, 20) · DATA(42, 10, sent 6) · DATA(43, 5) · END | resp, 20; resp, 6 **with TLAST on the 6th**; `TLAST_EARLY_SAMP_IN`, `tx_id = 42`; DATA 43 and END stay unread |
| `no_tlast` | DATA(51, 10, no TLAST) · DATA(52, 5) · END | resp, 10 with TLAST on the 10th; `NO_TLAST_SAMP_IN`, `tx_id = 51` |
| `timing` | DATA(61, 100, A) · END | the cycle-count scenario |

A = `[1, -2, -3, 4]` (today's); B = `[0.5, 0.25, -1, 2]`.

### What each stage runs (at both widths)

| Stage | Scenarios |
|---|---|
| model | all |
| pysim | all but `no_tlast`: a pysim stream cannot omit TLAST, but it can send a short burst, so `early_tlast` runs |
| csim | all |
| cosim | `timing`, for the cycle count; and best-effort `early_tlast_vcd` (below) |
| timing check | `timing` |

`early_tlast_vcd` is `early_tlast` with nothing sent after the bad burst.  It leaves nothing unread,
so cosim has no leftover input to replay, which avoids the main risk of revision 1.  It exists only
to record the waveform that the error-path page is drawn from.  If cosim still will not run it, the
timeline is drawn from the csim/model output and labelled as schematic, and the report says so.

**No pysim stream `reset()`** is needed, because there are no restarts to model.

## 7. Timing

**Before** (measured 2026-10-06 on the unchanged code with `--through summary`; all 16 steps pass;
32 bits only, because the 64-bit flow does not exist yet):

| | pysim | cosim | delta | DSP | FF | LUT | BRAM |
|---|---|---|---|---|---|---|---|
| w32, `timing` (1 × DATA 100) | 140 | 143 | 3 | 15 | 1814 | 2709 | 0 |

The committed `results/*.json` were stale: they still pointed at `examples/poly`.

**After: pysim will measure what cosim measures.**  The cosim report's number is one kernel call,
`ap_start` to `ap_done`.  Today pysim measures from the first sample read to the last sample written,
and `proc_latency = 40` absorbs the difference.  The longer header (4 more words at 32 bits) would
widen that gap without anything in the model saying why.  So pysim's span becomes the whole call,
`proc_begin` to `proc_end`: both sides measure one kernel call, which is the easiest version for a
student to reason about.  Then `proc_latency` is recalibrated from cosim at each width.  If the two
widths need different values, `proc_latency` becomes a per-width value in `param_supports`, and the
docs say so.

`05_cosim_timing.md` keeps the 10 → 40 calibration story and adds the next chapter: the span was made
to match, and this is what that did to the number.

## 8. Docs: `docs/examples/stream_inband/`

Every page opens with the question it answers and ends with "Check your understanding" (two or three
questions).  The order below is the nav order: the protocol and its reasons come before the build.

| Page | Question in its first sentence | Content |
|---|---|---|
| `index.md` | "What is the contract between a host and a command-driven streaming kernel?" | a two-stream protocol diagram (SVG, using the committed-figure workflow); the host/kernel table below; the contract block (section 2); the scope note (host-activated vs free-running); a short map of the pages, the files, and how to run |
| `protocol.md` *(new)* | "What crosses each interface, word by word?" | the three interfaces (`in_stream`, `out_stream`, AXI-Lite); `PolyCmdHdr` with `coeffs` and `PolyRespHdr` as declared; the serialized layout table at both widths; the register map (status only); the TLAST rules; **"Adding a footer"**: header for what is known before the data, footer only for what is known after it (why: no buffering of the burst); fixed-length schema, its own burst, success only, never an error channel; a few-line sketch, with links to the frame specs that use one |
| `why_not.md` *(new)* | "Why is the contract this way and not another?" | why not: configuration over AXI-Lite (the race, and mm_fir's `cfg_id` as the way to make it safe, with what it costs); a response footer with recovery; draining to TLAST; configuration that persists (registers, or a `CONFIG` command), and when a `CONFIG` command *is* worth it |
| `error_path.md` *(new)* | "What happens on the wire when a command fails?" | a timeline: the bad burst arrives; the kernel closes its output with TLAST and returns; queued commands stay in the FIFO; the host reads the status, resets, restarts.  Drawn from the cosim VCD of `early_tlast_vcd` |
| `decisions.md` *(new)* | "What must your own spec decide before you build it?" | the checklist, each item with poly's answer: where configuration lives (header or a `CONFIG` command); what zero-length `DATA` means; which error wins; how fields pack into words at each width; rounding and saturation for fixed point; what `tx_id` means in the status; TLAST on fixed-length bursts; unused opcode values |
| `01_python_golden_model.md` | "How do we know the Python model is right?" | `PolyCmdHdr` with its `coeffs`; intents and scenarios |
| `02_hls_codegen.md` | "Which parts of the kernel does Waveflow write, and which do you?" | the new top (no `coeffs`), `poly_bw64`, the body |
| `03_csim_verification.md` | "How does C simulation check the error paths?" | the error scenarios, the poisoned status, the TLAST-closed check |
| `04_csynth_resources.md` | "What did synthesis build, and does every loop reach II = 1?" | both widths |
| `05_cosim_timing.md` | "What does co-simulation tell us that C simulation cannot?" | cycle counts at both widths; the calibration story, kept and extended |
| `poly_axi_stream.md` | "How do you read this protocol off a waveform?" | decoding the new header |

History that only records how the code evolved is removed (for example "the kernel has since been
rewritten hook-first" and "Coefficients no longer appear on the stream").

### The host/kernel table (draft for the index)

| When | Host | Kernel | Why |
|---|---|---|---|
| start | writes `ap_start` | clears its status | rule 5: the status describes this run only |
| per command | sends `DATA` (header with its coefficients, then the samples) or `END`; never touches AXI-Lite | `DATA`: response header, then results.  `END`: returns | rules 2 and 4: one ordered path, so there is no race, and every command stands alone |
| on error | waits for `ap_done`; reads `halted`, `error`, `tx_id` | sets the status; closes its output burst with TLAST; returns, reading nothing more | rule 6: a DMA never waits for a TLAST that never comes, and no hardware is spent on recovery |
| restart | resets the accelerator and its stream path; restarts; resends from the failed command | starts clean | rule 7: the FIFO may still hold queued commands, so only a reset gives a known state |

## 9. Dependents

Taken from `git grep -il poly -- docs tests examples waveflow`: 105 files, classified in an inventory
pass.

### Fixtures that import the example's schemas: decouple, do not redesign

`tests/fixtures/poly_extracted/poly_extracted.py`, `tests/hw/state_poly_fixture.py` and the `polyb_*`
fixtures (used through `test_body_only_vitis.py`) import `PolyCmdHdr`, `PolyError`, `SCHEMA_CLASSES`
and other names from `examples.stream_inband.poly`.  They test the **extractor, `add_state` and the
body-only codegen**, not this example's protocol.  A dozen framework tests rest on them:
`test_extract_poly`, `test_phase3`, `test_codegen_check`, `test_codegen_dispatch`,
`test_hw_hostactivated`, `test_add_state`, `test_hw_testbench`, `test_body_only_vitis`, and others.

**The plan is to freeze today's schemas into a fixture module**
(`tests/fixtures/poly_extracted/poly_schemas.py`, a copy of today's schema section) and point those
fixtures at it.  Their regmap-`coeffs` design then stays as it is, as a regmap test, which is what it
is.  `test_hw_testbench`, `test_verify_steps` and `tests/mcp/test_schema_tools.py` import plain
schemas; each moves to the frozen copy or the new schemas, depending on what the test is about.

### Tests of the example: update

| Test | Change |
|---|---|
| `tests/examples/test_poly_demo.py` | new scenarios and errors; the worked `poly_eval` values stay; the timing assertion follows the new span; width cases 32 and 64 |
| `tests/examples/test_poly_codegen.py` | no `coeffs` in the signature or the pragmas; `poly_bw64` is emitted |
| `tests/poly/poly_timing_fixture.py`, `test_timing_analysis*.py`, `tests/fixtures/poly/timing/*.vcd` | regenerate the synthetic VCD with the longer header; `test_timing_analysis` also checks the decoded `coeffs` |
| `tests/poly/test_timing_capture.py` | project path becomes `waveflow_poly_w32` |
| `tests/mcp/test_usage_index.py`, `test_knowledge_corpus.py`, `test_retrieval_eval.py`, `test_server_smoke.py`, `test_kb_cli.py` | class and port names stay; the retrieval queries ("persistent loop END", "halted error tx_id") are re-checked against the new pages |
| `tests/mcp/test_scaffold.py` | the anchors (section 10); the `WRONG_NSAMP` assertion becomes `TLAST_EARLY_SAMP_IN`; `VitisRegMap` still survives (status) |
| `tests/examples/test_mcp_tools.py` | follows the replaced `schema_examples/poly.py` (section 10) |
| `tests/hw/test_add_state_vitis.py` | **already broken today**: it reads the deleted `poly_evaluate_impl.tpp`.  Pointed at the fixture's `.tpp` |
| `tests/utils/test_cosimparse.py`, `tests/build/test_cosim_steps.py` | no change: they read a static `.rpt` fixture |
| `tests/hw/test_regmap_vitis_layout.py`, `tests/hw/test_hwgen.py`, `tests/hw/test_dataschema_poly.py` | no change: self-contained.  One comment ("poly's shape") is reworded |

### Guide pages: update

| Page | Change |
|---|---|
| `comp_codegen/host_launch.md`, "Worked example: poly accelerator" (L282–394, plus L136–150, 204–215, 417–473) | **replaced** with the `examples/regmap` (`simp_fun`) kernel.  It is already stale: it has a `status_clear` field and offsets that poly no longer has |
| `interface/axi_mm/regmap.md` (L181–188, 226–249, 342–343) | the owner-side API and quick-reference lines use the `regmap` example's fields.  "Composite fields" keeps an array field but declares its own instead of using poly's, because `examples/regmap` has only scalars.  `PolyError.NO_TLAST`, which does not exist, goes |
| `custom_hooks/body_only.md`, `custom_hooks/stream.md` | the new top and body; the error names; `transaction()` with the TLAST-closed output |
| `comp_codegen/testbench.md`, `interface.md`, `structure.md`, `templating.md` | small fixes, such as "preloads coefficients" |
| `build/codegen.md`, `python.md`, `vitis.md`, `tcl.md`, `index.md`, `corecomp.md` | `coeffs.bin` is gone; both widths; the cycle numbers; the `run.tcl` snippet (already stale) |
| `schema/python/datalists.md`, `dataarrays.md`, `schema/hls/codegen.md`, `serialization.md`, `tbutils.md` | `PolyCmdHdr` as now declared.  `datalists.md` already shows `coeffs` in the header, so it becomes correct again |
| `patterns/command_response.md` | the poly row (coefficients in the command); the "Errors" section makes halt-and-reset the default for host-activated kernels and links `why_not.md`; "carry on and resynchronize" points to the free-running flow; "waits for the host to clear it" becomes the reset contract |
| `interface/primitive/stream.md`, `primitive/index.md`, `memory/hwstate.md`, `sim/running.md`, `sim/logging.md`, `timing/axistream.md`, `cosim_timing.md`, `vcd.md`, `parsing.md`, `overview/pymodel.md` | the snippets and claims that name coeffs in the regmap, `WRONG_NSAMP`, the cycle numbers or the project names |
| `docs/examples/mm_fir/*`, `bram_access/python.md`, `firblock/firblock.md`, `basic_vec/vitis.md` | links are checked; text changes only where it describes poly's design |

Comments and docstrings in `waveflow/` and in other examples that only name poly are reworded where
they become false (for example `hwgen.py:2211`).  None of them is functional.

`examples/timing/dump_poly.vcd` stays.  It is used only by `examples/timing/poly_timing.ipynb`, as a
generic VCD-parsing demo, and no test reads it.  `examples/stream_inband/vcd/dump.vcd` and
`view_timing.ipynb` are regenerated from the new cosim.

## 10. MCP

Nothing is cached on disk: the corpus, BM25 and usage indices are built in memory when the server
starts (checked).  So the doc and example changes reach search, the example card and
`waveflow_get_example` on their own.  These files need editing:

- **`frames/stream_inband/frame.md`**, the spec agents build against:
  - **F1:** the table updates; the regmap is status only.
  - **F3**, rewritten around the contract: the function's parameters go in the `DATA` header, and
    `out_stream` is `RespHdr | results`, plus a `RespFtr` only when the function spec has results
    known after the data.  The footer loses `nsamp_read`, which on success repeats `nsamp`, and it is
    never sent after an error.
  - **F4**, rewritten: the new codes; `BAD_PARAM` is "a `DATA` header whose parameters are illegal
    halts with `BAD_PARAM`, `tx_id` = that command's, and nothing is written for it"; after any error
    nothing more is written except the TLAST that closes the output.
- **`prompts/01_gain_clip.md`, `02_fir.md`, `03_cmag_peak.md`:** "Register map parameters" becomes
  "Command header parameters".  `nsamp_read` goes, and with it the "footer emitted" expectations
  on error.  All three keep a success-only results footer, because each has a result known only
  after the data: `01_gain_clip` its clip counts, `02_fir` `n_sat`, and `03_cmag_peak` the peak
  index and magnitude.
- **`frame.toml`:** `synopsis`, and `parameters = "in the DATA command header"`.  The stub anchors
  (`y = np.full(xs.shape, c[3]…)` and `float y = coeff[3];`) must still match literal lines.  I keep
  those lines, and `test_scaffold` guards them.
- **`process.md`**, what `waveflow_get_process` returns and what the scaffold writes as `AGENTS.md`:
  the "register-map parameter types" line goes, and Stage 1 gets a pointer to the contract and to
  `decisions.md` as required reading.
- **`waveflow/mcp/resources/schema_examples/poly.py` and `schema_examples.py`:** replaced with the new
  schemas (`PolyCmdHdr` with `coeffs`, `PolyRespHdr`, `PolyError`).  `poly_resp_ftr` goes.

After the change, I run the server's tools against the new tree:

- `waveflow kb examples` and `kb example stream_inband`;
- `kb search` for the queries in `test_retrieval_eval`;
- `waveflow_get_process`.

## 11. Questions

Answered in review: restore the old plan (yes); `BAD_CMD` (wanted, but it cannot occur with a 1-bit
`cmd_type`, see section 4); pysim stream `reset()` (no longer needed); the timing span (match cosim);
the new pages go before 01–05, starting with the protocol and its interfaces;
replace `schema_examples/poly.py`; footers: `RespHdr | data`, with an optional `RespFtr` (sent only
on success) for results known only after the data.  Poly has no footer; the protocol page teaches
when to add one.

Nothing is open.  The plan is ready for approval.

## 12. Risks

- **R1. Writing an `s_axilite` output twice per call** (cleared at entry, set on error).  This should
  be fine with `ap_ctrl_hs`.  If Vitis complains, the body keeps the status in locals and writes each
  register once, on every return.
- **R2. Width-dependent latency.**  One `proc_latency` may not fit both widths within tolerance.  Then
  it becomes a per-width value (section 7).
- **R3. Cosim of `early_tlast_vcd`.**  This is best-effort only; the fallback is in section 6.
- **R4. Scaffold anchors.**  Rewriting `poly_eval` or `eval_poly_horner` so that the anchor lines
  change would silently break `waveflow new-accel`.  The lines are kept verbatim, and `test_scaffold`
  fails if they are not.

## 13. Order of work

On a branch `stream-inband-pattern`, one PR, with commits in this order:

1. restore the old plan with its banner; add this plan
2. freeze the fixture schemas and point the fixtures at them (all fast tests green, no change in
   behavior)
3. schemas, model, scenarios, checker (model green at both widths)
4. pysim body and testbench (pysim green at both widths)
5. C++ body, top, `param_supports`, `poly_tb.cpp`, `run.tcl`, the build DAG per width (csim green at
   both widths)
6. csynth, cosim, recalibration (timing green at both widths); the `early_tlast_vcd` waveform
7. `timing_analysis.py`, the fixtures and the VCDs
8. the example docs: the four new pages, then 01–05 rewritten
9. the guide pages, with `host_launch.md` and `regmap.md` repointed at `examples/regmap`
10. the MCP frame, prompts, `process.md`, `frame.toml` and schema examples; the MCP tool check
11. the full suite: fast, `-m vitis` and `-m xsi` (expected baseline 0 / 0 / 1)

The report will give each step's result, the timing before and after at each width, and anything not
done.
