# Review sheet: hls_body/01_gain_stats

For the reviewer, not the agent (no spec links this file). Fill one sheet per run. First run:

- `python evaluation/skill_tests/kernel_hls/check_body.py <trial folder>`: compiles the body as plain C++
  and compares it with the golden at both widths (item E1);
- `python evaluation/skill_tests/lint_schema_use.py <trial folder>`: lists hand-packing lines (B4).

Run: ____________  arm: [ ] MCP only  [ ] MCP + skill   model: ________  tokens in/out: ________
Skill loaded (from summary.md): [ ] yes, at tool call # ___   [ ] no

Score each item **2** (as the guide does it), **1** (partly), **0** (not done / done otherwise).
The reference is `ref_01/gain_stats_body.cpp`.

## A. The shape (`docs/guide/vectorization/hls/loop_optimization.md`)

| # | item | the pattern | score |
|---|---|---|---|
| A1 | one command per call | no `while (true)`, no `state` variable; read, loop, answer, return | |
| A2 | one pipelined loop over the sample words, `PIPELINE II=1` on it | not a loop per element with a nested read, not a straight-line body | |
| A3 | a word per iteration, the lane compute `UNROLL`-ed | the spec asks for one `s_in` word a clock; an element per iteration is 0 (it runs at 1/`PF` of that) | |
| A4 | `PF` from `lane_capacity<W>()`; lane arrays sized by it and `ARRAY_PARTITION`-ed | not `n`-sized arrays, not a literal 2 or 4 | |
| A5 | no decision in the loop beyond the tail count and TLAST | the header, `total`, the status all outside the loop | |

## B. Moving the data

| # | item | the pattern | score |
|---|---|---|---|
| B1 | samples by `fixed16_1_array_utils::read_axi4_stream_lane<W>`, tail count passed | | |
| B2 | results by `fixed16_2_array_utils::write_axi4_stream_lane<W>`, TLAST on the last result word only | | |
| B3 | `GainCmd` read, `GainResp` and `GainStatus` written by their schema methods; TLAST on the status | `read_axi4_stream<W>` / `write_axi4_stream<W>` or `read_msg` / `write_msg` | |
| B4 (L) | no `.range()`, shifts or masks on words | | |

## C. State and reset (`docs/guide/memory/hwstate.md`)

| # | item | the pattern | score |
|---|---|---|---|
| C1 | `total` is a `static`, 32 bits, and the only `static` | not a global, not a `static` flag or counter the spec does not keep | |
| C4 | `total` is cleared by reset: `#pragma HLS reset variable=total` (or `config_rtl -reset state` stated for the build) | the spec counts "since reset"; a `static` alone is zero only at power-on. `check_body.py` cannot see this | |
| C2 | the firing starts with the blocking read of the command | no write before it (a write first lets the statics move during reset) | |
| C3 | `total` updated by `n` once per command, wrapping at 32 bits | | |

## D. Arithmetic

| # | item | the pattern | score |
|---|---|---|---|
| D1 | the product in `ap_fixed` at full precision, assigned to the `Out` type, which rounds and saturates | not `float`, not a hand-rolled clip | |
| D2 | `nsat` as the golden counts it (the product rounded wide vs the `Out` value, or an exact equivalent) | not a float threshold, not `y == max` (a value can round exactly to the maximum without saturating) | |
| D3 | no casts or conversions the golden does not make | | |

## E. Correctness and notes

| # | item | score |
|---|---|---|
| E1 | `check_body.py`: compiles, and matches the golden at both widths (2 = all ok; 1 = compiles, a mismatch; 0 = does not compile) | |
| E2 | `NOTES.md` cycles: about `n / PF` clocks for the loop (`n/2` at 32 bits, `n/4` at 64) plus a fixed overhead a command | |
| E3 | `NOTES.md` explains the shape from the throughput asked for | |

## F. Process (from summary.md)

| # | item | score |
|---|---|---|
| F1 | read the loop-pattern page (or the skill) **before** writing the body | |
| F2 | no API names guessed and then corrected (count them: ___ ) | |
| F3 | ran `gen_headers.py` (and `gain_model.py`); changed no given file | |

**Total:** ____ / 44.  **Hand packing anywhere (a 0 on B1, B2, B3 or B4):** [ ] yes  [ ] no

Free notes -- what it got wrong and which line of the skill (or which missing line) would have
prevented it:
