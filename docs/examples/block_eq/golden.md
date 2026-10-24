---
title: The equalizer's golden
parent: Block equalizer
nav_order: 3
---

# The golden

Everything on this page is a pure function or a schema, in the first three sections of
[`block_eq.py`](../../../examples/block_eq/block_eq.py): the schemas, the golden, the scenarios. It has
the same shape as [the polynomial's golden](../stream_kernel/golden.md); what is new is a second
computation, a message between the stages, and float32 arithmetic whose rounding order is fixed.

## The schemas

Four messages on the boundary -- `CmdHdr`, `Resp`, `Status` and the `StatusCode` enum -- and one between
the stages:

```python
class GainHdr(DataList):
    """gain -> apply: the command's identity, the rows that have a g, and how gain's part ended."""
    elements = {
        "tx_id":  {"schema": U16,             "description": "the command it answers"},
        "nvalid": {"schema": U16,             "description": "rows with a g, from row 0"},
        "code":   {"schema": StatusCodeField, "description": "OK, BAD_NOISE (row nvalid) or TLAST_EARLY (estimates)"},
    }
```

`GainHdr` is the command [travelling with the data](../../guide/systems/command_response.md): it tells
`apply` which command the `g` values answer, how many there are, and whether `gain` saw an error.
`SCHEMA_CLASSES` lists all five in dependency order; the build generates a C++ header for each, with
**framed** readers and writers ([`DataSchemaStep(..., framed=True)`](../../guide/comp_codegen/freerunning_override.md#framed)),
so the same struct is read from a top's AXI4-Stream port or from an internal FIFO.

## The arithmetic

Two functions, each the formula of [What an equalizer does](theory.md#what-the-example-computes) with its
rounding order written out:

```python
def gain_golden(hr, hi, n):
    """g = conj(h) / (|h|^2 + N), row by row: t1 = hr*hr, t2 = hi*hi, d = t1 + t2, d = d + N,
    q = 1/d, gr = hr*q, gi = -(hi*q)."""

def apply_golden(gr, gi, rr, ri):
    """z = g r: p1 = gr*rr, p2 = gi*ri, zr = p1 - p2, p3 = gr*ri, p4 = gi*rr, zi = p3 + p4."""
```

Every value is `np.float32` and every operation rounds once, to float32:

```text
gain, per row:              apply, per symbol:
  t1 = hr * hr                p1 = gr * rr
  t2 = hi * hi                p2 = gi * ri
  d  = t1 + t2                zr = p1 - p2
  d  = d + N                  p3 = gr * ri
  q  = 1 / d                  p4 = gi * rr
  gr = hr * q                 zi = p3 + p4
  gi = -(hi * q)
```

The order is part of the specification, because float32 rounding is not associative: `hr / d` and
`hr * (1 / d)` can differ in the last bit, and `(t1 + t2) + N` can differ from `t1 + (t2 + N)`. The HLS
bodies write the same operations in the same order, one statement each
([The HLS bodies](hls.md#the-arithmetic-one-statement-an-operation)), so the two agree bit for bit. A
**fused multiply-add** -- `p1 - p2` computed as one operation, rounding once -- would not match; the
bodies keep every product its own statement.

### Signed zeros {#signed-zeros}

Results are compared **by their bits**, so `-0.0` and `+0.0` differ. A real `h` gives `gi = -(0 * q)`,
which is `-0.0`, and it carries into the results: in row 2 of the worked examples `gr = 0 * q = +0.0`, and
the second symbol's `zi = (0.0 * -3) + (gi * 0) = -0.0 + -0.0 = -0.0`. The golden keeps every one of them,
and so must the hardware.

## The worked examples

`worked_cmd()` is three rows of two symbols, chosen so a reader can check them by hand:

| `h` | `N` | `g` | `r` | `z` | bits of `z` |
|---|---|---|---|---|---|
| 1 | 1.0 | 0.5 - 0.0j | 2 + 4j | 1.0 + 2.0j | `3f800000 40000000` |
| | | | -1 + 0.5j | -0.5 + 0.25j | `bf000000 3e800000` |
| 2j | 0.5 | 0.0 - 0.44444445j | 1 + 1j | 0.44444445 - 0.44444445j | `3ee38e39 bee38e39` |
| | | | -3j | -1.3333334 - 0.0j | `bfaaaaab 80000000` |
| 3 + 4j | 0.25 | 0.118811876 - 0.15841584j | 3 + 4j | 0.99009895 + 0.0j | `3f7d7720 00000000` |
| | | | 1 - 2j | -0.1980198 - 0.3960396j | `be4ac5b4 becac5b4` |

Row 1: `|h|^2 + N = 2`, so `g = 1/2`. Row 2: `|h|^2 + N = 4.5`, so `g = -2j / 4.5`. Row 3 is the strong
channel of [the theory page](theory.md#the-mmse-gain-the-noise-limits-the-gain): `g` is close to
`1/h = 0.12 - 0.16j`, and `g h = 0.99009895`, just short of 1. The twelve result floats' bits are the
first gate, `test_the_golden_reproduces_the_worked_examples_bit_for_bit` in
[`test_block_eq.py`](../../../tests/examples/test_block_eq.py), and two of them are `-0.0` (`80000000`).

## Intent, stimulus, and expected outputs

A scenario is a list of **intents**, `Cmd(tx_id, h, n, r, est_words=None, sym_words=None)`: per row the
channel and the noise, the symbols as an `nrows x m` array, and how many **words** of the estimates and of
the symbols are actually sent (fewer is an early TLAST). From it:

- `stimulus(cmds)` -- the input stream: per command three bursts, the `CmdHdr`, the estimates, the
  symbols, each serialized by its schema or by the float32 lane writer (`write_array`), never by hand.
- `gain_part(c)` -- what `gain` computes from what arrived: `(nvalid, code, gr, gi)`. Every lane of every
  word that arrived counts, so a row is valid when its three floats arrived; the first `N <= 0` is
  `BAD_NOISE` at that row.
- `outcome(c)` -- `(results written, status)`: the rows with a `g` times `m`, cut short if the symbols
  ended first -- the first cut in row-major order wins, and at a tie, `gain`'s code.
- `expected(cmds)` -- the two output streams: per command the `Resp`, the results burst (if any) and the
  status, up to and including the first error; after it, nothing.
- `link_expected(cmds)` -- the stream between the stages: per command the `GainHdr` and, if any, the `g`
  burst. It is `gain`'s expected output **and** the stimulus of `apply`'s own harness
  ([The system flow](system_flow.md#four-systems)), so `apply` is tested before `gain` exists.

As in the polynomial, the expectation is computed **from the intents**, not from the stimulus: a bug in
`stimulus` cannot hide by being repeated in `expected`.

## The scenarios

- `default_cmds()` -- seven commands: the worked examples; then commands of every shape (1 x 1, 4 x 7,
  16 x 1, 2 x 33); then a 5 x 3 command whose row 2 has `N = 0` (`BAD_NOISE`: the six results of rows 0
  and 1, then the status); then a command that is never answered. Six statuses, the last `BAD_NOISE`.
- `early_cmds()` -- four commands: two good; a 4 x 6 command whose symbols end after 9 of 24 words
  (`TLAST_EARLY`: nine results, then the status); one never answered. Three statuses, the last
  `TLAST_EARLY`.

Both are random but seeded (`default_cmds(seed=3)`, `early_cmds(seed=4)`), so the stimulus is the same on
every run, in pysim and at RTL.

Next, [The stages](stages.md) that have to produce these outputs.
