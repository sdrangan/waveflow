---
title: The equalizer's protocol
parent: Block equalizer
nav_order: 2
---

# The protocol

The [command-response pattern](../../guide/systems/command_response.md) on 64-bit AXI4-Streams -- two
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

## The words

Each message is a [DataSchema](../../guide/schema/index.md) class in
[`block_eq.py`](../../../examples/block_eq/block_eq.py), and its words are the schema's serialization:

| burst | schema | words |
|---|---|---|
| the command | `CmdHdr(tx_id, nrows, m)`, three `U16` | 1 |
| the estimates | `3 * nrows` float32s -- `hr, hi, N` per row, two a word | `ceil(3 * nrows / 2)`; a row's three floats may straddle two words |
| the symbols | `2 * nrows * m` float32s -- `rr, ri` per symbol | `nrows * m`: one symbol a word |
| the results header | `Resp(tx_id, nrows, m)` | 1 |
| the results | `zr, zi` per symbol | one symbol a word |
| the status | `Status(tx_id, status)` | 1 |

The schema descriptions bound a command at 1 to 64 rows (`MAX_ROWS`, the rows of `g` the hardware
buffers) and 1 to 256 symbols a row (`MAX_M`). When the estimates' last word has a lane to spare, it is
zero.

## Errors are fatal

| status | when | what is written first |
|---|---|---|
| `BAD_NOISE` | some `N[i] <= 0`, the first such row `i` | the `Resp` header, the results of the rows before `i` (none if `i = 0`: no results burst) |
| `TLAST_EARLY` | the estimates or the symbols end early (TLAST on a word before the last) | the results of the symbols that arrived and have their row's `g` |

When both happen, the status reports the first cut in row-major order; at a tie, `BAD_NOISE`. Then the
kernel stops: it reads nothing more and answers nothing more, until a reset. Every lane of every word
that arrived counts -- a word cannot say that only some of its lanes are data.

## The arithmetic

Float32, in a fixed operation order, compared by its bits: on [The golden](golden.md#the-arithmetic).

Next, [The golden](golden.md).
