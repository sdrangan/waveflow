# Skill test (devices) 1: an offload the host reaches over the bus, working on buffers in memory

Write a hardware device with Waveflow, in Python, simulated in pysim. **Nothing is synthesized or
simulated in Vitis**, and no HLS C++ is asked for. Everything you write must run in Python without
Vitis, and should be run.

The device is described the way its user described it. Turning that into kernels, ports, adaptors and
protocols is your job. Where the description leaves a choice open, make it, and say what you chose.

## What it does

Our ARM host keeps signed 16-bit sample buffers in DDR and wants to offload a gain-and-shift on them.
For each job the host gives the device:

- a job id (16 bits),
- where the input buffer is and where the output buffer goes (two DDR addresses),
- how many samples, `n` (16 bits; 1 to 2048 is allowed),
- a signed 16-bit gain and a shift (0..15).

The device reads the `n` input samples, computes

```text
y[i] = saturate_int16((x[i] * gain) >> shift)       (the product exact, the shift arithmetic)
```

and writes the `n` outputs to the output buffer. The memory and the bus are 64 bits wide.

## What the host needs

- It sends jobs over the AXI bus and may send several before the first is finished.
- For each job it wants a **status** it can read from a register: the job id, whether the job went
  well, and how many samples saturated.
- It wants an **interrupt when a job is done**, meaning the output is in DDR and it can read it. It
  must not poll.
- A job with `n` larger than 2048 is an error. After an error the device need not take any more jobs
  until it is reset, but the host must still learn about the error.

## What to deliver

1. The device's Python: its messages, its kernel(s), and whatever joins them to memory and to the bus.
2. Tests (pytest) that run it in pysim: the device alone on a scenario of at least three jobs (with a
   length that is not a multiple of four), an error case, and the device on a bus with a host and a
   memory, the host checking every output it reads back.
3. A short `DESIGN.md`: the structure, and every choice you made where this description left one open.
