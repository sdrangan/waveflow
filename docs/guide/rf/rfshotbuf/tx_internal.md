---
title: Internals
parent: RfShotBuf
grand_parent: RF converters
nav_order: 3
audience: agent
api: [RfShotTx, RfShotRx, ShotTxLoader, ShotTxPlayer, ShotPlayCmd, LockedT2pMemIF, PingPongCapture, PingPongWindow]
summary: "Internals of the RfShotBuf family for developers and agents: the three tasks and the composite that wires them, the lock protocol and the single ordering the whole thing turns on, every internal channel and why its depth is what it is, the on-wire bit layouts, the two hand-written task bodies, the measured II and cycle counts with the gate that produces each, and the findings that are easy to rediscover the hard way — the request/response deadlock, the reset trap, and why a yielded player still drives its read port."
---

# Internals

**You do not need this page to use either design.** Everything required to drive them from a host is
on [the TX page](./tx.md) and [the RX page](./rx.md). This page is for changing the designs, for
reviewing a change, or for an agent that has to reason about why a line is where it is.

Every claim below cites the file and line that makes it. Line numbers drift; the symbol names do not,
so a citation that no longer lands should be re-found by name rather than trusted.

## Where the source is

| what | where |
|---|---|
| the TX components, schemas and composite | [`waveflow/hw/rf_shot_tx.py`](../../../../waveflow/hw/rf_shot_tx.py) |
| the RX components, schema and composite | [`waveflow/hw/rf_shot_rx.py`](../../../../waveflow/hw/rf_shot_rx.py) |
| the lock interface and its pysim guard | [`waveflow/hw/locked_mem.py`](../../../../waveflow/hw/locked_mem.py) |
| the TX task bodies (hand-written C++) | `waveflow/build/shot_tx_loader_task.h`, `shot_tx_player_task.h` |
| the RX task bodies (hand-written C++) | `waveflow/build/pingpong_capture_task.h`, `pingpong_window_task.h` |
| the lock's C++ twin | `waveflow/build/mem_lock.h` |
| the worked examples and their gates | `examples/rf_shot_tx/`, `examples/rf_shot_rx/` |

The `_task.h` files are **hand-written** and shipped into an example's `include/` by
`RfShotTxStep` / `RfPingPongStep` / `RfShotBufStep` in `waveflow/build/streamutils.py`. The top that
calls them is generated. `run_iter` on each Python class is the **pysim twin**, not the source of the
RTL — the two are held together by the gates, not by codegen.

## TX: three tasks and a memory

`RfShotTx` is a composite of three free-running `hls::task` bodies plus one BRAM beside them
(`rf_shot_tx.py:836-955`).

```
    s_in ──▶ ShotTxLoader ──[lock]──▶ [ BRAM ] ──[lock]──▶ ShotTxPlayer ──▶ RfRelayoutToSlots ──▶ samp_out
               │   ▲ done                                        │                       
           resp_out└─────────────── rep ─────────────────────────┘
```

| task | job |
|---|---|
| `ShotTxLoader` | read a header, decide, take the region, write it, hand it back, answer |
| `ShotTxPlayer` | play the region a counted number of times or forever, and yield it on request |
| `RfRelayoutToSlots` | dense words → converter slot words; **last**, so it is the stage the converter back-pressures |

**The re-layout is last on TX and first on RX**, and that is not an arranged symmetry
(`rf_shot_rx.py:491-515`): the memory holds *dense* words on both sides, because dense is the
logic-side format a host can read and write without knowing anything about justification. The
conversion happens wherever the converter is, and the stage adjacent to the converter is the one that
carries `blk_words`.

### The channels, and why each depth is what it is

Three internal channels survive that the lock does not own (`rf_shot_tx.py:923-930`):

| channel | from → to | depth | why |
|---|---|---|---|
| `rep` | loader → player | 1 | exactly one play command is in flight by construction: one per accepted load |
| `done` | player → loader | 1 | a `done` cannot accumulate — a second finite load is refused until the first is harvested |
| `samp` | player → re-layout | 2 | the HLS default for a top argument; one beat of producer/consumer overlap is all an II=1 chain needs |

Everything else the two predecessors wired by hand — `pay`, `rdy_load`, `rdy_play`, `dense`, and both
`BramIF`s — is gone. One `add_if(self.lock)` (`rf_shot_tx.py:945`) files the two lock streams as
internal FIFOs **and** sweeps the two `BramIF`s into the RTL registry so the tasks' memory ports stay
boundary ports.

**The memory attribute is called `mem`, not `buf`** (`rf_shot_tx.py:935-937`), because the attribute
name becomes the Verilog *instance* name and `buf` is a primitive gate. The wrapper emitter refuses it
by name rather than letting `xvlog` fail on a syntax error that mentions no Python.

The boundary is stated explicitly (`rf_shot_tx.py:949`) as `add_comp` × `add_endpoint` order with
every internally-bound endpoint removed. The two `buf_*` entries are ports of the **kernel**, joined
to the memory inside the generated wrapper — which is why what a simulator elaborates is
`rf_shot_tx_top` and not `rf_shot_tx`.

## The lock, and the one ordering everything turns on

`LockedT2pMemIF` is a requester (`LockedMemMasterIF`) and an owner (`LockedMemSlaveIF`) over one
`T2pBram`, carrying four channels: two `StreamIF`s for the lock command and response (internal
edges), and two `BramIF`s (wrapper wires). On TX the loader is the requester and the player is the
owner — the owner is *the side that cannot stop*.

**Set the state before you grant. Always.**

```python
self.playing = False            # STOP TOUCHING IT ...
yield from self.lock.grant(...) # ... THEN grant
```
`rf_shot_tx.py:787-793`, and its C++ twin at `shot_tx_player_task.h:166-171`. Under `absolute_index` the same branch clears the **`pending`** bit as well: a stale arm surviving a handover would start a waveform nobody asked for at the next boundary.

Granting while still reading lets the loader write memory the player is reading — precisely the
collision the lock exists to prevent. **The pysim guard is what proves this ordering; the waveform
cannot.** `LockedMemSlaveIF.grant()` takes the region out of the owner's hands and raises on the very
next access; at RTL `bram_t2p.v`'s `$error` catches the same thing and **XSI discards `$error`**, so
nothing there would say a word. The gate for it is a *pysim* one:
`tests/hw/test_rf_shot_tx.py::test_a_player_that_grants_and_keeps_reading_raises`, which subclasses
the shipped player through the `player_cls` seam (`rf_shot_tx.py:760`) with that one line removed.

### FINDING: a two-stream request/response with a blocking read deadlocks

The obvious grant wait — write the request, then blocking-read the response — **deadlocks**. Vitis
schedules two operations on two streams with no data dependency between them into **one state**; that
state stalls on the empty response FIFO, and a stalled state performs none of its writes, so the
request is never sent.

Measured at RTL (Vitis HLS 2025.1, 2026-09-01): the loader's `ap_CS_fsm` sat in state 1 for the whole
run and `lock_if_cmd_write` never asserted once.

The fix is a `read_nb` **poll loop**, labelled `await_grant` (`mem_lock.h:98-118`). The owner needs no
such fix: its write is guarded by its read's result (`if (mem_lock_poll(...)) { ... grant ... }`), so
the dependency Vitis needs is already there.

`tests/examples/test_rf_shot_tx_xsi.py::test_the_grant_wait_is_still_a_loop_and_not_a_blocking_read`
asserts a synthesized module named for that loop still exists — which is what says the barrier has not
quietly been optimised back into a blocking read.

### FINDING: TX holds one region, RX holds two

`ShotTxLoader.region` is *"`[0, depth)` — the whole memory, which is the one region this design ever
asks for"*. Writer and reader therefore **do** share addresses, in turn — all of them, since
[`plans/rf_shot_geometry.md`](#) made the shot the buffer.

`play_chunk` is pipelined at II=1 and reads `buf[rd + i]` **unconditionally**, muxing the
filler in afterwards (`shot_tx_player_task.h:132-136`). A register guard was measured **not** to quiet
the port — Vitis owns the enable — so a *yielded* player keeps driving its read address. Consequently:

| scenario | both ports live on the region | same-address collisions |
|---|---|---|
| TX, finite stream (one grant, before anything plays) | 18 | **0** |
| TX, loop stream (three grants, two mid-play) | 55 | **2** |
| RX (two disjoint regions) | 140 | **0** |

TX numbers from `tests/examples/test_rf_shot_tx_xsi.py`; RX from
`tests/examples/test_rf_shot_rx_xsi.py`.

**The two collisions are not a defect, and the evidence is a different test.** pysim raises on any
read of a yielded region, and the two backends are byte-identical over the whole run
(`test_both_backends_agree_sample_for_sample`) — so the word is fetched and thrown away. The gate
**pins both counts** rather than asserting zero: a rise means the player started reading somewhere it
should not; a fall means the read port stopped being unconditional, which changes what a grant does
and does not enforce at RTL. Asserting `collisions == 0` would have been a green bought by choosing a
scenario that never preempts.

**"Disjoint regions are the mechanism" therefore covers RX and not TX.** The family is not uniformly
protected. A two-region TX would fix it and would also make waveform switching gapless; it is
recorded in `plans/rf_shot_unify.md` and is **not built**.

## The merge, and where it actually is

Both play modes are one body. The difference is four lines after the wrap
(`rf_shot_tx.py:872-885`, C++ twin `shot_tx_player_task.h:138-160`):

```python
if absolute or self.playing:            # the advance -- unconditional under absolute_index
    self.rd += bw
    if self.rd >= nw:
        self.rd = 0
        if self.playing:                # ... but the ACCOUNTING never is
            self.n_plays += 1
            if not self.loop:
                self.nrep_left -= 1
                if self.nrep_left <= 0:
                    self.playing = False
                    yield from self._send_done()
```

Both `loop` and `nrep_left` are register reads **outside** the pipelined loop body, which is why the
exit condition costs nothing in II — the shape one might expect to break II=1 does not.

### `busy`, and why it gates both opcodes but is set by only one

`busy` is set only on an accepted `SHOT_LOAD` (`rf_shot_tx.py:504`) and cleared on the *harvest* of a
`done` token (`rf_shot_tx.py:451`). While set, **every** load is refused, `SHOT_LOOP` included
(`rf_shot_tx.py:417`).

That asymmetry is the merge:

- **Set by `SHOT_LOAD` only** — a design that set it for both would answer `SHOT_BUSY` forever after
  the first loop, which is the defect the infinite-play predecessor was written to avoid.
- **Refuses both opcodes** — the objection is not to what the *arriving* shot is; it is that
  truncating the running one would be invisible, and that is true whatever replaces it.

`tests/examples/test_rf_shot_tx_xsi.py::test_shot_busy_answers_a_finite_shot_and_only_a_finite_shot`
needs **both** scenario streams to separate those two failures, which is why the gate runs one RTL
against two command bundles.

**`busy` clears on a harvest, not at the end of a run.** The `done` token is read non-blockingly and
*after* the header (`shot_tx_loader_task.h:99-106`), so `busy` is cleared by the **next arriving
frame**. A run whose last shot finishes with nothing behind it ends with `busy` still set, and that is
correct rather than a leak: the state is only ever read when a frame is being judged.

### `ShotPlayCmd` carries the host's own opcode

The loader → player wire is `{opcode: 2, nrepeat: 16}` (`rf_shot_tx.py:246-281`), one beat, and the
opcode is the host's `SHOT_LOAD` / `SHOT_LOOP` rather than a parallel `PLAY_FINITE` / `PLAY_LOOP`
vocabulary invented for the internal wire.

Two rules cover every case:

- **`nrepeat == 0` means play nothing** — the existing convention for a `SHOT_SHORT` shot, so no third
  mode is needed to express it.
- **`opcode` says who is waiting.** `SHOT_LOAD` → the loader is blocked on a `done`; `SHOT_LOOP` →
  nobody is, and the player must not send one. A spurious `done` would clear a `busy` that a *later*
  finite shot set, and the next load would preempt it — the exact truncation `SHOT_BUSY` exists to
  prevent, arrived at from the other side.

It is a schema rather than a raw word with a sentinel, because a sentinel in a bare `ap_uint` is how a
design ends up comparing against a magic number in two places.

### The play command goes out BEFORE the release

`rf_shot_tx.py:498-500`, C++ at `shot_tx_loader_task.h:188-190`:

```python
yield from self.rep_out.write(cmd)
yield from self.lock.release()
```

The player reads that command on the RELEASE branch of its poll (`shot_tx_player_task.h:177`), so
ordering the two writes this way makes that read a **bounded wait** rather than a guess. The read is
*control-dependent* on the poll's result, so nothing can hoist it above the loader's writes — which is
the shape that produced the deadlock above. Even a scheduler that reordered the two writes would be
correct, only slower by a beat.

## The verdict chain, in the order it is tested

`rf_shot_tx.py:408-422`, C++ at `shot_tx_loader_task.h:125-135`:

1. opcode not one of the three → `SHOT_BAD_OPCODE`
2. `busy` → `SHOT_BUSY`
3. otherwise accept; `SHOT_SHORT` is decided **after** the transfer, from how much arrived

**Two tests, and it was four.** The middle two read the header's `nsamp` — `== 0` and
`!= nword * samp_per_word` — and the field is gone, so they are too. What is left is the one thing a
header can still be malformed about. The verdict kept its wire value and changed its name:
`SHOT_WRONG_LEN` was already reporting a wrong *opcode* here, which is an overload a host debugging a
status byte pays for.

**Malformed before transient**, deliberately: a command that is wrong *and* badly timed should be told
the thing it can fix. Retry repairs a `BUSY`; nothing repairs an opcode this design does not know.

## On-wire layouts

Every schema packs into exactly one 64-bit word. The generated headers are `rf_shot_tx_hdr.h`,
`rf_shot_tx_resp.h`, `shot_play_cmd.h`, `capture_window_hdr.h` — a body that hand-rolled the packing
would be a second author of one statement.

**The response derives its width from the geometry** (`plans/rf_shot_wire_format.md` Part A).
`ShotTxHdr` and `ShotTxResp` are `ParamSchema`s, and `shot_tx_schemas(depth, samp_per_word)` is the
one place that decides — the pysim twin, the generated C++ and the build's `DataSchemaStep` all take
their pair from it, so they cannot disagree about the wire.

Only the response varies now: since `plans/rf_shot_geometry.md` the header carries no length, so its
layout is identical at every geometry.

| schema | fields (bits) | total |
|---|---|---|
| `ShotTxHdr` | `opcode` 2, `tid` 16, `nrepeat` 16, `_rsvd` 30 | **64, exactly** |
| `ShotTxResp` | `tid` 16, `status` 8, `nsamp_loaded` **derived**, `_rsvd` **declared** | **64, exactly** |
| `ShotPlayCmd` | `opcode` 2, `nrepeat` 16 | 18 |
| `CaptureWindowHdr` | `status` 8, `base_addr` 28, `n_dropped` 28 | **64, exactly** |
| `MemLockCmd` / `MemLockResp` | `opcode`/`status` 8, `start_addr` 28, `end_addr` 28 | **64, exactly** |

At the gated geometry — `depth=64`, `samp_per_word=4`, so a full buffer is 256 samples —
`nsamp_loaded` is **16** bits and the emitted header is:

```c
struct ShotTxHdr {
    ap_uint<2>  opcode;    // res.range(1, 0)
    ap_uint<16> tid;       // res.range(17, 2)
    ap_uint<16> nrepeat;   // res.range(33, 18)
    ap_uint<30> _rsvd;     // res.range(63, 34)  -- reserved, must be zero
    static constexpr int bitwidth = 64;
};
```

**Three things that table says, and each was a decision:**

* **`opcode` is 2 bits because there are three opcodes.** It was 8, which was a number nobody chose.
  Two bits also means **3 is the only illegal opcode the wire can carry**, which is what makes
  `SHOT_BAD_OPCODE` reachable from a real frame rather than only from a hand-built object.
* **`nsamp_loaded` is derived, not checked.** The width used to be 16 because `IDX_BW` said so — a
  constant imported from `rf_samp_buf`, *the superseded family* — and construction **refused** a
  geometry that did not fit it. Now `nsamp_bw_for(depth, samp_per_word)` sizes the field from the
  design, so the largest value it can ever report fits by construction. The witness is
  `tests/hw/test_rf_shot_tx.py::test_a_buffer_too_large_for_the_old_16_bit_field_builds_and_round_trips`,
  which builds the geometry the old code rejected and round-trips a 262144-sample length. It used to
  watch the header's `nsamp`; `plans/rf_shot_geometry.md` moved it onto the response's field, which
  is the only length left on the wire and fails the same way if it wraps — reporting a partial load
  as a full one.
* **The word stays 64 bits and the slack is a declared `_rsvd` field.** The point of deriving the
  widths is that a field cannot silently overflow, *not* that the message gets smaller — a stable
  wire size is what a DMA moves cleanly, so the padding is named rather than incidental.
  `test_both_messages_are_one_word_and_the_padding_is_declared` holds that across four geometries.

**Why `nsamp_loaded` has a floor of 16 bits rather than fitting exactly.** The floor's original
reason retired with the header's length: it stopped a host's mistyped length from *aliasing onto a
legal one* on the way in, and there is no length on the way in. What is left is a field the host
**reads**, and holding it at one width across geometries is what lets a host be compiled against this
wire once. The padding is declared either way, so a narrower field would buy nothing and cost that.

## `absolute_index`, and the one place the split matters

`plans/rf_shot_absolute.md`. Three edits turn the read pointer into a timestamp, and they are the
whole of the mode:

1. **the advance is unconditional** — `rd` moves and wraps on every firing, filler included;
2. **accept no longer resets `rd`** — it sets a `pending` bit instead;
3. **`playing` turns on when `pending && rd == 0`**, tested *before* the write loop so the chunk
   starting at `rd == 0` is the first one played.

**The trap is in the first one, and it fails silently.** Today the wrap and the repeat count live in
one block. Moving the whole block out of `if (playing)` is the natural edit and it is wrong:
`nrep_left` would tick once per pass while the design plays filler, so a finite shot ends early or
never starts — and the word counts still add up, so no counter gate catches it. The advance comes out
of the guard; the accounting stays in. `tests/hw/test_rf_shot_tx.py` ships the wrong edit as a class
and shows what it produces: a perfectly good *shorter* signal, two passes where three were asked for,
answered `SHOT_LOADED` with the right `nsamp_loaded` throughout.

**The `SHORT` path must not defer either.** `if (!playing && !loop) done_out.write(1)` answers a shot
that must never play, and the loader is *blocked* on that token — routing it through `pending` would
deadlock rather than fail a gate, because the boundary that would release it is one the shot never
reaches. So the answer is decided on *armed*, at accept.

One template argument, `ABS`, and `if (ABS)` branches Vitis folds — one body, not two, because two
copied bodies would be two designs that drift.

## The reset trap, and which body is on which side of it

**An `hls::task` that WRITES before it READS advances during reset.** An owner cannot avoid that
shape — writing without being asked is what *the side that cannot stop* means — so `ShotTxPlayer`'s
statics all carry `#pragma HLS reset` (`shot_tx_player_task.h:102-119`, five of them since `absolute_index` added `pending`) **and** the build needs
`config_rtl -reset state`, which is what actually closed it under Vitis 2025.1. The solution config
lives in the example's build (`examples/rf_shot_tx/rf_shot_tx_build.py`, `SOLUTION_CONFIG`).

`ShotTxLoader` opens with a **blocking** read of the header (`shot_tx_loader_task.h:99`), so at reset
its input is empty and it stalls. It inherits none of this. `busy` carries the pragma anyway, because
it is state a reset should clear.

## Measured

Every number here is asserted by a gate, named beside it. **They are measurements, not targets** — do
not re-record one without diagnosing why it moved.

### II, achieved rather than targeted

`tests/examples/test_rf_shot_tx_xsi.py::test_every_pipelined_loop_reaches_ii_1`, Vitis HLS 2025.1,
xczu48dr, 4 ns target. Achieved `PipelineII`, not the target — Vitis reports both and they differ
whenever it missed.

| module | loop | II |
|---|---|---|
| `shot_tx_loader_task_64_64_4` | `take_shot` | **1** |
| | `drain_tail` | **1** |
| | `await_grant` | **1** |
| `shot_tx_player_task_64_64_16_0` | `play_chunk` | **1** |
| `rf_relayout_to_slots_task_64_4_2_s` | *(unlabelled)* | **1** |

The absolute-index build synthesizes the same five loops at the same II, with the player's modules
named `shot_tx_player_task_64_64_16_1_*`
(`tests/examples/test_rf_shot_tx_abs_xsi.py::test_every_pipelined_loop_still_reaches_ii_1_in_the_absolute_build`).
**Read these names off the report directory, never predict them**: adding the fourth template argument
renamed the player's modules in *both* builds, including the one whose new argument is zero, and a
name that misses makes the II gate skip — which reads as a pass.

Estimated period **2.772 ns**, Fmax **360.8 MHz**.

**Label every pipelined loop.** Vitis names an unlabelled loop `VITIS_LOOP_<line>_1` and nests that
name into its children, so a comment edit renames the synthesized module — and a gate that looks the
II up by name then **misses and skips**, which reads as a pass. The re-layout's loop is deliberately
left unlabelled because other gates name that module.

### The two RTL scenarios

`tests/examples/test_rf_shot_tx_xsi.py`, 1400 cycles, `depth=64`, `blk_words=16`, region
`[192, 256)` at the top of the memory, 256 Msamp/s DAC. One design, one `xsimk.dll`, two command
bundles.

| | `cmd` (finite) | `cmd_loop` (infinite) |
|---|---|---|
| verdicts | `LOADED / BUSY / BAD_OPCODE / BAD_OPCODE / END→LOADED` | `LOADED / BAD_OPCODE / BAD_OPCODE / LOADED / SHORT / END→LOADED` |
| playout, converter blocks | `(F,3) (P,12) (F,7)` | `(F,3) (P,1) (F,2) (P,1) (F,15)` |
| last verdict at cycle | **273** | **502** |
| DAC words taken | 359 | 359 |
| blocks the grid zero-filled | **0** | **0** |
| lock grants | 1 | 3 |
| write addresses touched | `0..63` | `0..63` |

Both playouts match the pysim golden **sample for sample once each capture is aligned on its own
playout log** — see `plans/lt_transient.md`, which retired the byte-identical-from-`t=0` comparison
along with the metronome that made it possible.

`blocks_zero_filled == 0` on both paths is the sharpest number. The infinite path's way to fail it is
a player that back-pressures the converter through a handover; the finite path's is a player that
simply stops writing when its passes run out. **Quiet is a value.**

**The region used to sit at the top of the memory on purpose**, and does not any more.
`base + offset` was the shape of the byte-versus-word addressing bug: consistently mis-scaled
addressing round-trips *perfectly* right up to the point where the memory wraps, so the assertion was
which elements the writer actually touched, not that the data came back.
`plans/rf_shot_geometry.md` removed `base`, and its argument was that **the bug class disappears
rather than going untested** — the loader writes `mem[i]`, the player reads `mem[i]`, and the only
address arithmetic left is the read pointer's wrap at `depth`, which is a mask because `depth` is a
power of two. So the write-range assertion stays (it is what says the counted load pass *fills* the
buffer) and a new one covers the wrap: the player must sweep every element, read nothing outside, and
return to 0 — twice, since `cmd` plays three passes.

**The last verdict moved 269 → 273 and 500 → 502 with that change, and the cause was measured.** Not
the geometry: the old design rebuilt at the *new* geometry — depth 64, base 0, four-field header —
still answered 269. Not the scenario: the frames' word counts are unchanged and refusing a frame from
a different branch of the verdict chain costs nothing. Not the load: the write burst runs cycles
70..133 either way. It is the **wire** — one fewer header field to unpack and two fewer verdict tests
means a differently scheduled body, and the response lands a few cycles later. The same re-timing
slid the writer's sweep past the yielded player's address rather than through it, which is why
`cmd_loop`'s read-during-write collisions went 2 → 0.

### RX

`tests/examples/test_rf_shot_rx_xsi.py`, `depth=256` split into 2 regions, `blk_words=16`, 40
converter blocks: **640 words captured, 0 dropped**, window 516 words, last window at cycle **2205**,
**140** cycles with both ports live, **0** with writer and reader in the same region. Fmax
**367.3 MHz**.

## Traps, collected

- **Set filler before granting.** The one ordering everything turns on. Proven in pysim, invisible at RTL.
- **A two-stream request/response with a blocking read deadlocks.** Use a `read_nb` poll loop.
- **Enable-gating is closed.** A register guard costs nothing in II and still does not quiet the
  memory port. Disjoint regions are the mechanism — and TX does not have them.
- **Label every pipelined loop**, or the II gate misses by name and skips.
- **XSI discards `$display` and `$error`.** A condition an RTL model reports textually must be gated
  from the VCD instead, and always paired with a run that is supposed to trip it.
- **A `--force` regeneration emitting identical bytes used to look stale by mtime** and silently
  skipped every affected gate. The staleness guard now hashes sources; `-m xsi` **fails** if a gate
  skips.

## Next

- [Transmit — `RfShotTx`](./tx.md) and [Receive — `RfShotRx`](./rx.md) — the user-facing pages.
- `plans/rf_shot_unify.md` — how four designs became two, and what each stage measured.
- `plans/t2p_lock_chan.md` — the lock itself, and what its two stages built.
