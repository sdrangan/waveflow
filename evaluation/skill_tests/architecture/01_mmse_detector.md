# Skill test (architecture) 1: an MMSE MIMO detector, from the paper to a plan

I work on wireless receivers, not hardware. I want to put the uplink MIMO detector from the paper below
on our FPGA, using Waveflow. **I do not want code yet.** I want a plan I can check: what exactly gets
built, how it is split up, whether it fits on our board and runs fast enough, and what I still have to
decide. Write it in this folder. Nothing needs Vitis or Vivado: if you write any code, it must run in
Python.

## The algorithm (from the paper)

A base station with `B` receive antennas serves `U` single-antenna users on the same time-frequency
resources. On each resource element (one subcarrier of one OFDM symbol) the received vector is

```text
y = H x + n,      y in C^B,  H in C^{B x U},  x in C^U (QAM symbols),  n ~ CN(0, sigma^2 I)
```

The paper's detector is linear MMSE:

```text
x_hat = (H^H H + sigma^2 I)^{-1} H^H y
```

The paper evaluates it in double-precision floating point and inverts the `U x U` matrix directly. It
assumes `H` and `sigma^2` are known; in our receiver they come from the channel estimator, which
another team builds and which runs on the reference signals.

## The application

- A 5G NR cell, one 100 MHz carrier in FR1.
- 32 antenna ports at the base station. Up to 8 users are scheduled together on the same resources,
  but most of the time it is 2 to 4.
- The frequency-domain samples (after the FFT) and the channel estimates arrive at the detector from
  the rest of the receiver; the detector's outputs go to the demapper. Those blocks are not ours.
- Our board is an RFSoC 4x2.

I don't know what else you need from me. Ask, or tell me what you assumed.
