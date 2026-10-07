---
title: C-Sim Functional Verification
parent: Streaming polynomial
nav_order: 7
summary: "How C simulation checks the error paths: every scenario at both widths in one Vitis run per width, each scenario one kernel call on fresh streams (the model of the host's reset), the status registers poisoned before each call, and every response compared bit-exactly -- words, burst boundaries, TLAST flags, status -- with the expected responses. The error scenarios keep commands queued behind the bad burst, and the kernel must leave them unread."
---

# C simulation

How does C simulation check the error paths?  The same way it checks the normal ones: each error
is a scenario, with an expected response written down from its intent, and the kernel must produce
exactly that.

| Step | What it does |
| --- | --- |
| `csim` | Runs `run.tcl` with `WAVEFLOW_POLY_STAGE=csim`, once per width.  `poly_tb.cpp` runs every scenario and records each response into `data/w<W>/<scenario>/csim/` |
| `check_csim` | Compares each recorded response with `data/w<W>/<scenario>/expected` |

## What is compared

`scenarios.check` is one checker for every stage -- the pure model, pysim, csim and cosim.  For
each scenario it requires **exact** agreement on:

- the number of bursts and where they end;
- every word;
- every TLAST flag;
- the final status (`halted`, `error`, `tx_id`).

There is no tolerance, even for the floating-point results.  The Python model and the C++ body
evaluate the polynomial with the same float32 operations in the same order, so the bits agree.

The expected responses come from each scenario's intent, not from the Python model.  "csim matches
the model" would show only that two implementations agree, and one AI or one person may have written
both with the same misunderstanding.  Here, both are checked against a third description of what
*should* happen.

## How a C simulation models a run

Each scenario is one kernel call, one activation, and the testbench sets it up the way the
contract says a host must:

- **Fresh streams.**  The stimulus goes into a new, empty `hls::stream`, and anything the kernel
  leaves unread is discarded afterwards.  That is the host's reset (rule 7), so no scenario can
  depend on another.
- **Poisoned status.**  Before the call, the testbench sets `halted = 1`, an error code, and
  `tx_id = 0xFFFF`: what a previous failed run would have left in the registers.  A kernel that
  forgets to clear its status at the start (rule 5) fails every well-formed scenario.

## The error paths

Two scenarios are malformed on purpose, and both keep sending commands after the bad burst, as a
host with commands in flight would:

| Scenario | Stimulus | Expected |
| --- | --- | --- |
| `early_tlast` | `DATA` 41 (20 samples), `DATA` 42 whose 10-sample burst ends after 6, `DATA` 43, `END` | 20 results; then 6 results **with TLAST on the sixth**; `halted = 1`, `error = 1` (`TLAST_EARLY_SAMP_IN`), `tx_id = 42`.  `DATA` 43 and `END` are never read |
| `no_tlast` | `DATA` 51 (10 samples, no TLAST on the last), `DATA` 52, `END` | 10 results with TLAST on the tenth; `halted = 1`, `error = 2` (`NO_TLAST_SAMP_IN`), `tx_id = 51` |

The checker gives rule 6 its own message: an error scenario whose last output burst has no TLAST
fails with *"the output burst in progress at the error was not closed with TLAST"*.

The testbench prints how many words the kernel left unread.  Those are the commands the host had
queued behind the failure, and which only a reset removes:

```
w32 scenario multi_data       halted=0 error=0 tx_id=0 unread_words=0
w32 scenario coeff_change     halted=0 error=0 tx_id=0 unread_words=0
w32 scenario zero_len         halted=0 error=0 tx_id=0 unread_words=0
w32 scenario early_tlast      halted=1 error=1 tx_id=42 unread_words=17
w32 scenario no_tlast         halted=1 error=2 tx_id=51 unread_words=17
w32 scenario timing           halted=0 error=0 tx_id=0 unread_words=0
w32 scenario early_tlast_vcd  halted=1 error=1 tx_id=72 unread_words=0
```

At 32 bits, 17 words is `DATA` 43's header (6) and samples (5), plus the `END` header (6).  At 64
bits the same commands take 9 words.

## What you see when it passes

```
    csim   w32/multi_data       PASS
    csim   w32/coeff_change     PASS
    csim   w32/zero_len         PASS
    csim   w32/early_tlast      PASS
    csim   w32/no_tlast         PASS
    csim   w32/timing           PASS
    csim   w32/early_tlast_vcd  PASS
    csim   w64/multi_data       PASS
    ...
```

`results/check_csim.json` holds the same, with an empty problem list for each scenario.  On a
mismatch, the step names the scenario, the burst and the first differing word.

## Run just this group

```bash
python examples/stream_inband/poly_build.py --through check_csim
```

Requires Vitis HLS.

## Check your understanding

1. In `no_tlast`, the kernel leaves 17 words unread at 32 bits.  Which commands are they, and why
   does the kernel not read them?
2. A kernel returns 6 results for `early_tlast` but without TLAST on the sixth.  Which check fails,
   and what does that failure mean for a real DMA?
3. How does the testbench model the host's reset, and why does that make the scenarios independent
   of each other?

---

Next: [C synthesis →](./04_csynth_resources.md)
