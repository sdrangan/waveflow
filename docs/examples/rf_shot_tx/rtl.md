---
title: Taking it to RTL
parent: Playing a stored waveform
grand_parent: Examples
nav_order: 2
audience: hls
summary: "The XSI run for rf_shot_tx: one snapshot driven by two command bundles, thirty gates, and every measured number with the assertion that pins it. The playout block shapes, the last verdict's cycle, the converter counters that say the DAC was never starved, the write address range, the achieved II of five pipelined loops, the two read-during-write collisions that are recorded as a measurement rather than asserted away, and the three loosely-timed gates that replaced one byte-identical comparison."
---

# Taking it to RTL

`tests/examples/test_rf_shot_tx_xsi.py` — **30 gates**, run with `pytest -m xsi`. What xsim
elaborates is the wrapper `rf_shot_tx_top`; the testbench sees only AXI-Stream, and the converter
model consumes the playout exactly as it consumes any other design's.

**Both scenarios load the same `xsimk.dll`.** Two hand-written mains differ in three bundle names and
nothing else — that the RTL is one design is the point of running two streams through it.

Needs a prior `csynth` plus Vivado `xsim` and a C++ compiler. The gates **skip loudly** rather than
passing when either is missing, and `-m xsi` fails the session if any gate skips.

## Every number, and the gate that asserts it

Recorded at `nword=64`, `blk_words=16`, `depth=256`, region `[192, 256)`, 256 MSa/s DAC, over 1400
cycles.

| measurement | `cmd` | `cmd_loop` | asserted by |
|---|---|---|---|
| playout, converter blocks | `(F,3) (P,12) (F,7)` | `(F,3) (P,1) (F,2) (P,1) (F,15)` | `test_the_playout_has_the_recorded_block_shape` |
| last verdict at cycle | **269** | **500** | `test_the_scenario_was_consumed_and_the_last_verdict_landed_on_the_recorded_cycle` |
| DAC words taken | 359 | 359 | `test_the_dac_is_never_starved_on_either_path` |
| blocks the grid zero-filled | **0** | **0** | *same* |
| underruns | 1, at cycle 4 | 1, at cycle 4 | *same* |
| write addresses touched | `192..255` | `192..255` | `test_the_write_addresses_reach_the_last_element_and_no_further` |
| both memory ports live on the region | 18 cycles | 55 cycles | `test_the_handover_leaves_a_speculative_read_that_the_design_discards` |
| read-during-write collisions | **0** | **2** | *same* |

Plus, at `csynth`: **II = 1** on all five pipelined loops — the loader's `take_shot`, `drain_tail`
and `await_grant`, the player's `play_chunk`, and the re-layout — asserted by
`test_every_pipelined_loop_reaches_ii_1` against the achieved `PipelineII`, not the target.

## The three that carry an argument

**`blocks_zero_filled == 0` on both paths is the sharpest number here.** It counts blocks the
converter's grid had to fill *itself*. The infinite path's way to fail it is a player that
back-pressures the converter through a handover; the finite path's is a player that simply stops
writing when its passes run out — a failure mode the loop-only predecessor never had to survive,
because it never stops. Either way the grid fills the gap and this counter says so.

**The last verdict's cycle is a result, not the run's loop bound.** 269 for the finite stream is one
accepted load — a one-word header, a grant costing one poll period of the DAC-paced player, and 64
payload words at one per cycle — plus four refusals, two carrying a full payload to drain. The loop
stream's 500 is longer because it *accepts* three of its six frames and each acceptance pays the
grant wait again.

**The two collisions are recorded, not asserted away.** `bram_t2p.v`'s own predicate — same address,
same cycle, one port writing and the other reading — finds **0** on the finite path and **2** on the
loop path. They are benign, and the evidence is a *different* test: pysim raises on any read of a
yielded region, and `test_the_two_backends_agree_after_their_own_transients` shows every played
sample agrees, so the word is fetched and thrown away. The gate **pins both counts** rather than
asserting zero, because asserting zero here would be a green bought by choosing a scenario that never
preempts. Why they happen at all is
[in the guide](../../guide/rf/rfshotbuf/tx_internal.md#finding-tx-holds-one-region-rx-holds-two).

## What the gates would catch

* **A stall rather than a wrong answer.** `CMD_SENT == CMD_TOTAL` is checked on both streams. This
  family's first RTL run deadlocked after two words while every other counter still looked plausible.
* **`SHOT_BUSY` set for the wrong opcode.** `test_shot_busy_answers_a_finite_shot_and_only_a_finite_shot`
  needs both streams: a design that set `busy` for both opcodes passes the first half and fails the
  second; one that set it for neither passes the second and fails the first.
* **The grant wait quietly becoming a blocking read.**
  `test_the_grant_wait_is_still_a_loop_and_not_a_blocking_read` asserts a synthesized module named
  for the `await_grant` loop still exists. The obvious body — one blocking read right after the
  request — **deadlocks**, measured: the loader's `ap_CS_fsm` sat in state 1 for the whole run and
  `lock_if_cmd_write` never asserted once.

## Next

- [Running it](./run.md) — the build rungs that produce the RTL these gates read.
- [Internals](../../guide/rf/rfshotbuf/tx_internal.md) — the traps, the lock protocol, and the II table.
