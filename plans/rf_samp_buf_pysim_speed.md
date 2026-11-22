# Plan: pysim's per-sample cost in `RfSampBufRx` (and in `HwModule.__setattr__`)

**Status: (b) and (c) DONE 2026-11-21, (a) deferred -- see §6, *As built*, which also corrects §2.**
Written 2026-11-20. A finding from the toy experiment's lab calibration
(`plans/toy_shared_memory.md`, step S0b, task L1). That plan waits on this fix: its question is how
much faster pysim is than RTL, and on the sample buffer the answer was set by this overhead rather
than by pysim's design. Line numbers below are at `main` `00eafcac`.

## 1. What was measured

L1 timed the design `examples/rf_samp_buf_rx` (the RX sample buffer fed by the RFDC model) in XSI
and in pysim, over the same long scenario:

- the converter delivers **256-sample blocks**, **one 16-bit sample per AXI-Stream word**
  (`samp_per_word = 1`), at 64 MSPS into the 250 MHz fabric: a block every 1,000 cycles,
  0.256 samples a cycle;
- a 256-sample capture command every 8,192 samples.

| | cycles/s | note |
|---|---|---|
| XSI (Vivado 2025.1), alone | 278,000 | 10^7 cycles; build 6.2 s |
| pysim, unprofiled | 1,254,823 | 1,005,257 cycles (1,004 blocks, 257,024 samples) in 0.80 s |

So pysim was only **4.5x** the RTL here, against about 300x on the CG system. Pysim costs about
**3 µs per sample**: the scheduler work is small (about 48 SimPy steps a block), and the rest is
Python run once per sample.

Results: `~/sundeep/toy/lab/L1.json` and `NOTE.md`. The lab code is on the local, unpushed branch
`lab/toy_s0b`, checked out at `/home/wirelesslab914/sundeep/waveflow-s0b`
(`examples/toy_s0b_lab/l1_speed.py`).

## 2. The profile

`cProfile` of the 10^6-cycle pysim run (1.99 s under the profiler), top entries by own time:

| function | calls | own s | cumulative s |
|---|---|---|---|
| `HwModule.__setattr__` (`waveflow/hw/hw_module.py:877`) | 259,005 | 0.310 | 0.534 |
| builtin `getattr` (mostly from `__setattr__`) | 1,815,445 | 0.225 | 0.225 |
| `RfSampBufIngress.run_iter` (`waveflow/hw/rf_samp_buf.py:304`) | 7,917 | 0.177 | **1.125** |
| `BramIFMaster.write` (`waveflow/hw/bram.py:357`) | 257,024 | 0.154 | 0.356 |
| `T2pBram._check` / `store` / `_memory` / `_check_access` | ~260k each | 0.21 together | |
| `RfSampBufCapture.run_iter` (`rf_samp_buf.py:481`) | 8,687 | 0.026 | 0.218 |

Three hot spots, in order of cost.

**(a) `HwModule.__setattr__` is slow for every model, not only this one.** After construction,
each attribute assignment walks the class's MRO to refuse reassigning an `HwParam` field. With
`from __future__ import annotations` the hints are strings, and they are `eval`'d when the name
matches. An attribute that is *not* an `HwParam` (here `self.wr`) checks every class in the MRO,
about 7 `getattr`s a call. About 27 % of this run, and it is paid by any pysim body that assigns an
attribute per sample or per word, anywhere in the framework.

**(b) The ingress writes one sample at a time** (`rf_samp_buf.py:348-350`):

```python
for x in raw:
    self.buf_w.write((self.wr // spw) & mask, int(x))
    self.wr = (self.wr + spw) % wrap
```

Each sample pays a port write (direction check, memory lookup, range check, store) and a
`__setattr__`. Together with (a) this loop is about 57 % of the run. `bram.py`'s own comment above
`store_block` (line ~808) says a per-element loop is what the block operations exist to remove.

**(c) The capture reads and emits one word at a time** (`rf_samp_buf.py:502-526`), about 11 %.
It is harder to change: it waits, per word, until the ingress has written that word, and its output
is one `s_out.write` per word.

Also noticed, not investigated: `RfSampBufIngress.run_iter` fires about 7.9 times per 256-sample
block (7,917 times for 1,004 blocks), so the converter's block reaches the ingress in pieces of
about 32 words. A fix must handle chunks of any length.

> **Correction (2026-11-21): the ingress fires once per block.** cProfile counts every *resumption*
> of a generator as a call, so 7,917 is resumptions, not firings. Counted directly: 44 firings of
> 256 words each for 44 blocks. The requirement to handle any length stands (the module does not
> know the block size), but the converter does not split its blocks.

## 3. The work

1. **Measure the baseline again on `main`**, alone on the machine (the account is shared; check
   `ps` for other tool jobs first). Use the scenario of §1: copy `RfLongSystem` from
   `l1_speed.py` (lines 69-106) into a script in your scratchpad, run
   `RfLongSystem(n_cycles=10**6, n_blk=1004)` after `write_scenario(tmpdir)`, and time
   `sysm.run()`. `cmd_pysim` in the same file is the exact code L1 ran. Also time the `mem_copy`
   scenario (`mc_system(n_cycles=10**6, n_jobs=1626)`, L1's 1,222,178 cycles/s), because (a) may
   move it too. Its golden check fails by construction (the jobs reuse source regions); time it
   anyway.
2. **Fix (a).** Keep the refusal of reassigning an `HwParam` after construction exactly as it is
   (find its tests: `grep -rn "Cannot reassign HwParam" tests/`). Make the check O(1): for example,
   the set of `HwParam` field names per class, computed once (lazily on first use, so forward
   references resolve) and cached. On an `eval` failure, keep today's behaviour: the name is not
   treated as an `HwParam`.
3. **Fix (b).** Write the whole chunk in one operation and update `self.wr` once. The loop has no
   `yield` in it, so this is observably the same if the final memory contents and `self.wr` are the
   same. Keep:
   - the address of each word exactly as the loop computes it, `((wr0 + spw*k) % 2**IDX_BW // spw)
     & mask`. Do not assume the addresses are contiguous unless you prove it. Only power-of-two
     `depth` and `samp_per_word` are enforced, not `depth * samp_per_word <= 2**IDX_BW`;
   - the last write wins when a chunk is longer than the buffer. numpy does not define which value
     lands for a repeated index in a fancy assignment, so dedupe (keep the last) or split into
     contiguous slices;
   - the port's direction check and the memory's range check, which refuse rather than wrap;
   - the values bit-exact (the loop stores `int(x)` of `uint64` words into the memory's dtype);
   - the timing: the `timeout` and the `offer` after it stay as they are.

   `T2pBram.store_block` (`bram.py:813`) exists; `BramIFMaster` has a per-element `write` and a
   `write_pipelined` (line 495). Read what the latter charges before using it: the ingress charges
   its own time. A port-level block write may be the right addition. If so, add it to
   `BramIFMaster` beside `write`, with the same checks.
4. **Measure again**, same scenarios and conditions, and profile once more to see what is left.
5. **(c) only if it is still worth it** after (a) and (b), and only if the output framing stays
   identical (the tests compare the captured words and responses). Otherwise, write down the
   remaining cost and stop.

Out of scope: the HLS/RTL bodies (`waveflow/build/rf_samp_buf_*_task.h`, `bram_t2p.v`), the
converter model's chunking, and the toy plan itself.

## 4. Tests

The usual loop, because (a) touches every `HwModule`:

```bash
pytest -m "not vitis and not xsi and not gem5 and not sionna"
```

Read in particular: `tests/hw/test_rf_samp_buf_twin.py` (the paced twin, drop threshold),
`tests/examples/test_rf_samp_buf_rx.py` (the four capture cases, buffer contents, every word
width), `test_rf_samp_buf_tx.py`, `test_rf_blk_delay.py`, `test_rf_loopback.py`,
`test_rf_repeat_play.py`. The XSI gate `tests/examples/test_rf_samp_buf_rx_xsi.py` checks the RTL
against the same prediction as pysim; the change does not touch the RTL, so running it with
`-m xsi` is optional. If you do, export `VIV=/tools/Xilinx/2025.1/Vivado` first (`run.sh` cannot
find Vivado on this machine otherwise).

## 5. Done when

- (a) and (b) are on a branch from `main`, with the tests above passing;
- a short table of before and after cycles/s for both scenarios (machine idle, Python 3.12, the
  date), and the new profile's top entries, is added to this plan;
- the `run_iter` docstring and comments still describe what the code does.

The user pushes by hand (this machine cannot push). An empty worktree from an earlier attempt
exists at `/home/wirelesslab914/sundeep/waveflow-rfbuf` (branch `fix/rf_samp_buf_ingress`, at
`00eafcac`). Use it, or remove it with `git worktree remove` and `git branch -d`.

## 6. As built (2026-11-21)

Branch `fix/rf_samp_buf_ingress`. (b) and (c) are done; (a) is not, for a measured reason. The HLS
and RTL bodies are untouched.

### What changed

- **(b) The ingress stores a burst as a block.** `RfSampBufIngress._store` writes it with
  `BramIFMaster.array_ref`, one numpy assignment per contiguous run of the buffer (one, or two when
  the burst wraps past the end), and moves `wr` once. No new port operation was needed:
  `array_ref` is untimed and keeps the port's direction check and the memory's range check, once
  per run. Three things a block write could get wrong are handled and tested against the word loop:
  - the address period, `_word_period`: `depth`, or `2**IDX_BW // spw` when the counter wraps
    first (`depth * spw > 2**IDX_BW`);
  - a burst longer than the buffer: only its last `period` words are written, which is what the
    loop left;
  - a value too wide for the word is refused. A block assignment into the 16-bit storage wraps it
    silently (70000 lands as 4464), where the per-word store raised.

  `write_pipelined` was not used. It charges time of its own (the ingress already does), needs a
  clock on the `BramIF`, and stores the data at the end of the delay rather than the start, which
  changes what the capture can see near the horizon: a modelling change, not a speedup.
- **(c) The capture sends bursts.** `RfSampBufCapture.run_iter` sends every word owed that is already
  written, up to `pysim_burst_words = 16`, as one burst. The wait, the horizon check (on the burst's
  oldest word, read at the moment it is checked) and the response are the per-word body's. The
  output framing changes (480 bursts instead of 7,680 in the scenario below); the words do not, and
  no consumer depends on the framing (the RTL output has no TLAST; `BlkDelay` and the TX loader take
  bursts of any length within a frame).
- **(a) not done.** Once (b) removes the per-sample assignment, a cached `__setattr__` check
  changes neither scenario: measured with a prototype, rf 2.5 M cycles/s with or without it, and
  mem_copy 1.19 M either way. The cost is real (890 ns per attribute assignment on a module
  against 52 ns on a plain object), and a body that assigns an attribute per sample still pays it.
  Worth doing when such a body matters.
- **Tests.** `tests/hw/test_rf_samp_buf_block_store.py`: `_store` against the word loop over four
  geometries (including `depth * spw > 2**IDX_BW`), seven burst lengths and three write positions;
  an empty burst; a too-wide word; and a window read across the end of the buffer, which none of
  the gate's commands do. `test_rf_blk_delay.py`: the 400 MSa/s floor pinned at 6 blocks (it fails
  with the burst unbounded).

### Why the burst is bounded

A burst reaches the next stage only when its last word has, so each burst adds up to its own length
of store-and-forward delay per hop; the RTL passes words on one at a time. `examples/rf_blk_delay`
relays the capture's output to the TX loader, and its delay floors are pysim sweeps:

| floor, blocks | one word a burst (before) | unbounded | at most 16 | at most 4 |
|---|---|---|---|---|
| 250 MSa/s | 4 | 4 | 4 | 4 |
| 400 MSa/s | 6 | **7** | 6 | 6 |

Below the floor the partial placements move in every variant (at a delay of 3 and 250 MSa/s: 244 /
0 alternating before, 216 each now). No test pins them, and no RTL measurement of them exists.
"Unchanged" means the twin agrees with its earlier self, not that either matches RTL.

### Speed

2026-11-21, this machine (Ubuntu 24.04, Python 3.12.3), idle (load under 1, no tool jobs), best of
three runs alternating `main` and the branch, each in a fresh process, the scenarios of §3 step 1
(`RfLongSystem` and `mc_system` copied from `l1_speed.py`):

| scenario | `main` | branch | |
|---|---|---|---|
| rf, the sample buffer, 1,005,257 cycles | 0.804 s, 1.25 M cycles/s | 0.280 s, **3.59 M cycles/s** | 2.9x; the same captured words and responses |
| mem_copy, 1,626 jobs, 886,179 cycles | 0.711 s, 1.25 M cycles/s | 0.726 s, 1.22 M cycles/s | unchanged (no sample buffer) |

Against L1's XSI rate for the sample buffer (278,000 cycles/s, Vivado 2025.1), pysim is now about
13x the RTL there, from 4.5x. Prototype steps on the same scenario: the ingress fix alone 2.6 M
cycles/s; with the capture unbounded 3.85 M; bounded at 16, 3.7 M; at 4, 3.3 M.

### What is left (cProfile of the branch, 0.57 s inside SimPy under the profiler)

26,534 SimPy events, 26 a block (48,134 before), about 10 µs each unprofiled, everything included.
Cumulative time per process:

| process | s | share |
|---|---|---|
| the converter model, `Rfdc._adc_proc` | 0.231 | 41% |
| ... of which `RfdcSampWord.pack`, once a block | 0.122 | 21% |
| the ingress, `RfSampBufIngress.run_iter` | 0.099 | 17% |
| ... of which `_store` | 0.027 | 5% |
| the capture, `RfSampBufCapture.run_iter` | 0.043 | 8% |
| the rest: SimPy, the interfaces, the testbench | ~0.20 | ~35% |

Nothing is per sample any more. The next candidate is the converter model's packing (out of scope
here), then the cost per event itself. Why this design's ratio stays far below the FFT's or CG's is
not pysim: its RTL is cheap per cycle (a few counters and one memory port), so there is little
internal activity for pysim to skip. See `docs/evaluation/sim_speed/index.md`'s cost model.
