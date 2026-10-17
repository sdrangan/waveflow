# Kernel spec: `cplx_gain`, a complex gain per command

Frame: `stream_kernel` -- read [frame.md](../frame.md) first; this file adds only what is specific to
`cplx_gain`.

## 1. Overview

Each command carries a complex gain `g` and is followed by `n` complex samples `x[k]`; the kernel
writes `y[k] = g x[k]`, rounded and saturated, headed by the command's `tx_id` and `n`, and then a status.

## 2. Messages

- **Command** (`s_in`, one burst): `tx_id` (uint16), `n` (uint16, 1 to 4096), `g` -- a complex value of
  two signed `fixed<16, 2>` (`ap_fixed<16, 2>`).
- **Samples** (`s_in`, one burst): `n` complex values, each two signed int16 (`rI`, `rQ`).
- **Results** (`m_out`): a header `tx_id`, `n` (one burst), then `n` complex values, each two signed
  int16 (one burst).
- **Status** (`m_status`): `tx_id`, `status` (uint16: 0 `OK`, 1 `TLAST_EARLY`).

`WORD_BW` is a build parameter, 32 or 64: one or two complex samples a word.

## 3. The operation

`yr = gr xr - gi xi`, `yi = gr xi + gi xr`, computed exactly, then rounded to the nearest integer, a
tie toward +infinity, and saturated to int16 (`AP_RND`, `AP_SAT`).

Worked example: `g = 0.5 + 0.25j`, `x = 100 - 40j` -> `yr = 50 + 10 = 60`, `yi = -20 + 25 = 5`.

## 4. Errors

Samples that end early (TLAST on a word before the last) are fatal: the results of the words that
arrived, the burst closed short, `Status(tx_id, TLAST_EARLY)`, and the kernel stops taking `s_in`.

## 5. Acceptance

- Golden model with the worked example; pysim bit-exact in the kernel's harness at both widths.
- The harness through the system DAG to `compare` at both widths, including a scenario with the error
  in the middle and nothing answered after it.
- `results/report.md`: each gate, its number, and where it came from.
