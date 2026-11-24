# Review sheet: 01_pulse_report

For the reviewer, not the agent (no spec links this file).  Fill one sheet per run.  Run
`python lint_schema_use.py <trial folder>` first: it lists the lines to look at for items marked (L).

Run: ____________  arm: [ ] MCP only  [ ] MCP + skill   model: ________  tokens in/out: ________
Skill loaded (from summary.md): [ ] yes, at tool call # ___   [ ] no

Score each item **2** (as the guide does it), **1** (partly), **0** (not done / hand-rolled).

## A. The Python schemas (`pulse_msgs.py`)

| # | item | the pattern | score |
|---|---|---|---|
| A1 | the command is a `DataList` | one class, four fields in spec order | |
| A2 | `op` and `kind` are `EnumField`s over `IntEnum`s, **8 bits** | `EnumField.specialize(enum_type=..., bitwidth=8)` -- the default (minimum) width is wrong here | |
| A3 | `thresh` is `FixedField.specialize(16, 4, signed=False)`; the sample `FixedField.specialize(12, 1)` | not an `IntField` with a scale factor done by hand | |
| A4 | the samples are an array of the sample element, not a field per sample | `DataArray` / `read_array` / `get_array` of the element type | |
| A5 | the output is one `DataList` per kind, **each starting with `kind`**, plus a one-field `Tag` (or equivalent) to read the kind | `schema/python/tagged_union.md`; a single padded `DataUnion` scores 1 | |
| A6 | widths: `tx_id`, `nsamp`, `index`, `count` all 16-bit unsigned | | |

## B. The headers (`gen_headers.py`)

| # | item | the pattern | score |
|---|---|---|---|
| B1 | `StreamUtilsStep` + a `DataSchemaStep` per schema (enums included), `word_bw_supported=[32, 64]` | `schema/hls/codegen.md` | |
| B2 | `ArrayUtilsStep(<sample elem>, [32, 64])` for the samples | | |
| B3 | it ran and produced `include/` | check the folder | |
| B4 (L) | no generated file edited; no struct written by hand | summary.md flags edits to generated files | |

## C. The model (`pulse_model.py`)

| # | item | the pattern | score |
|---|---|---|---|
| C1 (L) | the command decoded by `Cmd().deserialize(words, word_bw=W)` | not shifts / masks / slicing | |
| C2 (L) | samples decoded by `read_array(..., elem_type=..., shape=nsamp)` | not a per-lane loop with `>> 12*k` | |
| C3 (L) | each output message built as a schema instance and `serialize`d | not words assembled by hand | |
| C4 | word counts from `nwords_per_inst(W)` | not `ceil(bits / W)` | |
| C5 | the threshold compare on the fixed-point values as the hardware does it | stored integers aligned, or `.real` of both; no float rounding surprise | |
| C6 | the worked example asserted at both widths, and it passes | run it | |
| C7 (L) | no defensive `int()` / `np.asarray()` around field reads (a commented one at a JSON / arithmetic boundary is fine) | `schema/python/datalists.md` | |

## D. The HLS body (`pulse_det_body.cpp`)

| # | item | the pattern | score |
|---|---|---|---|
| D1 (L) | the command read with `cmd.read_axi4_stream<W>(s_in)` (or `read_msg`) | not `s_in.read().data.range(...)` | |
| D2 (L) | the samples read with the generated lane routine (`<elem>_array_utils::read_axi4_stream_lane<W>`) in a lane loop | `vectorization/hls/arrayutils.md`; not `.range(12*k+11, 12*k)` | |
| D3 (L) | each output message a struct instance written with `write_axi4_stream<W>(m_out, last)` / `write_msg` | TLAST only on `DONE` | |
| D4 | `op == Op::END` -- the generated enum, not `== 1` | | |
| D5 | the compare in `ap_fixed` types (`x > cmd.thresh`) | not on raw bits | |
| D6 | the body shape: a loop per command, a straight-line loop over the sample words | `vectorization/hls/loop_optimization.md`; a state machine scores 1 | |
| D7 | it would plausibly compile: includes, namespaces, template arguments | reviewer's judgment | |

## E. Process (from summary.md)

| # | item | score |
|---|---|---|
| E1 | read the schema pages (or the skill) **before** writing `pulse_msgs.py` | |
| E2 | no API names guessed and then corrected by an error (count them: ___ ) | |
| E3 | `NOTES.md` layout at 32 bits is correct (check against `serialize`) | |

**Total:** ____ / 54.   **Hand-rolled packing anywhere (any 0 in C1-C3, D1-D3):** [ ] yes  [ ] no

Free notes -- what it got wrong and which line of the skill (or which missing line) would have
prevented it:
