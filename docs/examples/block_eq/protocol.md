---
title: The equalizer's protocol
parent: Block equalizer
nav_order: 1
---

# The protocol

The [command-response pattern](../../guide/patterns/command_response.md) on 64-bit AXI4-Streams -- two
float32s, or one complex value, a word:

```text
s_in     : CmdHdr(tx_id, nrows, m) | est[0..nrows-1] = (hr, hi, N) | r[0..nrows*m-1]     each a burst
m_out    : Resp(tx_id, nrows, m)   | z[0..k-1]                                          each a burst
m_status : Status(tx_id, status)                                                       one per command
```

- **One input carries everything a command needs**: its shape, the estimates (three floats a row), the
  symbols (complex, real first, row-major). Three bursts.
- **The results** are headed by `Resp`, so a reader sizes them from the stream; **the status** is the last
  command seen.

## Errors are fatal

| status | when | what is written first |
|---|---|---|
| `BAD_NOISE` | some `N[i] <= 0`, the first such row `i` | the `Resp` header, the results of the rows before `i` (none if `i = 0`: no results burst) |
| `TLAST_EARLY` | the estimates or the symbols end early (TLAST on a word before the last) | the results of the symbols that arrived and have their row's `g` |

When both happen, the status reports the first cut in row-major order; at a tie, `BAD_NOISE`. Then the
kernel stops: it reads nothing more and answers nothing more, until a reset. Every lane of every word
that arrived counts -- a word cannot say that only some of its lanes are data.

## The arithmetic

Float32, one rounding per operation, in exactly this order -- a fused multiply-add rounds once and would
not match:

```text
gain, per row:              apply, per symbol:
  t1 = hr * hr                p1 = gr * rr
  t2 = hi * hi                p2 = gi * ri
  d  = t1 + t2                zr = p1 - p2
  d  = d + N                  p3 = gr * ri
  q  = 1 / d                  p4 = gi * rr
  gr = hr * q                 zi = p3 + p4
  gi = -(hi * q)
```

Results are compared **by their bits**, so `-0.0` and `+0.0` differ. The worked examples
(`worked_cmd()` in [`block_eq.py`](../../../examples/block_eq/block_eq.py)) include both: `gi = -(0 * q)`
is `-0.0`, and so is one result's imaginary part.

| `h` | `N` | `g` | `r` | `z` | bits of `z` |
|---|---|---|---|---|---|
| 1 | 1.0 | 0.5 - 0.0j | 2 + 4j | 1.0 + 2.0j | `3f800000 40000000` |
| | | | -1 + 0.5j | -0.5 + 0.25j | `bf000000 3e800000` |
| 2j | 0.5 | 0.0 - 0.44444445j | 1 + 1j | 0.44444445 - 0.44444445j | `3ee38e39 bee38e39` |
| | | | -3j | -1.3333334 - 0.0j | `bfaaaaab 80000000` |
| 3 + 4j | 0.25 | 0.118811876 - 0.15841584j | 3 + 4j | 0.99009895 + 0.0j | `3f7d7720 00000000` |
| | | | 1 - 2j | -0.1980198 - 0.3960396j | `be4ac5b4 becac5b4` |

Next, [The stages](stages.md).
