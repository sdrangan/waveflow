# Design: two devices, a memory and a host on one bus

## Structure

```text
host (SwHost) --m--+                                   +-- scale: q_cmd, q_status   (ScaleDev's views)
scale.m_rd_mem ----+--[ AXIMMCrossBarIF  xbar ]--------+-- chk:   q_chk, q_sum      (Checksum's views)
scale.m_wr_mem ----+                                   +-- mem:   MemoryMod (DDR)
```

- `system.py`: `make_system(kind)` returns an `HwSystem(sim, dut)`. The DUT (`pair_dut`) is the two
  devices (each from its class's `mm_views` by `build_mm_device`), the memory and the crossbar. The
  host is the testbench, derived. Four `IrqIF` lines, one per queue the host waits on; nothing polls.
- `build.py`: one DAG with three systems: each device alone in its harness (`scale_`, `chk_`) and the
  pair (`pair_`), each through `add_system_steps`, run by `run_dag_cli` (default `--through
  pair_pysim`).
- `test_system.py`: each device alone against its own golden, the pair for `test` and `error` against
  `golden()`, and the DAG through every step that needs no toolchain. Every test counts first.

## Address map (bytes)

| slave | base | size |
|---|---|---|
| ScaleDev's views | `0x0000` | from `device.ranges` |
| Checksum's views | `0x4000` | from `device.ranges` |
| memory | `0x10_0000` | 8192 words of 64 bits |

Input buffers from the start of the memory, output buffers from its middle, each a whole number of
words: ScaleDev writes the last output word whole, so its lanes past `n` become zero, and the host reads
back whole words and keeps `n` samples.

## Choices (the spec leaves them open)

- **One job at a time.** The host writes the input, queues the command, waits for the status, reads the
  output, sends it to Checksum, waits for the checksum, then starts the next. The status and result
  queues therefore hold at most one message, and Checksum's input queue (1024 words) holds the longest
  job (2048 samples = 512 words, plus its command). Overlap (a producer and a consumer thread, as
  markov's host) would be faster; the spec asks for correctness.
- **The scenario** (`jobs_for`): `test` is four jobs of 70, 2048, 3 and 512 samples (one longer than
  1024, two not a multiple of four, a gain that saturates); `error` puts a job claiming `n = 2055` second,
  with a job after it. After the error the host stops: ScaleDev answers nothing more, so there is no
  checksum for it.
- **The golden** is `scale` and `checksum` (the given pure functions) on the inputs: per job its status
  code, saturation count, samples and checksum; nothing after an error.

## Build steps

| step | run here | why not |
|---|---|---|
| `*_scenario`, `*_pysim` | yes | |
| `scale_golden`, `chk_golden` | yes | (the pair states no `golden(kind)`: its check is the test's) |
| `*_csynth` | no | Vitis HLS 2025.1, and each kernel's HLS body behind `kernel_task()` (`ScaleCore`, `Checksum`), not written |
| `*_system_rtl` | no | Vivado 2025.1 (the crossbar IP) |
| `*_system_xsi`, `*_compare` | no | Vivado's xsim, and the host's C++ twin (a class on the generated `Host_endpoints`), not written; `scenario_bursts()` gives what it would replay |

## What the tests catch

`check_variants.py` runs the tests against two broken scalers: one that stops after its second job
(caught by the scaler's harness test: two statuses of three), and one that writes one word fewer per
output (its statuses are right, so the harness test passes; caught by the pair test, whose host reads a
stale last word back).
