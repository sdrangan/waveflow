# System spec: `level_detect`, one kernel reached over the bus

Frame: `bus_system` -- read [frame.md](../frame.md) first; this file adds only
what is specific to `level_detect`.

## 1. Overview

A level detector on the crossbar.  The host streams packets of unsigned
12-bit samples (carried in uint16) to it over the bus; for each packet the
kernel returns one flag per sample -- 1 where the sample is at or above the
threshold -- and a response with the count of flags set.  The threshold and
a hysteresis are a **configuration** the host changes mid-stream.

## 2. Configuration

A configuration is `cfg_id` (uint16), `threshold` (uint16, 0..4095) and
`hyst` (uint16, 0..255).  The host writes configurations through a register
bank on the kernel; every packet header names the `cfg_id` it must be
processed with, and the kernel does not start a packet until that
configuration has been committed.  Configuration never travels in timing:
a packet sent "after" a write but naming an older `cfg_id` uses the older
one.

## 3. Messages

- **Packet header** (host -> kernel, through its input queue): `tx_id`,
  `nsamp` (1..512), `cfg_id`; then `nsamp` samples.
- **Flags** (kernel -> host, through its output queue): `nsamp` flags, uint8
  each, packed four per 32-bit lane by the Waveflow array utilities.
- **Response** (kernel -> host, through its response queue): `tx_id`,
  `nsamp`, `cfg_id` echoed, `n_set` (uint16).
- **Status** (read by the host from the register bank): the `cfg_id` in use
  and the number of packets processed.

## 4. The function

Per packet, with state starting at 0: a sample `x` sets the state to 1 when
`x >= threshold`, and clears it to 0 when `x < threshold - hyst` (saturating
at 0); otherwise the state holds.  The flag is the state after the sample.
State does not carry from one packet to the next.

## 5. The host

A writer thread and a reader thread; it waits on the queues' interrupts and
never polls.  The scenario changes the configuration twice, the second time
while packets that name the first are still queued.

## 6. Acceptance

- Golden model with worked examples, including a hysteresis edge.
- pysim bit-exact; a negative control: a packet naming a `cfg_id` that is not
  the latest is processed with the one it names.
- The system DAG at RTL: traces identical to pysim, zero polls, cycles
  recorded and pysim within 5%.  `results/report.md` as in the frame.
