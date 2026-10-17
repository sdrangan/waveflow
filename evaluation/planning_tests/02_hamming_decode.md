# Planning test 2: a two-stage Hamming (8,4) soft decoder on a stream

Plan the Python and RTL models of the kernel below.  The deliverable -- what to write, and how -- is in
[deliverable.md](deliverable.md); read it first.

## 1. Purpose

The decoder after a soft demapper: each codeword arrives as eight LLRs, and the kernel picks the most
likely of the 16 codewords of an extended Hamming (8,4) code and outputs its four information bits.  The
algorithm is given in full below; the task is the hardware.

## 2. Architecture

A **free-running kernel** -- no start signal, no register map -- that runs forever, one command at a
time, until an error stops it.  It has three AXI4-Streams of **64-bit** words: a command and its LLRs in,
the decoded codewords out, a status out.  In verification, a **stream source** plays the input and
**stream sinks** record the two outputs.  Clock: 250 MHz.

**The kernel is two stages**, one after the other:

```text
                    +------------------- hamming_decode -------------------+
source --s_in--> [ correlate ] --c--> [ select ] --m_out-----> sink
                                                 --m_status--> sink
```

- **`correlate`** reads the command and the LLRs, and computes the 16 correlations `c[0..15]` of each
  codeword (below).  It passes them on to `select`, with the command.
- **`select`** picks the largest correlation of each codeword, writes the decoded bits and the
  correlation, and writes the status: the last stage answers.

Both stages handle **one codeword per cycle** in steady state.  What the link from `correlate` to `select`
carries, how it is laid out and how deep it is are yours to design, and your plan must say.

## 3. The boundary

The messages, their fields, widths and order are fixed below; how they pack into 64-bit words is yours,
and your plan must give each one's word layout.  Bits a message does not use are zero.  On every stream a
header and the data that follows it are each a burst; a status is a burst.  A burst ends with TLAST on its
last word.  `fixed<W, I>` is `W` bits with `I` integer bits including the sign (Vitis `ap_fixed<W, I>`).

**`s_in`** -- a command, then its LLRs:

- **the command**: `tx_id` (uint16), `ncw` (uint16, the codewords that follow, 1 to 4096);
- **the LLRs**: `ncw` codewords; codeword `n` is eight LLRs `z[n, 0] .. z[n, 7]`, each a signed
  `fixed<8, 6>` -- 64 bits, one word.  A positive LLR favours the bit being 0.

**`m_out`** -- a header, then the decoded codewords:

- **the header**: `tx_id`, `ncw` (uint16 each), echoed;
- **the decoded codewords**: `ncw` entries; entry `n` is `bits` (uint4: `u0` in bit 0 .. `u3` in bit 3) and
  `cmax` (signed `fixed<12, 10>`).

**`m_status`** -- one message per command, after its decoded codewords: `tx_id` (uint16) and `status`
(uint16: 0 `OK`, 1 `TLAST_EARLY`).  It is the **last command seen**.

## 4. The operation

**The code.**  The 16 codewords are `x_k = [u, u P] mod 2`, where `u = (u0, u1, u2, u3)` are the bits of
`k = u0 + 2 u1 + 4 u2 + 8 u3`, and

```text
P = 0 1 1 1
    1 0 1 1
    1 1 0 1
    1 1 1 0
```

**`correlate`**, for each codeword of LLRs `z`: for `k = 0 .. 15`,

```text
c[k] = sum over j = 0..7 of  S[k, j] * z[j],      S[k, j] = +1 if x_k[j] = 0,  -1 if x_k[j] = 1
```

computed exactly (eight terms of +-`z`, so `fixed<12, 10>` holds every `c[k]` with nothing lost).  The
table, written out -- hand-code it:

```text
 k  x_k        S[k, 0..7]
 0  00000000   + + + + + + + +
 1  10000111   - + + + + - - -
 2  01001011   + - + + - + - -
 3  11001100   - - + + - - + +
 4  00101101   + + - + - - + -
 5  10101010   - + - + - + - +
 6  01100110   + - - + + - - +
 7  11100001   - - - + + + + -
 8  00011110   + + + - - - - +
 9  10011001   - + + - - + + -
10  01010101   + - + - + - + -
11  11010010   - - + - + + - +
12  00110011   + + - - + + - -
13  10110100   - + - - + - + +
14  01111000   + - - - - + + +
15  11111111   - - - - - - - -
```

**`select`**: `k* = ` the index of the largest `c[k]`; **of equal largest values, the lowest `k` wins**.
The decoded entry is `bits = k*` (its four bits are `u0..u3`) and `cmax = c[k*]`.

**Normally**, after a command's decoded codewords the kernel writes `Status(tx_id, OK)` and takes the next
command.  Nothing carries over from one command to the next.

**An error is fatal.**  If the LLRs end early -- TLAST on a word before the last word `ncw` needs -- the
kernel writes the decoded entries of the codewords that arrived, closes the `m_out` burst with TLAST on the
last of them, writes `Status(tx_id, TLAST_EARLY)`, and **stops taking `s_in`**: only a reset recovers it.

### Worked examples

LLR values in real units (the stored integer is 4 times the value):

| `z[0..7]` | `k*` | `bits` (u3..u0) | `cmax` | what it shows |
|---|---|---|---|---|
| 0, 0, 0, 0, 0, 0, 0, 0 | 0 | 0000 | 0 | all 16 equal: the lowest `k` wins |
| 1, 1, 0, 0, 0, 0, 0, 0 | 0 | 0000 | 2 | a tie of `k` = 0, 4, 8, 12 |
| 2, -2, 2, -2, 2, -2, 2, -2 | 10 | 1010 | 16 | a clean codeword: `x_10 = 01010101` |
| 3, -1, 2.5, 2.25, -1.75, 0.75, -2.75, 1.5 | 2 | 0010 | 12.5 | a noisy codeword; the runner-up is `k` = 8 at 7.5 |
| 31.75 (all eight) | 0 | 0000 | 254 | the positive extreme |
| -32 (all eight) | 15 | 1111 | 256 | the largest `cmax` there is -- why `fixed<12, 10>`: `fixed<11, 9>` stops at 255.75 |

## 5. The test plan

Every scenario is a sequence of commands sent back to back, checked on both outputs: every header, entry
and status field, and TLAST.

| # | scenario | what it exercises |
|---|---|---|
| 1 | the worked examples, one command each | the table, the sums, the tie rule |
| 2 | every codeword `x_k` sent clean (LLR +-2), then each with one LLR flipped | correct decoding, and correction of one error |
| 3 | ties: inputs where 2, 4 and 16 correlations are equal | the lowest index wins |
| 4 | the extremes of `z` (-32 and 31.75), mixed | the range of `fixed<12, 10>` |
| 5 | `ncw = 1`; `ncw = 4096` | the smallest and the largest command |
| 6 | 20 commands from a fixed seed: random codewords, Gaussian noise on the LLRs, random `ncw` | everything mixed |
| 7 | good commands, then one whose LLRs end early, then more | the error: the short burst, its status, and nothing answered after it |
| 8 | one command, `ncw = 4096` | throughput |

**Acceptance:**

- a golden model of the operation, written independently of the kernel, reproducing the worked examples;
- the Python model identical to the golden on every scenario;
- the RTL identical to the golden on every scenario -- every header, entry and status field, and TLAST; in
  scenario 7, nothing on either output after the error's status;
- throughput at RTL: one codeword per cycle in steady state on scenario 8; report the cycle count of the
  command, from its first input word to its status.
