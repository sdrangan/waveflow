---
title: C-Sim Functional Verification
parent: Streaming polynomial
nav_order: 3
summary: "Every scenario through Vitis C simulation in one run, with the hand-written testbench, and every response compared bit-exactly -- words, burst boundaries, TLAST flags and the final register status -- with the expected responses the Python model was also checked against. The error paths are verified in C++ too."
---

# C simulation

The third group runs the kernel under Vitis C simulation and checks every scenario's
response.

| Step | What it does |
| --- | --- |
| `csim` | Runs `run.tcl` with `WAVEFLOW_POLY_STAGE=csim`.  `poly_tb.cpp` plays every scenario into the kernel and records each response into `data/<scenario>/csim/` |
| `check_csim` | Compares each recorded response with `data/<scenario>/expected` |

## What is compared

`scenarios.check` is one checker used for every stage -- the pure model, pysim, csim
and cosim.  For each scenario it requires **exact** agreement on:

- the number of bursts and where they end;
- every word;
- every TLAST flag;
- the final register status (`halted`, `error`, `tx_id`).

There is no tolerance, even for the floating-point results.  The Python model and the
C++ body evaluate the polynomial with the same float32 operations in the same order, so
the bits agree.

The expected responses come from each scenario's intent, not from the Python model.
"csim matches the model" would show only that two implementations agree, and one AI or
one person may have written both with the same misunderstanding.  Here, both are
checked against a third description of what *should* happen.

## The error paths, in C++

Two scenarios are malformed on purpose:

| Scenario | Stimulus | Expected |
| --- | --- | --- |
| `early_tlast` | a 10-sample transaction whose burst ends after 6 | 6 results, no output TLAST, then `halted = 1`, `error = 3` (`TLAST_EARLY_SAMP_IN`), `tx_id = 32` |
| `no_tlast` | a 10-sample burst with no TLAST on its last word | 10 results, then `halted = 1`, `error = 4` (`NO_TLAST_SAMP_IN`), `tx_id = 41` |

Both run in the same C simulation as the well-formed scenarios.  The testbench drains
any input a halted kernel left unread, so each scenario starts clean.

## What you see when it passes

```
    csim   nominal      PASS
    csim   zero_len     PASS
    csim   early_tlast  PASS
    csim   no_tlast     PASS
    csim   timing       PASS
```

`results/check_csim.json` holds the same, with an empty problem list for each scenario.
On a mismatch, the step names the scenario, the burst and the first differing word.

## Run just this group

```bash
python examples/stream_inband/poly_build.py --through check_csim
```

Requires Vitis HLS.

---

Next: [C synthesis →](./04_csynth_resources.md)
