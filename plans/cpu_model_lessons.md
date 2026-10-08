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
