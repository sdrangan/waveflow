---
title: The equalizer's HLS bodies
parent: Block equalizer
nav_order: 5
---
# The HLS bodies

Each stage's body is hand-written, the twin of its `run_iter`:
[`src/eq_gain_task.h`](../../../examples/block_eq/src/eq_gain_task.h) and
[`src/eq_apply_task.h`](../../../examples/block_eq/src/eq_apply_task.h). Everything around them is
generated: the schemas' structs, the float32 lane routines, the broadcast's body, and three tops. As in
[the polynomial&#39;s kernel](../stream_kernel/kernel.md#the-hls-body), a body is
[straight-line loops, one per part of the message](../../guide/vectorization/hls/loop_optimization.md),
and never packs or unpacks a word by hand.

## One body, two placements

Each body takes its ports' **word types** as template parameters:

```cpp
template <int W, typename TI, typename TO>
static void eq_gain_task(hls::stream<TI>& s_in, hls::stream<TO>& m_g);

template <int W, typename TI, typename TG, typename TO, typename TS>
static void eq_apply_task(hls::stream<TI>& s_in, hls::stream<TG>& s_g,
                          hls::stream<TO>& m_out, hls::stream<TS>& m_status);
```

The stage's `kernel_task()` names the body, its argument order, and which ports are framed:

```python
def kernel_task(self):
    ports = ("s_in", "s_g", "m_out", "m_status")
    return KernelTask("eq_apply_task", "eq_apply_task.h", ports, template_args=(W,), framed=ports)
```

The generator supplies the types where the body sits ([One body, wherever it
sits](../../guide/comp_codegen/freerunning_override.md#framed)). Inside the composite, `gain` reads and
writes `framed_word<64>` FIFOs, and `apply` reads `framed_word<64>` and writes the top's `axi4s_word<64>`
ports; as a stage alone, every port is the top's `axi4s_word<64>`. The generated composite top,
`block_eq.cpp` (`streamutils::` dropped from the task lines):

```cpp
hls_thread_local hls::stream<streamutils::framed_word<64> > est_gain;
#pragma HLS STREAM variable=est_gain depth=128
...
hls_thread_local hls::task t0(stream_broadcast_task<64, axi4s_word<64>, framed_word<64>, framed_word<64>>, s_in, est_gain, est_apply);
hls_thread_local hls::task t1(eq_gain_task<64, framed_word<64>, framed_word<64>>, est_gain, g);
hls_thread_local hls::task t2(eq_apply_task<64, framed_word<64>, framed_word<64>, axi4s_word<64>, axi4s_word<64>>, est_apply, g, m_out, m_status);
```

and the stage alone, `eq_gain.cpp`, is one task on the top's ports:

```cpp
hls_thread_local hls::task t0(eq_gain_task<64, axi4s_word<64>, axi4s_word<64>>, s_in, m_g);
```

A body reads and writes only through helpers that take either word type -- a schema message with
`streamutils::read_msg<W>` / `write_msg<W>`, a word of float32 lanes with
`float32_array_utils::read_boundary_lane<W>` / `write_boundary_lane<W>` -- so it never names one.

## `eq_gain_task`

One firing is one command, in four loops:

1. **The command and the estimates.** `read_msg<W>(hdr, s_in)` reads the `CmdHdr`. The `EST` loop reads
   a word an iteration at II=1 -- two floats, through `read_boundary_lane` -- into `est[3 * EQ_MAX_ROWS]`,
   partitioned `cyclic factor=6` -- a multiple of both 2 and 3 -- so this loop's two writes a cycle and
   the next loop's three reads a cycle each land in separate banks. An early
   TLAST ends the loop and records `est_early`; `got` counts the floats that arrived.
2. **`g`, row by row.** The `ROWS` loop, a row an iteration at II=1, over the rows whose three floats
   arrived (`got / 3` if the estimates ended early, else `nrows`), computes `g` in the golden's order
   ([below](#the-arithmetic-one-statement-an-operation)) into `gr[]`, `gi[]`. The first row with
   `!(n > 0.0f)` sets `nvalid = r` and `BAD_NOISE` -- written so a NaN noise is bad too, as in the
   golden's `~(n > 0)`.
3. **To `apply`.** `write_msg<W>(gh, m_g)` writes the `GainHdr`; the `G` loop writes the `nvalid` `g`
   values, one row a word, TLAST on the last.
4. **Keep consuming.** The `SYM` loop reads and drops the `nrows * m` symbol words, a word an iteration,
   and stops at an early TLAST.

Then, if `code` is not `OK` or the symbols ended early, `halted = true`.

## `eq_apply_task`

Also one firing a command, in three loops and a status:

1. **The command, then the estimates discarded.** `read_msg<W>(hdr, s_in)`, then the `EST` loop reads
   the `ceil(3 * nrows / 2)` estimate words and keeps nothing -- **before** anything is read from `s_g`.
2. **`g`, from `gain`.** `read_msg<W>(gh, s_g)`, then the `G` loop reads `nvalid` words into
   `gr[EQ_MAX_ROWS]`, `gi[EQ_MAX_ROWS]`.
3. **The results.** `write_msg<W>(resp, m_out)` writes the `Resp`. The `SYM` loop runs `need = nvalid * m`
   iterations at II=1, a symbol an iteration: read a word, look up its row's `g` (`row` and `col` are
   counters, not a division), compute `z` into two lanes, `write_boundary_lane` them. TLAST goes on the
   last result, or on the result answering an input word whose TLAST came early -- which also ends the
   loop. `done` counts the results written.
4. **The status.** `TLAST_EARLY` if the symbols were cut before `need`, else `gain`'s `gh.code`; then
   `halted = true` on anything but `OK`.

When `nvalid` is less than `nrows` -- `gain` found an error -- `apply` reads only `need` symbols and then
halts, never reading the rest of the command: the error is fatal, and nothing after it is read
([Errors are fatal](protocol.md#errors-are-fatal)).

## The arithmetic: one statement an operation

Both bodies write every float operation as its own statement, in the golden's order:

```cpp
// gain, per row                       // apply, per symbol
const float t1 = hr * hr;              const float p1 = g_r * lane[0];
const float t2 = hi * hi;              const float p2 = g_i * lane[1];
float d = t1 + t2;                     const float p3 = g_r * lane[1];
d = d + n;                             const float p4 = g_i * lane[0];
const float q = 1.0f / d;              z[0] = p1 - p2;
gr[r] = hr * q;                        z[1] = p3 + p4;
const float t = hi * q;
gi[r] = -t;
```

Each product is stored before it is added, so no multiply and add are fused into one rounding, and the
results are the golden's bit for bit -- the [RTL gates](system_flow.md#the-gates) compare them by their
bits. `gi[r] = -t` is a sign flip, so a zero `hi` gives `-0.0`, as in the golden's `-(hi * q)`
([signed zeros](golden.md#signed-zeros)).

## The fatal error: `halted`

```cpp
static bool halted = false;
#pragma HLS reset variable=halted
if (halted) {
    return;
}
```

A `static` flag under `#pragma HLS reset`, as in the polynomial: once set, every later firing returns at
once, reading and writing nothing, until a reset clears it. Each stage has its own; after an error both
halt -- `gain` on its own code or on its symbols ending early, `apply` on the status it writes.

## What csynth reports

From the composite's csynth report (`block_eq_proj/solution1/syn/report/csynth.rpt`, written by the
[`csynth`](../../guide/build/xsi_system.md#csynth) step; Vitis HLS 2025.1, `xc7z020-clg484-1`, 10 ns
target): every loop of both bodies achieves II=1. The longest iterations are `gain`'s `ROWS` loop, 37
cycles -- the division is in it -- and `apply`'s `SYM` loop, 13 cycles; at II=1 those are latency, not
rate.

The division does not stand in the way of II=1: Vitis pipelines the float divider (16 cycles of latency,
a new division every cycle), so it lengthens the iteration, not the interval. Since `gain` needs only one
`g` per row, an II above 1 would also have been an option, and would let HLS share the multipliers and
adders. But `ROWS` runs between the estimates and the symbols in each firing, so its interval adds to every
command's time, and at II=1 the loop is cheap: about one cycle a row.

Next, [The system flow](system_flow.md): each stage alone, then the composite, at RTL.
