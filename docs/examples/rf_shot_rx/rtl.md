---
title: Taking it to RTL
parent: Capturing without losing anything
grand_parent: Examples
nav_order: 2
audience: hls
summary: "The XSI run for rf_shot_rx: nine gates, and every measured number with the assertion that pins it. The ramp arriving contiguous, the windows alternating between the two halves, every header carrying CAP_OK and zero lost, the converter never having a word refused, and the memory scan that finds both ports live together and never in the same region."
---

# Taking it to RTL

`tests/examples/test_rf_shot_rx_xsi.py` — **9 gates**, run with `pytest -m xsi`. What xsim elaborates
is the wrapper `rf_shot_rx_top`. Needs a prior `csynth` plus Vivado `xsim` and a C++ compiler; the
gates **skip loudly** rather than passing when either is missing, and `-m xsi` fails the session if
any gate skips.

## Every number, and the gate that asserts it

Recorded at `depth=256` split into two regions, `blk_words=16`, 256 MSa/s ADC, over 40 converter
blocks (2800 cycles).

| measurement | value | asserted by |
|---|---|---|
| ADC words captured | **640** | `test_the_converter_never_had_a_word_refused` |
| words dropped | **0** | *same* |
| converter blocks | **40** | *same* |
| window words delivered | **516** | `test_the_host_got_the_recorded_words_on_the_recorded_cycle` |
| last window at cycle | **2205** | *same* |
| both memory ports live | **140** cycles | `test_both_ports_are_live_together_and_never_in_the_same_region` |
| writer and reader in the same region | **0** | *same* |

Plus **II = 1** on every pipelined loop — the capture's `store_block`, the reader's `drain_window`
and `await_grant` — asserted by `test_every_pipelined_loop_reaches_ii_1` against the *achieved*
`PipelineII`, which Vitis reports separately from the target and which differs whenever it missed.

## The gate that needs no counter to be believed

`test_the_rtl_captures_the_ramp_with_no_gap` concatenates every window the host received and checks
it is a contiguous ramp. The source plays a ramp, so **a dropped block is a step in the numbers** —
visible in the data whether or not anything counted it.

That is the strongest statement of *nothing was lost* available, and it is deliberately not the drop
counter. `test_every_window_carries_CAP_OK_and_zero_lost` checks the counter too, because the two
agreeing is what says the design knows what it lost rather than merely happening to be right.

## The two-region claim, measured on the memory's own pins

`test_both_ports_are_live_together_and_never_in_the_same_region` reads the write and read address
pins out of the VCD and reports two numbers:

* **140 cycles with both ports live** — the memory is genuinely being used from both sides at once,
  which is what a true-dual-port memory is for. A design that had serialised them would show zero
  here and would be slower for no reason.
* **0 cycles with the writer and the reader inside the same region** — the guarantee two disjoint
  regions buy, **by construction** rather than by measurement.

`test_the_memorys_own_predicate_also_finds_nothing` then runs `bram_t2p.v`'s own read-during-write
check — same address, same cycle — and finds nothing, which follows from the row above.

**On TX the same scan finds 2 collisions**, benign by measurement rather than absent by construction,
because that design holds
[one region](../../guide/rf/rfshotbuf/tx_internal.md#finding-tx-holds-one-region-rx-holds-two). This
is the concrete difference between the two halves of the family.

## Why the windows alternate

`test_the_windows_alternate_between_the_two_halves` checks the base address on each window header
against the ping-pong order. The base is **on the wire**, not a parameter — a host learns which half
a window came from by reading it, which is the precedent
[`RfShotRx`](../../guide/rf/rfshotbuf/rx.md) sets and `RfShotTx` does not follow.

## Next

- [Running it](./run.md) — the build rungs that produce the RTL these gates read.
- [The design](../../guide/rf/rfshotbuf/rx.md) — the window header and what each field answers.
