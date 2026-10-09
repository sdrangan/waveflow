# Lessons learned: the general-purpose processor model (`plans/cpu_model.md`)

Appended as the work teaches something. Newest last.

## M0: environment and ground-truth tools

- **Check the physical board's part number, not the repo's.** Every `xczu48dr` platform in the repo
  says `-2` (`xczu48dr-ffvg1517-2-e`), but the RFSoC 4x2 carries `XCZU48DR-1FFVG1517E`. For the A53
  that is 1200 MHz, not 1333 (DS926 `F_APUMAX`). The FPGA synthesis target and the silicon on the
  desk are different facts; a processor model must take the second.
- **gem5's HPI caches are the A53's caches.** `HPI_ICache` / `HPI_DCache` / `HPI_L2` (32 KiB 2-way,
  32 KiB 4-way, 1 MiB 16-way) match UG1085 exactly, so the reference configuration needs no cache
  override. Only `starter_se.py`'s clock (default 4 GHz) and DRAM (default DDR3, 2 channels) must be set.
- **`scons` ignores `CROSS_COMPILE` from the environment.** `util/m5` takes it as a build variable:
  `scons arm64.CROSS_COMPILE=<prefix> build/arm64/out/libm5.a`. The Vitis 2024.1 cross compiler runs
  inside the gem5 dependency image when `/tools/Xilinx` is mounted read-only.
- **McPAT at `74d4759f` builds 64-bit with `make CXX=g++ CC=gcc`.** Its `mcpat.mk` hardcodes `-m32`;
  overriding both variables on the command line is enough, with no source change.
- **A long background test run can die with the session.** The first baseline stopped at 18 % when the
  session restarted, with no summary. Write test output to a file and check it before trusting it.
  With `-q -rfE` this suite printed no "N passed" line; count the progress characters instead.
- **An empty m5-marked region costs 94 cycles on HPI at 1.2 GHz.** Larger than many tiny kernels'
  own cost, so subtracting it (step 9) is not optional for the scheduler operations.
- **Budget the gem5 build for a shared machine.** `build/ARM/gem5.opt` took 88 min at `-j6` with the
  machine's load around 16; it is a one-off, but don't plan a step around the 30–60 min estimate.

## M1: the processor model

- **An unconfigured `DirectMMIF` charges no time.** It adds only `latency_write` / `latency_read`
  (default 0); per-word time is the slave's to model. A test of "bus time is charged once" first saw
  zero bus time and so proved nothing. Configure the link and the slave before measuring the bus.
- **The heap ready queue holds its rate under a burst.** 37k tasks/s with 20,000 tasks queued at
  once, against 218/s measured for `simpy.PriorityResource` in planning. Streaming arrivals give 40k/s.
  Both were on a machine at load ~9 of 8 threads.
- **Hand-computed timelines at 1 Hz caught nothing, and were still worth it.** With one cycle per
  second every expected time is an integer, so the preemption rule (floor, partial switch lost,
  original `(prio, seq)` kept) is pinned exactly rather than approximately.

## M2: calibration harness

- **GCC renames the functions it specializes.** At `-O2` the aarch64 build turned `tg_remove` into
  `tg_remove.isra.0` (IPA-SRA). Code bytes counted by exact symbol name miss it; count every symbol
  whose name before the first dot is the function's.
- **Two int16 products can overflow int32 when summed.** `(-32768)^2 * 2 = 2^31`. The Q15 kernel's
  first draft did exactly that, which is undefined behaviour in C and would have broken the twin on a
  rare input. Widen to int64 before adding.
- **A test that a gate fails is part of the gate.** Pointing `WAVEFLOW_GEM5_ROOT` at nothing must turn
  `-m gem5` red (it does: 9 of 9 skipped, exit 1); otherwise "9 passed" means nothing.
- **Fit the criterion you will be judged by.** Ordinary least squares on a corpus spanning five decades
  of cycles fits the largest points and fails the small ones; the acceptance metric was relative error,
  and weighting each point by 1/measured^2 fixed every validation miss without changing a model's form.
- **A validation pass at a few sizes does not cover the sizes between them.** Validation at
  n = 3/12/48 passed; test at n = 6/24 and dispatch n = 32 missed the max bound by up to 39 %. Small
  operations carry microarchitectural effects (branch-predictor training, pipeline fill) that counters
  from a Python twin cannot see, and two seeds per size is thin. Register denser held-out sizes where a
  family's cost is small.
- **Append to a module above its `__main__` guard, never below.** The area stages were appended after
  `if __name__ == "__main__": raise SystemExit(main())`, so the CLI ran `main()` before they existed
  (`NameError`); imports and tests were unaffected, which is why it surfaced only on the command line.
- **McPAT cannot size a TLB for a virtual address wider than 32 bits** (at `74d4759f`): every width
  above 32 fails with "no valid data array organizations found". Bisecting the description from the
  shipped template, one change at a time, found it in five runs.
