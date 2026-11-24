# Skill test (systems) 1: two devices, a memory and a host on one bus -- the system, its DAG, its tests

Put a hardware system together with Waveflow, in Python, and verify it in pysim. **Nothing is
synthesized or simulated in Vitis**, and no HLS C++ or host C++ is asked for. Everything you write must
run in Python without Vitis, and should be run.

Two devices are given, finished and tested, in this folder:

- `scale_msgs.py`, `scale_dev.py`: **ScaleDev**, a gain-and-shift offload. The host queues a
  `ScaleCmd` (job id, `n`, source and destination word addresses, gain, shift) on its command queue
  `q_cmd`; the device reads the input buffer from memory, writes the output buffer, and only then puts
  a `ScaleStatus` on its status queue `q_status`. A job with `n` over 2048 is a fatal error: its status
  says so and the device takes nothing more. Its module docstrings say the rest.
- `checksum.py`: **Checksum**. The host queues a `ChkCmd(tx_id, n)` and then the `n` samples on
  `q_chk`; the device answers a `ChkResp(tx_id, n, sum)` on `q_sum`.
- `devices.py`: **import both devices from here**, never from their own modules. Read its docstring:
  your tests will be run again against a broken version of one device, and must fail.

Each device's queues raise an interrupt, and each device states its own `workload(kind)` and
`golden(kind)` for its harness. Do not change the given files.

## What the system does

An ARM host, the two devices and one DDR memory share one 64-bit AXI bus. For each job the host puts
its input buffer in memory, has ScaleDev scale it into an output buffer, reads the output back, sends
it to Checksum and receives its checksum. The host waits on interrupts; it never polls. The host
wants, per job: the status, the scaled samples and their checksum.

## What to deliver

1. The system in Python: the host, and the whole put together so that the same Python can later be
   simulated at RTL. Choose the address map.
2. A build script that puts **each device alone and the whole system** on one build DAG, runnable from
   the command line, and that runs every step it can without Vitis or Vivado.
3. Tests (pytest) in pysim that would catch a device that stops answering partway through, or one that
   writes a wrong or missing word: each device alone, the whole system on a scenario of at least four
   jobs (one longer than 1024 samples, one whose length is not a multiple of four), an error case, and
   the build steps you can run.
4. A short `DESIGN.md`: the structure, the address map, which build steps you ran and which you could
   not, and why, and every choice you made where this description left one open.
