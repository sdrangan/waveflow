# Witness: a two-port BRAM beside a Vitis kernel

The measured basis for `plans/rtl_module.md`.  Hand-written, no Waveflow involvement — the point is
that it ran before any infrastructure was designed against it.

```
rx_kernel.cpp   two SIZED bram array params, one direction each, latency=1, two free-running tasks
bram_t2p.v      hand-written true-dual-port memory, 1-cycle read, $error on read-during-write
rx_top.v        the wrapper: kernel + memory, exposing only AXIS.  THE DESIGN SCOPE.
tb.v            the ramp test: write buf[i]=i+100, read back by address, check VALUES
run_hls.tcl     csynth only
```

Reproduce (Vitis HLS 2025.1, `xczu48dr-ffvg1517-2-e`):

```
vitis-run --mode hls --tcl run_hls.tcl
xvlog rx_top.v bram_t2p.v tb.v proj/sol1/syn/verilog/rx.v \
      proj/sol1/syn/verilog/rx_read_task.v proj/sol1/syn/verilog/rx_write_task.v \
      proj/sol1/syn/verilog/rx_regslice_both.v
xelab tb -s tbsim --debug off && xsim tbsim -runall
```

Expected: `T2P-BRAM EXPERIMENT: PASS (5 words, ramp verified)`

**A ramp, not a constant, on purpose.** The likeliest failure is a read-latency mismatch between the
pragma and the memory, which shifts the data by one and would pass a constant-value check.

The two structures that do NOT work, and why, are in the plan's motivation section — a shared local
array silently becomes a synchronizing PIPO channel, and a single port used both ways is refused.

**2026-10-11 -- the shipped memory no longer equals this file.**  `waveflow/build/rtl/bram_t2p.v`
writes each port in its own `always` block (Vivado's true-dual-port template): this file's single
block built a 512 x 64 instance from 32832 flip-flops and no block RAM
(`plans/system_resources.md`).  Ports, statements, read-first and latency 1 are unchanged; this copy
is kept as it ran.  `tests/build/test_rtl_module.py` gates what still must match.
