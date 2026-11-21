# A 5G NR PRACH searcher, in Waveflow

Short preambles (L_RA = 139, 30 kHz), a 20 MHz carrier at 30.72 Msps, 64 preambles, up to 4 antennas
and 12 repetitions (B4); a free-running stream pipeline for the RFSoC 4x2 at 250 MHz.

| where | what |
|---|---|
| `prach.md` | the task |
| `decisions.md` | the 20 decisions taken without you, each with its options |
| `plans/spec.md`, `arch.md`, `eval.md`, `blocks/` | the plan: what, how (3 alternatives), does it fit, each block |
| `design.md` | the `stream_pipeline` frame's checklist |
| `prach/nr.py` | the NR side: preambles, configuration, a UE / channel model |
| `prach/golden.py` | the executable definition: bit-exact and float |
| `prach/schemas.py` | every message, declared once |
| `prach/prach.py` | the sample lanes, the four stages, the composite (the library's `SsrFft` twice) |
| `prach/thresholds.py` | the host's `thr_q8(n_ant, n_cs)` table (Pfa 1e-3; decision 18) |
| `prach/scenario.py` | the scenarios, the stimulus, every expected message |
| `src/*.h` | the four hand-written HLS bodies (**not compiled here**: no Vitis) |
| `prach_build.py` | the build DAG: codegen, then per system scenario / pysim / csynth / XSI / compare |
| `tests/test_prach.py` | the gates that run without Vitis |
| `results/` | the report, test log, detection-performance runs |

```bash
python -m pytest tests -q                       # golden, each stage alone, the composite, codegen
python prach_build.py --through codegen         # headers and tops into include/ and gen/
python prach_build.py --through pysim           # the composite on the build DAG
python prach_build.py                           # through RTL compare: needs Vitis HLS + Vivado 2025.1
python scratch/perf.py; python scratch/perf_fixed.py   # thresholds and Pd (minutes)
python scratch/robust.py                        # CFO, multipath, timing, near-far (decision 16)
python scratch/margin_rel.py                    # the detector variants behind decision 17
python scratch/thr_table.py; python scratch/thr_check.py   # the threshold table and its bit-exact check
```

`include/` and `gen/` are generated: never edit them, rerun codegen.
