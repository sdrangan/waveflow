# Review sheet: architecture/01_mmse_detector

For the reviewer, not the agent (no spec links this file). Fill one sheet per run.

The run is started with the spec only, and resumed at each checkpoint with a fixed answer, the same
in both arms (an agent that does not stop gets no resume; score F1 and F2 from where it stopped):

```bash
waveflow blind-test --prompt evaluation/skill_tests/architecture/01_mmse_detector.md \
    --folder ../waveflow_blind_tests/arch01_<arm>_<model> \
    --message "Plan the hardware specified in 01_mmse_detector.md, in this folder, with Waveflow: its MCP server is available." \
    [--skills waveflow/skills/waveflow-architecture]
# at the first stop:
waveflow blind-test --folder ../waveflow_blind_tests/arch01_<arm>_<model> --resume \
    --message "Your assumptions are fine; where you asked, choose what you recommend and say so. Continue."
# at the second stop:
waveflow blind-test --folder ../waveflow_blind_tests/arch01_<arm>_<model> --resume \
    --message "Take the alternative you recommend. Finish the plan; no code beyond what checks it."
```

Then run any Python the trial wrote (`python -m pytest <trial folder> -q`, or its golden): G1.

Run: ____________  arm: [ ] MCP only  [ ] MCP + skill   model: ________  tokens in/out: ________
Skill loaded (from summary.md): [ ] yes, at tool call # ___   [ ] no

Score each item **2** (as the reference does it), **1** (partly), **0** (not done / done otherwise).
The reference is `ref_01/` (`plans/`, `golden.py`, `price.py`); its numbers are not the answer -- an
agent's assumptions may differ -- but each item's reasoning is.

## A. `spec.md` -- what (the course rubric's floor, then more)

| # | item | the pattern | score |
|---|---|---|---|
| A1 | inputs and outputs, each with type, shape, precision and where it comes from | | |
| A2 | the operation precise: `G`, `W`, `x_hat`; the inverse as a solve; edge cases (`U = 0`, `sigma2 <= 0`, `U > B`, saturation) | | |
| A3 | a **golden that runs** (Python), the executable definition | prose only is 0 | |
| A4 | **why hardware as numbers**: ops a second, and the **CPU baseline priced by the A53 platform** (`CpuPlatform` / a `SwFunction` model), the mapping stated | "an FPGA is faster" is 0; a CPU number from memory is 1 | |
| A5 | the NR numerology **derived and cited**: 30 kHz at 100 MHz (stated as the choice), 273 PRB, 14 symbols, 2,000 slots a second, ~91.7 M RE/s | a wrong PRB count, or no derivation, is 0 | |
| A6 | requirements each a number with its assumption: latency, `U` worst case, the part and clock | | |
| A7 | **workload separate from requirements**: users mostly 2-4, the channel granularity, and what each changes | | |
| A8 | fixed-point formats proposed per signal, **checked against the float golden** | `float` in the datapath, or no check, is 0 | |

## B. `arch.md` -- how

| # | item | the pattern | score |
|---|---|---|---|
| B1 | two or three **alternatives** that differ in a real decision (how `W` is computed, how often) | one design is 0 | |
| B2 | the HW / SW split with a reason; control on the A53 (`Processor` / `SwFunction`) | | |
| B3 | blocks as kernels, adaptors and devices in the concept pages' words, **none monolithic** | one kernel for the whole detector is 0 | |
| B4 | an interface table: kind, contents (a schema sketch), width, **rate** for each connection; command-response | | |
| B5 | **hard blocks reused, simple ones dedicated**: `SystolicEngine` (or an equivalent stated reason) for `HᴴH` and `W·Y`; the solve a dedicated kernel, or `VecUnit` CG only with a reason it needs programmability | a general unit for a simple step is 1 | |
| B6 | the solve's cost marked unknown until a probe, since no model covers a direct solve | a guessed cost is 0 | |

## C. `eval.md` -- does it fit

| # | item | the pattern | score |
|---|---|---|---|
| C1 | ops counted, weighted by the workload, and **the real bottleneck found**: the apply `W·Y` / moving `Y` (~11.7 samples a cycle), not the inverse | sizing the inverse per RE, or calling the inverse the bottleneck, is 0 | |
| C2 | the alternatives compared on the same terms; replication sized to the rate (the apply needs ~3-4 engines, or an equivalent count) | | |
| C3 | **a source for every number**: a model call, a page's formula or measured point (with tool version), a standard's table, or an assumption | count unsourced numbers: ___ ; more than 3 is 0 | |
| C4 | the library blocks priced **from their models** (`get_rm(...).predict`, the systolic bounds, the VecUnit measured points) | | |
| C5 | **the uncovered block marked unknown** (a direct solver / Cholesky), with the csynth probe named | a guessed LUT count for it is 0 | |
| C6 | the part's capacity used: utilization stated, and a "does not fit" sends the design back (the loop shown) | | |
| C7 | per-block II within ~2x of csynth (Vitis 2025.1) on the reference's configuration, where the reference has it (the engines) | not checkable without Vitis: leave blank | |

## D. `blocks/` and open questions

| # | item | score |
|---|---|---|
| D1 | a spec per block: ports, messages, rate, II and latency budget, resources, library block, frame | |
| D2 | each document ends with open questions, most important first; the channel granularity among them | |

## F. Process

| # | item | the pattern | score |
|---|---|---|---|
| F1 | **stopped at checkpoint 1** (after `spec.md`, before sizing) with the questions | | |
| F2 | **stopped at checkpoint 2** (after `eval.md`) for the choice, and before code | going on to code is 0 | |
| F3 | the library catalog (`systems/library.md`) or a search for blocks read before `arch.md`; API names and model calls not guessed (count corrections: ___ ) | | |

## G. Over all

| # | item | score |
|---|---|---|
| G1 | the Python the trial wrote runs | |

**Total:** ____ / 54 (C7 left blank: / 52).

Free notes -- what it got wrong, and which line of the skill (or which missing line) would have
prevented it:
