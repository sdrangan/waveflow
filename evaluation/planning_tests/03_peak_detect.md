# Planning test 3: a peak detector on a stream

Plan the Python and RTL models of the kernel below.  The deliverable -- what to write, and how -- is
in [deliverable.md](deliverable.md); read it first.

## 1. Purpose

The detection stage of a packet receiver.  Upstream, a correlator produces a non-negative metric per
sample; it is high where a packet's preamble lines up with the received signal.  This kernel finds
the peaks of that metric and reports where they are.

## 2. Architecture

A **free-running kernel** -- no start signal, no register map -- that runs forever, one command at a
time.  It has three AXI4-Streams of `WORD_BW`-bit words: commands and metrics in, the detections out,
a status out.  In verification, a **stream source** plays the input and **stream sinks** record the two
outputs.  Clock: 250 MHz.

**`WORD_BW` is a build-time parameter**, one of 32, 64 and 128, the same on all three streams.  A
metric is 32 bits, so a word carries `PF = WORD_BW / 32` metrics.

**The kernel is two stages**, because the detections arrive at a rate that depends on the data.  With
`W = 1, D = 0` every metric can be a detection, and a detection takes more output than a metric takes
input, so a single loop cannot both read a word every cycle and write every detection.

```text
                    +---------------- peak_detect ----------------+
source --s_in--> [ detect ] ==wide==> [ serialize ] --m_det----> sink
                                                    --m_status-> sink
```

- **`detect`** reads `PF` metrics a cycle and runs the search below across them, carrying its state
  from lane to lane and word to word.  For each input word it passes on the detections that word
  produced -- none, or up to `PF` -- together with the command's identity and any error.
- **The wide link** between the stages carries that, and buffers it: a burst of detections waits there
  while `serialize` catches up.
- **`serialize`** writes the command's `START`, its `DET`s and its `END` onto `m_det`, then its status
  onto `m_status`.

How the wide link's messages are laid out, how deep it is, and how `detect` evaluates the search over
`PF` lanes in one cycle are yours to design, and your plan must say.

## 3. The boundary

The messages, their fields, widths and order are fixed below; how they pack into `WORD_BW`-bit words
is yours (see *Packing is yours*, at the end of this section).  On `s_in` the command and the metrics
are each a burst; on `m_det` a command's messages together are one burst; each status is a burst.  A
burst ends with TLAST on its last word.

**`s_in`** -- a command, then its metrics:

- **the command**, one message:

  | field | width | meaning |
  |---|---|---|
  | `tx_id` | uint16 | the sender's command id, echoed in the results |
  | `n` | uint16 | metrics that follow, 1 to 4096 |
  | `threshold` | uint32 | T: a metric at or above T starts a peak search |
  | `window` | uint16 | W: the samples a search covers, at least 1 |
  | `dead` | uint16 | D: samples ignored after a search ends, 0 or more |

- **the metrics**, one message of `n` values `m[0] .. m[n-1]`, each uint32.

**`m_det`** -- per command, **one burst**: a `START` message, one `DET` message per peak, then an `END`
message, TLAST on the last word of `END`.  A message on `m_det` is a **tagged union**: a type field,
then the fields of that type.

| `resp_type` (uint16) | the fields that follow |
|---|---|
| `START` = 0 | `tx_id` (uint16), echoed |
| `DET` = 1 | `index` (uint16): the sample index `k` of the peak within its command; `peak` (uint32): the metric at that index |
| `END` = 2 | `tx_id` (uint16), echoed; `count` (uint16): the `DET` messages written |

The number of detections is not known until the metrics have been read, so the end is in band: a
downstream reader knows a command's detections have ended when it reads `END`.  A command with no
detection writes `START` then `END`.

**`m_status`** -- one message per command, written after its `END`.  It is the **last command seen**:
on a bus it would become a status register, read to see that the kernel is alive and what it last did.

| field | type | meaning |
|---|---|---|
| `tx_id` | uint16 | the command it reports on |
| `status` | uint16 | 0 `OK`, 1 `TLAST_EARLY`, 2 `BAD_PARAM` |

**Packing is yours.**  Pack every message into `WORD_BW`-bit words as you choose, and document the
choice, at every width: each message's word layout, and for `m_det`, how a reader tells the types
apart.  The `m_det` messages may all have the same length -- the shorter ones padded to the longest --
or each its own; say which, and why.  Several messages may share a word or not, as you choose.  Bits a
message does not use are zero.

## 4. The operation

Over `k = 0 .. n-1`, starting **idle**, with nothing carried over from an earlier command:

- **Idle.**  If `m[k] >= T`, a search starts at `k`: the best so far is `(k, m[k])`, and the search
  covers samples `k .. k+W-1`.
- **Searching.**  At each later sample in the search, if `m[k]` is **strictly** greater than the best,
  it becomes the best (so of equal maxima the earliest wins).  When the search's last sample has been
  taken, the best is written as a `DET`, and the next `D` samples are ignored.
- **Ignoring.**  The `D` samples after a search ends cannot start a search; then idle again.
- **The end of the metrics** -- the `n`-th sample, or an early TLAST -- ends a search in progress: its
  best is written as a `DET`, though the search covered fewer than `W` samples.

**Normally**, after a command's `END` the kernel writes `Status(tx_id, OK)` and takes the next command.

**An error is fatal**: the kernel reports it on `m_status` and then **stops taking `s_in`** -- it reads
nothing more and answers nothing more; only a reset recovers it.  An error is the sender's bug, not an
operating condition.

- **TLAST early**: the metrics end on a word before the last word `n` needs.  The kernel processes
  **every lane of every word that arrived** (`PF` per word: a stream word cannot say that only some of
  its lanes are metrics; the sender pads its last word with zeros) -- a search in
  progress is ended and written as a `DET`, as at the end of the metrics -- then closes the `m_det` burst
  with TLAST on the last message it wrote (`START`, if there was no detection), **without** an `END`;
  then `Status(tx_id, TLAST_EARLY)`; then it stops.
- **`W = 0`, or `n = 0`**: nothing on `m_det`; `Status(tx_id, BAD_PARAM)`; then it stops.  It reads
  nothing after the command.

The sender never sends more metrics than `n`.

### Worked examples

`tx_id = 7, T = 10, W = 3, D = 2`, `m = [0, 12, 15, 9, 11, 30, 2, 40, 1]` (`n = 9`):

- `k = 1`: 12 >= 10, a search over 1..3; the best becomes 15 at `k = 2`; the search ends at `k = 3` --
  `DET(2, 15)`.
- `k = 4, 5` are ignored -- the 30 at `k = 5` starts nothing.
- `k = 6`: 2 < 10.  `k = 7`: 40 starts a search over 7..9; the metrics end at `k = 8` -- `DET(7, 40)`.
- On `m_det`: `START(7)`, `DET(2, 15)`, `DET(7, 40)`, `END(7, count = 2)`; on `m_status`: `Status(7, OK)`.

`tx_id = 8, T = 5, W = 4, D = 0`, `m = [5, 7, 7, 3, 6]`:

- `k = 0` starts a search over 0..3; 7 at `k = 1` is the best; the equal 7 at `k = 2` is not -- `DET(1, 7)`.
- `k = 4`: 6 >= 5 starts a search; the metrics end -- `DET(4, 6)`.
- On `m_det`: `START(8)`, `DET(1, 7)`, `DET(4, 6)`, `END(8, count = 2)`; on `m_status`: `Status(8, OK)`.

## 5. The test plan

Every scenario is a sequence of commands sent back to back, and is checked message by message: every
message on `m_det` and `m_status`, every field, in order, and TLAST.

| # | scenario | what it exercises |
|---|---|---|
| 1 | the two worked examples, in one sequence | the rule; nothing carried over |
| 2 | equal maxima in a window | the earliest wins |
| 3 | every metric below T | `START` then `END`, `count = 0` |
| 4 | a peak in the last sample, and a search cut short by the end | the end of the metrics ends a search |
| 5 | peaks closer together than `W + D` | windows and dead time suppress the later ones |
| 6 | `T = 0` (every idle sample starts a search), `T = 0xFFFFFFFF`, metrics of `0xFFFFFFFF` | the extremes of the comparison |
| 7 | `W = 1, D = 0` | every sample at or above T is its own detection |
| 7b | `W = 1, D = 0, T = 0`, `n = 4096` | **dense**: a detection on every metric -- correct, though the input stalls |
| 7c | at `PF > 1`: searches that start, end and are ignored in every lane position, and cross word boundaries | the search carried across lanes and words |
| 8 | good commands, then one whose metrics end early in the middle of a search, then more | the error: the detection it still owes, the burst closed without `END`, its status, and **nothing** answered after it |
| 9 | good commands, then `W = 0` with metrics following, then more; separately, the same with `n = 0` | `BAD_PARAM`: nothing on `m_det`, its status, and the kernel stopped |
| 10 | 20 commands from a fixed seed, `n` up to 4096, random parameters | everything mixed |
| 11 | one command, `n = 4096`, `W = 16`, `D = 64`, peaks seeded, on average no more than one detection per 16 metrics | sparse throughput |

Every scenario runs at each `WORD_BW`: 32, 64 and 128.

**Acceptance:**

- a golden model of the operation, written independently of the kernel, reproducing both worked
  examples message for message;
- the Python model identical to the golden on every scenario;
- the RTL identical to the golden on every scenario -- every message, field and TLAST; in scenarios 8
  and 9, nothing on either output after the error's status;
- **sparse throughput** at RTL: `PF` metrics per cycle in steady state on scenario 11, at each
  `WORD_BW`;
- **dense** (scenario 7b): correct at every width; the input may stall, and the plan says what limits
  it;
- report the cycle count of scenarios 11 and 7b at each width, from the command's first input word to
  its status.
