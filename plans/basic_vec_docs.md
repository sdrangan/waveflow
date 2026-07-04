# Expand docs/examples/basic_vec into a walkthrough

**Status: plan, not yet executed.** Apply when no CLI/branch effort is in flight on the
working tree (tracked-doc edits under `docs/examples/basic_vec/`). Pairs with
`plans/overview_docs.md` — both are docs polish to do once ComplexField merges.

## Goal

`docs/examples/basic_vec/index.md` is a single short page (it was the Phase-4 front-door
stub). It reads hard because the concepts live in `guide/vectorization` and the example
page tries to do both. Split into a **multi-page walkthrough** matching the other examples
(regmap, stream_inband), that **complements** the guide (concrete example, *not* a re-teach
of operators/growth/two-paths — link to the guide for those).

## Key fact (frames the `vitis.md` page)

The basic_vec Vitis kernels are **hand-written**, not generated: `examples/basic_vec/
kernels.py` has parameterized C++ templates (`render_int_mac`/`render_float_mac`/
`render_fixed_mac`), deliberately minimal so the Python↔C++ parallel is explicit. This is
**different from the real Waveflow codegen flow** (poly / HwComponent examples generate the
kernel). So the page is named **`vitis.md`** (the hand-written Vitis equivalent), not
"codegen.md" — and it says so, pointing to the poly example for the generated flow.

Notable kernel details to surface:
- **int**: `ap_int<wy> y = a*b + c` — full-precision product+sum (ap_int grows; `wy` is the
  grown width); operands reconstructed bit-for-bit via `.range()`.
- **fixed**: `target_t y = a*b + c` — full-precision then **quantize-on-assign** to the
  declared target (mirrors the operator + explicit quantize).
- **float**: split intermediate (`t = a*b; y = t + c`) built with **`-ffp-contract=off`**
  (run.tcl) → **two roundings**, matching numpy `float32` rather than a fused FMA. (Same
  float-edge discipline as ComplexField §5.)

## Page map

| Page | nav | Content |
|---|---|---|
| `index.md` | 1 | What basic_vec demonstrates — one MAC `a*b+c` across **int/float/fixed**, bit-exact vs Vitis (the vectorization selling point, end-to-end); file map; run commands. Link to `guide/vectorization` for concepts. |
| `python.md` | 2 | The Python golden — declare `DataArray[Int.../Float.../Fixed...]`, the `a*b+c` op (operators), produce the golden bits. |
| `vitis.md` | 3 | The **hand-written** Vitis equivalent — the three kernel templates, how each mirrors the op (full-precision growth + `.range()` reconstruction; the float two-roundings detail). States it's hand-written for clarity; points to poly for the *generated* flow. |
| `eval.md` | 4 | The `build_dag` → Vitis csim → bit-compare; the bit-exact result across all three types. |

## Mechanics

1. Keep `index.md` (rewrite to the overview role); add `python.md`/`vitis.md`/`eval.md`
   with the front-matter `parent: basic_vec` (or whatever the section parent is) + nav 1–4.
2. Pull real code from `examples/basic_vec/{basic_vec_build.py,kernels.py,run.tcl}` —
   **verify any executed snippet runs** (operators are merged; the golden is real).
3. Cross-link: the four `guide/vectorization` pages already point at
   `../../examples/basic_vec/` — keep that landing on `index.md`. Add "concepts: see
   [Vectorization](../../guide/vectorization/)" from the example pages.
4. Link check (Grep tool / `grep -a`, NOT `grep -I`/`git grep`).
5. One small docs PR (can combine with `plans/overview_docs.md` if done together).

## Note
Don't duplicate the guide. The example answers "how is *this* example built and verified";
the guide answers "what are operators / the two paths / growth rules / why fast".
