---
title: Functional verification
parent: Flow Overview
nav_order: 6
audience: python
api: [harness, StreamSink, add_system_steps, add_robustness_steps, StandInPolicy, load_run]
summary: "How to verify a kernel or a system, in pysim and at RTL -- the fourth step of the flow: what is compared with what, and what a passing gate proves. Three links -- the golden reproduces the spec's worked examples; pysim against the golden, burst for burst, from the harness's sinks; RTL against pysim, word for word (compare), so the RTL is the golden's too. The error scenarios (a bad command mid-scenario, nothing answered after it). Every gate asserts counts, because a deadlock looks like success. With a host: its bus traces identical at RTL and in pysim, the host finished, no polls. Hostile stand-ins for robustness, an exact cycle count as a regression gate, and what none of this proves."
---

# Functional verification

Verifying a kernel or a system -- in pysim, then at RTL -- is a chain of comparisons, each against something already trusted. No link is "it ran".

```text
spec's worked examples  ->  golden  ->  pysim  ->  RTL
                          (pure functions) (the model) (the hardware)
```

## The golden against the spec

The golden is pure functions ([The DUT](dut.md#what-it-does-the-golden-first)), so its test is
the spec's own worked examples, reproduced exactly. Everything after it is checked against the golden's
expected outputs, computed **from the intents** -- so a bug in the stimulus cannot hide by being repeated
in the expectation.

## pysim against the golden

In the module's harness, each `StreamSink` keeps what it recorded as `words`, one array per burst. The
check compares bursts, so it checks the framing as well as the values:

```python
sysm = harness(PolyKernel, workload="test")
sysm.run()                                                     # pysim
got = {p: [np.asarray(b, dtype=np.uint64) for b in sysm.stand_ins[p].words]
       for p in ("m_out", "m_status")}
want_out, want_status = expected(default_cmds())               # from the intents
assert len(got["m_out"]) == len(want_out) and len(got["m_status"]) == len(want_status)
for g, w in zip(got["m_out"] + got["m_status"], want_out + want_status):
    np.testing.assert_array_equal(g, w)                        # bit for bit, burst for burst
```

A header and its results are two bursts; a short results burst is short; after a fatal error there are
none. To read a burst as a message, deserialize it with its schema -- `Status().deserialize(words,
word_bw=W)` -- never by hand.

## RTL against pysim

The [system flow](rtl.md#the-system-flow)'s **`compare`** step checks what the testbench recorded at RTL
against what it recorded in pysim, from the same scenario file: each sink's words, each memory model's
final contents, a host's traces. Equal, word for word, means the RTL's outputs are pysim's -- and so the
golden's. A recording pysim cannot match by construction is marked `recording_check = "example"` on its
participant and checked by the example instead.

## The error scenarios

A design's behavior on bad input is part of its function. Put the bad command **in the middle** of a
scenario and check what follows it: the polynomial's fourth command ends early, so its results are
short, its status is `TLAST_EARLY`, and the fifth command is **never answered on either stream**
([Command-response](command_response.md#errors-are-fatal)). The same comparisons check it -- the golden
says what an error produces, and what it silences.

## Every gate asserts counts

**A deadlock looks like success**: every process simply stops, and a check that only looks at what
arrived passes on nothing. So every gate counts -- bursts recorded, statuses written, jobs answered --
against what the golden says there must be. The polynomial's test asserts the number of bursts on each
stream before it compares one.

## With a host

A system with a software host is checked through the host:

- **its traces**: every endpoint the host uses records what crossed it, and `compare` requires the RTL's
  and pysim's to be identical, byte for byte;
- **it finished**: the run reports the host done, with every job answered;
- **it never polled**: a host waits on interrupts, and the run reports zero polls.

The example then checks the host's results against the golden ([markov's XSI testbench](../../examples/markov/xsi.md)).

## Robustness: hostile stand-ins

A polite testbench never shows that a module survives anything else. A
[`StandInPolicy`](../flows/units.md#hostile-stand-ins) makes each stand-in misbehave as its protocol
allows -- a sink that drops TREADY, a driver that holds off its next word, credit held back -- and
`add_robustness_steps` runs the harness again under each. The check is the same `compare`: the RTL must
still produce exactly pysim's words.

## An exact cycle count

The RTL run's cycle count is a gate of its own: `mem_copy` 2908 cycles, markov 1870 (Vivado 2025.1). It
is exact, not a bound -- a count that moves is a real change in behavior, a regression or an improvement,
and both deserve a look. Whether pysim's timing *predicts* it is the next page.

## What this does not prove

- **That pysim's timing is right.** `compare` checks words, not times; the cycle count is checked
  against the RTL's own previous run. Timing is [calibrated](calibration.md) separately.
- **That the RTL meets its clock.** csynth's timing estimate is a separate number, and a design that
  simulates correctly can still miss it.
- **That it works on a board.** The RTL simulation is the synthesized design with models around it;
  [Toward the board](../flows/bitstream_ipi.md) is the next step.

Next: [Timing and resources](calibration.md).
