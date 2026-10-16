---
title: Calibration
parent: Processor Model
nav_order: 2
audience: python
api: [CpuPlatform, Gem5Runner, SweepPlan, RelLinCalibModel, Mcpat]
summary: "How the shipped Cortex-A53 platform was calibrated and how accurate it is. Ground truth is gem5 v25.1's HPI in-order core in syscall-emulation mode, configured as the RFSoC 4x2's A53 (1.2 GHz, 32 KB L1s, 1 MB L2, DDR4-2400); energy and area come from McPAT at 22 nm. Six C kernels with bit-exact Python twins, 305 pre-registered points split fit / validation / test, models fitted by relative-error least squares, and the one-time test result per family -- including the documented misses on small operations and on back-to-back sequences."
---

# Calibration

The shipped platform, `waveflow/calib/platforms/a53_hpi_1200mhz_gem5v25_1/`, prices software on the
Cortex-A53 of the RFSoC 4x2. Everything below was measured with **gem5 v25.1.0.1**, **Vitis 2024.1's
aarch64 gcc 12.2.0** (`-O2 -static`) and **McPAT** at commit `74d4759f`; a number from other tools
belongs to them, not to this platform.

## Ground truth

- **The core.** gem5's HPI, an in-order Armv8-A model of the A53 class, in syscall-emulation mode
  (`starter_se.py --cpu hpi`). Its caches equal the A53's in UG1085: 32 KB L1I (2-way), 32 KB L1D
  (4-way), 1 MB shared L2 (16-way). The clock is the -1 speed grade's 1.2 GHz (DS926 `F_APUMAX`) — the
  board carries `XCZU48DR-1FFVG1517E`. DRAM is `DDR4_2400_8x8`, one channel.
- **What is not in it.** HPI is a model of the A53 class, not the A53: accuracy here means agreement
  with gem5. Syscall emulation has no operating system, so the model describes bare-metal or
  RTOS-style software.
- **Energy and area.** McPAT, from each measured region's activity, with an in-order A53 description
  adapted from McPAT's ARM template: 22 nm (McPAT's smallest node, not the A53's 16 nm), the core and
  L2 only (no DRAM energy), and a 32-bit virtual address (McPAT cannot size a TLB for the A53's 48).

## The kernels

Six small C programs, each with a Python twin that computes the same outputs **and** the same work
counters, bit for bit — checked at every one of the 305 points, against both the host build and the
binary gem5 ran:

| Kernel | What it exercises | Counters |
|---|---|---|
| `sched_ops` | a scheduler's ready-list operations: add, delete, reprio, sort | `n_tasks`, `n_scanned`, `n_moved` |
| `cdot_q15` | streaming Q15 arithmetic, crossing L1 and L2 | `n` (+ regime features) |
| `gather_hist` | random access over a working set from 256 B to 16 MB | `n`, `m` (+ regime features) |
| `dispatch` | dequeue and call through a function pointer | `n_dispatch` |
| `ctx_switch` | a hand-written cooperative AAPCS64 context switch, no syscall | `n_switches` |
| `swapcontext` | libc's switch, for information (gem5 emulates its syscall at near-zero cost) | `n_switches` |

Each program draws its inputs, makes one warm-up call, then measures one call between m5 markers. The
markers' own cost in that position — 94 cycles — is measured once and subtracted.

## Pre-registration

The 305 points and their roles were committed before any of them was measured, and the runner refuses
a point that is not registered:

- **fit** points span each family's range, corners included;
- **validation** points are interior, and are what model-structure decisions may look at;
- **test** points are interior, on different seeds, and were evaluated **once**.

The first fit, by ordinary least squares, missed validation on four cycle models: the largest points
(up to 15M cycles) set every fit, and the small ones paid. Fitting by **relative** error — what the
acceptance bound measures — fixed all four without changing any model's form.

## Accuracy

Relative error of the fitted models on the test points, evaluated once. The bound is a median of at
most 10 % and a maximum of at most 25 %.

| Family | Cycles median | Cycles max | Energy median | Energy max |
|---|---|---|---|---|
| `sched_ops.add` | 4.7 % | **39.3 %** | 1.6 % | 1.9 % |
| `sched_ops.delete` | 3.3 % | **29.0 %** | 2.5 % | 9.1 % |
| `sched_ops.reprio` | 2.1 % | 16.6 % | 2.5 % | 8.4 % |
| `sched_ops.sort` | 5.8 % | 18.1 % | 0.4 % | 2.4 % |
| `cdot_q15` | 1.9 % | 21.8 % | 0.1 % | 1.8 % |
| `gather_hist` | 7.6 % | 12.3 % | 1.3 % | 2.4 % |
| `dispatch` | 1.1 % | **38.1 %** | 0.5 % | 3.3 % |
| `ctx_switch` | 0.8 % | 12.5 % | 0.2 % | 0.3 % |

Area and leakage, over a grid of 96 configurations (1–4 cores, 16–64 KB L1, 256 KB–2 MB L2): area
median 4.4 %, max 6.3 %; leakage median 0.3 %, max 0.9 %.

### The two documented misses

- **Small operations.** The three bold maxima are the smallest test points — an `add` on a list of
  six, a `dispatch` of 32 calls — where branch-predictor and pipeline state that no counter sees
  moves the cost by tens of cycles. Their medians are within bound.
- **Back-to-back sequences.** Each calibration point is one call after one warm-up; a scheduler runs
  operations back to back. On the [example](../../examples/cpu_sched/index.md)'s 410 operations and
  200 dispatches, gem5 measured 63,266 cycles in one region and the model predicted 75,656: **19.6 %**
  high, a roughly constant 20–40 cycles per call. Timing each operation in its own region does not
  settle it either: the markers perturb a region by tens of cycles, the size of the operations
  themselves (94 cycles when their code is cold, 8 when it is hot).

Both were accepted as recorded; calibrating small operations as batches of back-to-back calls is the
known way to measure the steady state.

## Recalibrating

```bash
python -m waveflow.cpu.calib.sweep --platform-dir <platform>      # write the plan; commit it
python -m waveflow.cpu.calib.campaign --platform-dir <platform>   # gem5, resumable
python -m waveflow.cpu.calib.calibrate energy --platform-dir <platform>
python -m waveflow.cpu.calib.calibrate fit --platform-dir <platform>    # + validation report
python -m waveflow.cpu.calib.calibrate test --platform-dir <platform> --commit <sha>   # once
python -m waveflow.cpu.calib.calibrate area --platform-dir <platform>   # then area-fit, area-test
```

gem5 runs in its dependency image (`ghcr.io/gem5/ubuntu-24.04_all-dependencies:v25-1`) from a build
at `WAVEFLOW_GEM5_ROOT`; McPAT runs natively from `WAVEFLOW_MCPAT_ROOT`. `pytest -m gem5` runs the
ground-truth gates, and fails the session if any of them skips.
