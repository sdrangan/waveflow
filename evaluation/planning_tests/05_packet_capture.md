# Planning test 5: packet capture -- two kernels, a shared memory and a host on a bus

Plan the Python and RTL models of the system below.  The deliverable -- what to write, and how -- is in
[deliverable.md](deliverable.md); read it first.

## 1. Purpose

The capture stage of a receiver.  A host hands the hardware a block of samples; the hardware finds where
the signal power crosses a threshold, stores a short window of samples at each such place in an on-chip
memory, and tells the host where each window is.  The host then reads the windows back.  The algorithm is
given in full below; the task is the system.

## 2. Architecture

```text
              +--------------------------------- DUT ---------------------------------+
host --AXI-MM--> [ crossbar ] --> [ device A: views | detect  ] --over the bus--> [ device B ]
  ^                    |          [ device B: views | capture ] --> [ memory ]              |
  |                    +--------> [ memory: a window onto the RAM ]                        |
  +--------------- interrupts from the devices' views ---------------------------------------+
```

- **`detect`** (kernel A) takes jobs from the host: a command and its samples.  It computes each sample's
  power, finds the detections (below), and sends `capture` each detection's window of samples, and then
  the end of the job.  It sends them **over the bus**, into `capture`'s input queue, **with flow
  control**: it never writes more than the queue has room for, and never holds the bus waiting.
- **`capture`** (kernel B) writes each window into the **shared on-chip memory**, at the slot the
  detection is assigned, and -- once the window is stored -- reports the detection to the host; at the
  end of the job it reports the job's end.
- **The memory** is on-chip, 4096 words of 32 bits, on the crossbar: `capture` writes it, the host reads it.
- **The host** (the testbench) is software of **two threads**: a writer that sends each job, and a
  reader that takes each job's reports and then reads each window from the memory.  At most **two jobs
  in flight**.  It **never polls**: every wait is on an interrupt.
- The DUT is the two devices, the memory and the crossbar.  Clock: 100 MHz.

How the two kernels are reached over the bus (which views), how `detect`'s messages to `capture` are laid
out, and how the flow control is sized are yours to design, and your plan must say.

## 3. The boundary: the host's view

The messages, their fields, widths and order are fixed below; their packing into 32-bit words is yours,
and your plan must give each layout.

**Host -> `detect`**, per job: the command, then the samples.

| field | type | meaning |
|---|---|---|
| `tx_id` | uint16 | the job's id, echoed in the reports |
| `n` | uint16 | samples that follow, 1 to 1024 |
| `thr` | uint32 | the power threshold |
| `len` | uint16 | the window length `L`, 1 to 64 |
| `slot0` | uint16 | the slot of the job's first detection, 0 to 63 |

then `n` samples, each a complex int16 (`xr`, `xi`; one 32-bit word).

**`capture` -> host**, per job: one report per detection, in order, then one end -- a **tagged union**,
each message beginning with its type:

| `type` (uint16) | the fields that follow |
|---|---|
| `DET` = 1 | `tx_id` (uint16); `index` (uint16): the sample index `k` of the detection within the job; `count` (uint16): the window's samples stored; `slot` (uint16): where |
| `END` = 2 | `tx_id` (uint16); `ndet` (uint16): the job's detections |

A report is sent only after its window is in the memory.

**The memory**: slot `s` is words `64 s` .. `64 s + 63`; a window's sample `j` is the word `64 s + j`, the
same complex int16 as on the way in.

## 4. The operation

For each sample, the power is `p[k] = xr[k]^2 + xi[k]^2`, exact (a uint32).  Over `k = 0 .. n-1`:

- if `p[k] >= thr`, sample `k` is a **detection**: its window is the samples `k .. k + L - 1` (cut short at
  the end of the job), stored in slot `(slot0 + d) mod 64`, where `d` is the detection's number in the job
  (0, 1, ...); then the scan continues **after the window**, at `k + L`;
- otherwise the scan continues at `k + 1`.

The host never has more windows in flight than slots, and reads each window before its slot is reused.

### Worked example

`tx_id = 5, thr = 64, L = 3, slot0 = 10`, samples
`1+1j, 3-2j, 10+0j, 2+2j, -1+0j, 0+9j, 1+1j, 0+0j, 7-7j, 1+0j` (`n = 10`):

- powers `2, 13, 100, 8, 1, 81, 2, 0, 98, 1`;
- `k = 2` (100): a detection, window `k = 2..4` in slot 10; the scan resumes at 5;
- `k = 5` (81): a detection, window `k = 5..7` in slot 11; the scan resumes at 8;
- `k = 8` (98): a detection, window `k = 8..9` -- cut short, `count = 2` -- in slot 12;
- reports: `DET(5, 2, 3, 10)`, `DET(5, 5, 3, 11)`, `DET(5, 8, 2, 12)`, `END(5, 3)`.

## 5. The test plan

| # | scenario | what it exercises |
|---|---|---|
| 1 | the worked example | the rule, the windows, the slots |
| 2 | no sample at or above `thr` | `END` alone |
| 3 | a detection in the last sample; windows cut short | the end of the job |
| 4 | every sample above `thr`, `L = 1` | a detection per sample: the bus link at its fullest |
| 5 | `slot0` near 63 | slots wrapping |
| 6 | 16 jobs from a fixed seed, random `n`, `thr`, `L`, two in flight | everything mixed |
| 7 | one job, `n = 1024`, detections every ~100 samples, `L = 64` | throughput |

**Acceptance:**

- each kernel verified **alone** first: `detect` against the golden's detections and windows, `capture`
  against the golden's memory writes and reports;
- the system in pysim: every report the host reads, and every window it reads from the memory, equal to the
  golden's;
- the system at RTL: the host's reads and writes on the bus identical to pysim's, the host finished, **zero
  polls**;
- the RTL's cycle count for scenario 7 recorded, and the Python model's -- built from each kernel's timing
  calibrated alone, nothing fitted to the system -- within 5% of it.
