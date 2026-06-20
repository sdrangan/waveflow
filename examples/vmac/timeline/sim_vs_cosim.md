# VMAC ab_eq: sim prediction vs Vitis RTL cosim (the headline validation)

Stage 3 of `plans/vmac_mm_queue_timing.md`. PF=1, n_rows=n_cols=4, clk 100 MHz (`create_clock -period 10`).

## (a) m_axi read bursts in RTL — does ab_eq issue half the reads?

| scenario | ab_eq | read bursts | read words | write words |
|---|---|---|---|---|
| anorm  | True | 16 | 16 | 4 |
| abcorr | False | 32 | 32 | 4 |

=> anorm read words are HALF of abcorr (16 vs 32): the B read-bus traffic **is** suppressed by ab_eq.

## (b) kernel latency in RTL — did eliding B's read lower latency, or is it fixed-II?

| scenario | RTL transaction cycles | RTL latency (ns, first burst -> last write) |
|---|---|---|
| anorm  | 347 | 3200.0 |
| abcorr | 347 | 3200.0 |

=> **FIXED-II: same RTL latency, only the read bus freed.**

## (c) sim-predicted vs RTL-measured anorm latency (the loosely-timed model error)

- sim predicted: anorm latency = 6.3e-07 s, abcorr latency = 9.4e-07 s  -> sim gap (abcorr - anorm) = 3.1e-07 s (anorm predicted FASTER).
- RTL measured:  anorm cycles = 347, abcorr cycles = 347  -> RTL gap = 0 cycles.

The loosely-timed sim is **transaction-gated**: it makes anorm finish sooner because it issues one fewer read block. The fixed-II RTL shows the SAME latency for both (the gap the sim predicts is the model error a cosim-calibrated II would close); only the read bus is freed.

## Scope
Queue occupancy is a sim-only quantity (the kernel has an m_axi but no command ring); it is out of cosim scope and not fabricated here.
