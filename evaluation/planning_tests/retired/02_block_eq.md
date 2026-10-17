# Planning test 2: a block equalizer on two streams

Plan the Python and RTL models of the kernel below.  The deliverable -- what to write, and how -- is
in [deliverable.md](deliverable.md); read it first.

## 1. Purpose

The equalizer of a receiver.  Symbols `s[i, j]` were sent over a channel that scales row `i` by a
complex gain `h[i]` and adds noise of variance `N[i]`:

```text
r[i, j] = h[i] s[i, j] + w[i, j],      var(w[i, j]) = N[i]
```

An estimator upstream supplies `h[i]` and `N[i]` for each row.  The kernel computes, for each row, the
MMSE equalizer `g[i]`, and applies it to every symbol of the row: `z[i, j] = g[i] r[i, j]`.

## 2. Architecture

A **free-running kernel** -- no start signal, no register map -- that runs forever, one command at a
time, until an error stops it.  It has four AXI4-Streams of `WORD_BW`-bit words: the channel estimates
in, the received symbols in, the equalized symbols out, a status out.  In verification, **stream
sources** play the two inputs and **stream sinks** record the two outputs.  Clock: 250 MHz.

**`WORD_BW` is a build-time parameter**, 64 or 128, the same on all four streams.  A complex value is
two float32s, 64 bits, so a word carries `PF = WORD_BW / 64` symbols.

**The kernel is two stages**, at two rates:

```text
                      +--------------------- block_eq ---------------------+
source --s_est--> [ gain ] --g--> [ apply ] <--s_sym-- source
                                              --m_out-----> sink
                                              --m_status--> sink
```

- **`gain`** reads a command and its estimates, and computes `g[i]` for each row -- a division, once
  per row.  It passes each `g[i]` on to `apply`, with the command.
- **`apply`** reads the symbols, a row at a time, and multiplies each by its row's `g[i]` -- once per
  symbol.  It checks that the two inputs describe the same command, writes the equalized symbols, and
  writes the status: the last stage answers.

What the link from `gain` to `apply` carries, how it is laid out and how deep it is are yours to
design, and your plan must say.

## 3. The boundary

The messages, their fields, widths and order are fixed below; how they pack into `WORD_BW`-bit words is
yours, and your plan must give each one's word layout at each width.  Bits a message does not use are
zero.  On every stream a header and the data that follows it are each a burst; a status is a burst.  A
burst ends with TLAST on its last word.  A complex value is two float32s, the real part first.

**`s_est`** -- a command, then its estimates:

- **the command**: `tx_id` (uint16), `nrows` (uint16, 1 to 64), `m` (uint16, the symbols per row, 1 to
  256);
- **the estimates**: `nrows` entries; entry `i` is `h[i]` (complex) then `N[i]` (float32).

**`s_sym`** -- a header, then the symbols:

- **the header**: `tx_id`, `nrows`, `m` (uint16 each), the same values as the command's;
- **the symbols**: `nrows * m` complex values `r[i, j]`, row-major (all of row 0, then row 1, ...).

**`m_out`** -- a header, then the results:

- **the header**: `tx_id`, `nrows`, `m` (uint16 each), echoed -- so a reader sizes the results from it;
- **the results**: `nrows * m` complex values `z[i, j]`, row-major.

**`m_status`** -- one message per command, after its results: `tx_id` (uint16) and `status` (uint16): 0
`OK`, 1 `TLAST_EARLY`, 2 `MISMATCH`, 3 `BAD_NOISE`.  It is the **last command seen**: on a bus it would
become a status register.

## 4. The operation

In float32, one rounding per operation, in exactly this order (a fused multiply-add rounds once and
does not match):

```text
gain, per row i:                          apply, per symbol (i, j):
  t1 = hr * hr                              p1 = gr * rr
  t2 = hi * hi                              p2 = gi * ri
  d  = t1 + t2                              zr = p1 - p2
  d  = d + N                                p3 = gr * ri
  q  = 1 / d                                p4 = gi * rr
  gr = hr * q                               zi = p3 + p4
  gi = -(hi * q)
```

where `h[i] = hr + j hi`, `g[i] = gr + j gi`, `r[i, j] = rr + j ri`, `z[i, j] = zr + j zi`.  Results are
compared **by their bits**: `-0.0` and `+0.0` differ.

**Normally**, after a command's results the kernel writes `Status(tx_id, OK)` and takes the next
command.  Nothing carries over from one command to the next.

**An error is fatal**: the kernel writes what the error allows on `m_out` (below), writes the status,
and **answers nothing more**: it reads no further symbols, and writes nothing more on either output.
`gain` may already have read ahead on `s_est`; it need not be stopped there.  Only a reset recovers
the kernel.

- **`MISMATCH`**: the two headers disagree on `tx_id`, `nrows` or `m`.  Nothing on `m_out` -- not even
  its header.
- **`BAD_NOISE`**: some `N[i] <= 0`, the first such row `i`.  The `m_out` header is written, then the
  results of the rows before `i`, the burst closed with TLAST on the last of them; if `i = 0` there are
  no results, and no results burst (a burst cannot be empty).  Row `i` and after are not written.
- **`TLAST_EARLY`**: either input's data ends on a word before the last word it needs (an estimates
  entry that is cut short makes its row invalid).  The results of
  **every symbol lane of every word that arrived** whose row has its `g[i]` are written, and no more (a
  stream word cannot say that only some of its lanes are symbols; a sender pads its last word with
  zeros).

**One status per command, and one error.**  When several things are wrong, the status reports the
first in this order: `MISMATCH` (the headers are compared before any data); then whichever of
`BAD_NOISE` and `TLAST_EARLY` cuts the results earlier in row-major order; at a tie, `BAD_NOISE`.

### Worked examples

One command, `nrows = 3`, `m = 2`: each row's two symbols are the two lines of its group.

| `h[i]` | `N[i]` | `g[i]` | `r[i, j]` | `z[i, j]` | bits of `z` |
|---|---|---|---|---|---|
| 1 + 0j | 1.0 | 0.5 - 0.0j | 2 + 4j | 1.0 + 2.0j | `3f800000`, `40000000` |
| | | | -1 + 0.5j | -0.5 + 0.25j | `bf000000`, `3e800000` |
| 0 + 2j | 0.5 | 0.0 - 0.44444445j | 1 + 1j | 0.44444445 - 0.44444445j | `3ee38e39`, `bee38e39` |
| | | | 0 - 3j | -1.3333334 - 0.0j | `bfaaaaab`, `80000000` |
| 3 + 4j | 0.25 | 0.118811876 - 0.15841584j | 3 + 4j | 0.99009895 + 0.0j | `3f7d7720`, `00000000` |
| | | | 1 - 2j | -0.1980198 - 0.3960396j | `be4ac5b4`, `becac5b4` |

Note the signed zeros: `gi = -(0 * q)` is `-0.0` in the first row, and `zi = p3 + p4` is `-0.0` in the
fourth.

## 5. The test plan

Every scenario is a sequence of commands sent back to back -- the estimates on `s_est`, the symbols on
`s_sym` -- checked on both outputs: every header, result and status, by their bits, and TLAST.

| # | scenario | what it exercises |
|---|---|---|
| 1 | the worked examples: one command, `nrows = 3`, `m = 2` | the arithmetic, the operation order, signed zeros |
| 2 | `nrows = 1, m = 1`; `nrows = 64, m = 256` | the smallest and the largest command |
| 3 | `m = 1` for every row, `nrows = 64` | `gain` sets the pace: a division per symbol |
| 4 | at `PF > 1`, `m` not a multiple of `PF`, and rows that start mid-word | rows across word boundaries |
| 5 | `h = 0` (so `g = 0`), very large and very small `|h|`, `N` from 1e-6 to 1e6 | the range of the division |
| 6 | 20 commands from a fixed seed, random `nrows`, `m`, `h`, `N`, `r` | everything mixed |
| 7 | good commands, then a `MISMATCH`, then more | the check, and nothing answered after it |
| 8 | good commands, then `N[i] = 0` in a middle row, then more; separately, `N[0] = 0` | `BAD_NOISE`: the rows before it (none for row 0: the header, no results burst), then the stop |
| 9 | good commands, then symbols ending early mid-row, then more | `TLAST_EARLY` |
| 10 | one command, `nrows = 16, m = 64` | throughput |

Every scenario runs at each `WORD_BW`: 64 and 128.

**Acceptance:**

- a golden model of the operation, written independently of the kernel, reproducing the worked
  examples bit for bit;
- the Python model identical to the golden on every scenario;
- the RTL identical to the golden on every scenario -- every header, result and status, by their bits,
  and TLAST; in scenarios 7 to 9, nothing on either output after the error's status;
- throughput at RTL: `PF` symbols per cycle in steady state on scenario 10, at each `WORD_BW`; for
  scenario 3, report the cycle count and what limits it;
- report the cycle counts of scenarios 3 and 10 at each width, from the command's first input word to
  its status.
