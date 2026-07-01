# FIR full-duplex investigation — the isolation ladder

Why does the free-running FIR's steady period ≈ **bus occupancy** (`read+write` ADD, ~704 cyc/job @
4×64) instead of `max(read,write)` (~306)? i.e. it does *not* exploit the full-duplex `gmem` bundle
even multi-job. Each rung below is a standalone Vitis cosim that removes one variable. (`N`=256/job.)

| rung | dir | what it isolates | result |
|---|---|---|---|
| — | `duplex_toy/` | is one `gmem` bundle full- or half-duplex? | **FULL-duplex** (read+write ≈ max), for 1 process AND 2 DATAFLOW processes |
| 1 | `compute_iso/` | the FIR compute alone (X from BRAM, no m_axi) | **II=1.000**, per-row L1≈0, L0=56 — compute is clean |
| 2 | `loadstore_iso/` | load ‖ store, **no compute** | period ≈ **max** — overlaps, full-duplex USED |
| 3 | `lcs_iso/` | load → *pass-through* → store, sweep FIFO depth | **overlaps at every depth** (256…2048) — skeleton/depth ruled out |
| 4 | `fir_skel/` | the **real** shift-register FIR compute in the skeleton | **direct gmem↔FIFO = 306 (OVERLAP)**; **2-pass buffer (`-DFS_BUF`) = 534 (SERIALIZED)** |
| — | `halfpipe/` | lc (load+compute) vs cs (compute+store) — read vs write side | built; moot once the pass-through skeleton already overlapped |

## Conclusion

The slowdown is **not** the bus, compute, FIFO depth, or per-job structure. It is the **transfer
pattern**: the hook (`fir_pipeline_impl.tpp`) routes X/Y through `read_array_slice`/`write_array_slice`,
which stage through an intermediate buffer (`gmem → cb → FIFO`, 2 passes). Per
`docs/guide/vectorization/hls/raw.md` that is the *resident* path; the canonical **throughput** path is
**"The lane loop"** — stream `gmem ↔ FIFO` directly (one II=1 pass, no buffer). Rung 4 shows the buffer
alone flips full-duplex overlap (306) → serialized (534; the real kernel's 704 adds the `h`-read +
`FIRCmd` deserialize on top).

**Fix:** rewrite the hook load/store as the lane loop (done for LW==1; `static_assert` guards LW>1,
which needs an `s[LW][T]` window + `LW·T` MAC array for `LW` samples/cycle). See the memory note
`project-fir-slice-vs-laneloop-rootcause` and `plans/fir_freerun_integration.md`.

## Running a rung

```bash
cd <rung_dir>
PYTHONPATH=../../../.. ../../../../pysilicon-venv/Scripts/python.exe run_<name>.py   # see each runner's docstring
```
Each needs Vitis HLS 2025.1 + Vivado xsim. The `*_proj/`, `vcd/`, and `*.txt`/`*.log` outputs are scratch.
