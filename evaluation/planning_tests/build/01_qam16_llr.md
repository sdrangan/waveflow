# A 16-QAM soft demapper on a stream

The kernel to build (the same spec as planning test 1; the task is in the build prompt beside this file).

## 1. Purpose

The soft demapper of a receiver.  Each received sample is a noisy 16-QAM symbol; the kernel computes,
for each of the symbol's four bits, a log-likelihood ratio (LLR) -- how strongly the sample says the
bit is 0 rather than 1 -- for the decoder downstream.

## 2. Architecture

One **free-running kernel** -- no start signal, no register map -- that runs forever, one command at a
time.  It has three AXI4-Streams of `WORD_BW`-bit words: a command and its samples in; the LLRs out,
headed by a response; a status out.  In verification, a **stream source** plays the input and **stream
sinks** record the two outputs.  Clock: 250 MHz.

**`WORD_BW` is a build-time parameter**, one of 32, 64 and 128, the same on all three streams.  A
complex sample is 32 bits and so is a sample's LLR entry, so a word carries `PF = WORD_BW / 32`
samples in, and `PF` entries out.  The kernel must sustain **`PF` samples per cycle**: one input word
and one output word every cycle, in steady state, at every width.

```text
source --s_in--> [ qam16_llr ] --m_llr----> sink
                               --m_status-> sink
```

## 3. The boundary

The messages, their fields, widths and order are fixed below.  **Packing is yours**: pack every
message into `WORD_BW`-bit words as you choose, and document each one's word layout, at every width.
Bits a message does not use -- the unused lanes of a last word, say -- are zero.  On `s_in` the command
and the samples are each a burst; on `m_llr`, likewise, the header and the LLRs are each a burst; each
status is a burst.  A burst ends with TLAST on its last word.

The fixed-point formats are written `fixed<W, I>`: `W` bits in all, `I` of them integer bits
(including the sign for a signed format), so `W - I` fractional bits -- the meaning of Vitis HLS
`ap_fixed<W, I>` / `ap_ufixed<W, I>`.

**`s_in`** -- a command, then its samples:

- **the command**, one message:

  | field | type | meaning |
  |---|---|---|
  | `tx_id` | uint16 | the sender's command id, echoed in the response |
  | `nsamp` | uint16 | samples that follow, 1 to 4096 |
  | `invnoise` | unsigned `fixed<16, 8>` | the inverse noise scale `1 / N0`, 0 to 255.996 |

- **the samples**, one message of `nsamp` complex values `r[0] .. r[nsamp-1]`; each is two signed
  `fixed<16, 8>` values, the real part `rI` and the imaginary part `rQ`.

**`m_llr`** -- per command, a response header, then the LLRs, framed as `s_in` is: the header one
burst, the LLRs another.

- **the response header**: `tx_id` (uint16), echoed; `nsamp` (uint16), echoed -- so a reader can size
  the LLRs that follow without having seen the command;
- **the LLRs**: `nsamp` entries, one per sample; entry `i` is four LLRs `llr[i, 0] .. llr[i, 3]` in that
  order, each a signed `fixed<8, 6>` (range -32 to 31.75, step 0.25).

**`m_status`** -- one message per command, written after the command's LLRs.  It is the **last command
seen**: on a bus it would become a status register, read to see that the kernel is alive and what it
last did.

| field | type | meaning |
|---|---|---|
| `tx_id` | uint16 | the command it reports on |
| `status` | uint16 | 0 `OK`, 1 `TLAST_EARLY` |

## 4. The operation

**The constellation.**  The samples are scaled so that the 16-QAM points sit at `rI, rQ` in
`{-3, -1, +1, +3}`.  Each dimension carries two bits, Gray-coded:

| level | -3 | -1 | +1 | +3 |
|---|---|---|---|---|
| sign bit | 1 | 1 | 0 | 0 |
| amplitude bit | 1 | 0 | 0 | 1 |

Bits 0 and 1 of a symbol are the sign and amplitude bits of `rI`; bits 2 and 3, those of `rQ`.

**The LLRs** (max-log, exact for this constellation).  For one dimension with value `y`:

```text
Ls(y) = 4 y              if |y| <= 2
        8 (y - 1)        if y > 2
        8 (y + 1)        if y < -2

La(y) = 4 (2 - |y|)
```

and for sample `i`:

```text
llr[i, 0] = Q( invnoise * Ls(rI[i]) )
llr[i, 1] = Q( invnoise * La(rI[i]) )
llr[i, 2] = Q( invnoise * Ls(rQ[i]) )
llr[i, 3] = Q( invnoise * La(rQ[i]) )
```

A positive LLR favours the bit being 0.

**The arithmetic.**  `Ls`, `La` and the product with `invnoise` are computed **exactly** -- every
intermediate wide enough that nothing is lost.  `Q` is the one rounding: to the nearest multiple of
0.25, a tie rounded toward +infinity, then saturated to [-32, 31.75] -- Vitis HLS's `AP_RND` and
`AP_SAT` for a `fixed<8, 6>`.

**Normally**, after a command's LLRs the kernel writes `Status(tx_id, OK)` and takes the next command.
Nothing carries over from one command to the next.

**An error is fatal.**  If the samples end early -- TLAST on a word before the last word `nsamp`
needs -- the kernel:

1. writes an entry for **every lane of every sample word that arrived** (`PF` per word: a stream word
   cannot say that only some of its lanes are samples) and closes the `m_llr` burst with TLAST on the
   last of them, so the burst is shorter than its header announced;
2. writes `Status(tx_id, TLAST_EARLY)`;
3. **stops taking `s_in`**: it reads nothing more and answers nothing more.  Only a reset recovers it.

An early TLAST is the sender's bug, not an operating condition.  The sender never sends more samples
than `nsamp`, and pads the unused lanes of its last word with zeros.  An early TLAST therefore always
ends a whole word, and the entries for that word's lanes are computed like any others.

### Worked examples

| `invnoise` | `r` | `llr[i, 0..3]` | what it shows |
|---|---|---|---|
| 1.0 | (1.0, -3.0) | 4.0, 4.0, -16.0, -4.0 | on the constellation points |
| 2.0 | (2.5, 0.125) | 24.0, -4.0, 1.0, 15.0 | the outer segment of `Ls` (`y > 2`) |
| 4.0 | (3.0, -3.0) | 31.75, -16.0, -32.0, -16.0 | saturation, both ends |
| 0.5 | (0.0625, -0.0625) | 0.25, 4.0, 0.0, 4.0 | ties: 0.125 -> 0.25, 3.875 -> 4.0, -0.125 -> 0.0 |

## 5. The test plan

Every scenario is a sequence of commands sent back to back, checked on both output streams: every
header, entry and status field, and TLAST.

| # | scenario | what it exercises |
|---|---|---|
| 1 | the worked examples, one command each, in one sequence | the formulas, the rounding, nothing carried over |
| 2 | every constellation point, `invnoise = 1.0` | the sixteen symbols' signs and Gray bits |
| 3 | `y` at and around the breakpoints: -2, 2, ±(2 ± 2^-8), 0 | the segments of `Ls`, and `|y|` at 0 |
| 4 | the extremes of `r` (-128, 127.996) and of `invnoise` (0, 255.996) | saturation; an `invnoise` of 0 gives all-zero LLRs |
| 5 | rounding ties, positive and negative | `AP_RND`, not round-half-even or truncation |
| 6 | `nsamp = 1`; `nsamp = 4096`; at `PF > 1`, every `nsamp` from 1 to `2 PF + 1` | the smallest and the largest command; last words partly filled |
| 7 | two good commands, then one whose samples end early (TLAST on a word before the last), then two more | the error: the short burst, its status, and **nothing** answered after it -- the kernel has stopped |
| 8 | 20 commands from a fixed seed: constellation points plus Gaussian noise, random `invnoise` and `nsamp` | everything mixed |
| 9 | one command, `nsamp = 4096` | throughput |

Every scenario runs at each `WORD_BW`: 32, 64 and 128.

**Acceptance:**

- a golden model of the operation, written independently of the kernel, reproducing the worked
  examples;
- the Python model identical to the golden on every scenario;
- the RTL identical to the golden on every scenario -- every header, LLR and status field, and TLAST;
  in scenario 7, nothing on either output after the error's status;
- throughput at RTL: `PF` samples per cycle in steady state on scenario 9, at each `WORD_BW`; report
  the cycle count of the command at each width, from its first input word to its status.
