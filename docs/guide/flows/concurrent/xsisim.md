---
title: XSI sim
parent: Concurrent (free-running)
grand_parent: Realization Flows
nav_order: 7
audience: python
summary: "Running the XSI simulation: elaborate the RTL into xsimk.dll, drive it cycle-by-cycle from the generated harness, and assert the golden plus an exact cycle count. Why XSI and not Vitis co-sim (the free-running kernel has no start/done handshake), and where the pipelining shows up in the numbers."
---

# The XSI simulation

<!-- WRITE ME. Running the sim and reading the result.
     - Why XSI, not cosim: ap_ctrl_none has no start/done handshake, so Vitis C/RTL cosim refuses it;
       XSI drives the elaborated RTL directly. (Contrast the sequential flow's cosim.)
     - The build: xvlog/xelab (-dll) -> xsim.dir/<top>/xsimk.dll; the harness drives it via XSI.
     - Running: pytest -m xsi. Each gate regenerates rtl_<top>.f + deletes xsim.dir first (a stale
       .f + cached dll can go green while proving nothing).
     - The gates are EXACT cycle counts: mem_r_stream 158, mem_w_stream 176, mem_copy 2835,
       interleaver_canon 3469 — a count that moves is a real behaviour change.
     - Where pipelining shows: mem_copy is ~177 cyc/job across 16 jobs vs ~176 for one write alone —
       the reads hide behind the writes (max(read, write), not read + write). -->

**Run:** `pytest -m xsi`. Needs Vivado xsim + mingw g++, and a prior csynth of each top.

**Source of truth:** `tests/examples/test_xsi_bfm.py` (the gates + exact counts),
`waveflow/build/xsi/xsi_bfm.h` (`XsiSim` — the clock/port driver), [Build / XSI](../../build/xsi.md)
(xvlog/xelab/loader). The pipelining numbers are explained on the
[memory-wrapper page](../../memory/memstream.md).
