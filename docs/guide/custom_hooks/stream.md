---
title: Stream — process as you read
parent: Custom Hooks
nav_order: 3
audience: hls
api: [read_axi4_stream_lane, write_axi4_stream_lane, read_stream_lane, write_stream_lane, synthesizable]
summary: "The stream hook pattern: data arrives incrementally on an AXI-Stream port, so the hook owns its own lane loop — read pf lanes, UNROLL the compute, write pf lanes — carrying TLAST framing and returning an error status. Walked through the poly evaluator in examples/stream_inband."
---

# Stream — process as you read

When the operand arrives **incrementally** on an AXI-Stream — too large to materialize, or simply
produced beat by beat — the hook can't wait for a resident block. It owns the data movement itself: a
**lane loop** that reads the next `pf` elements off the stream, computes them, and writes `pf`
results out, carrying `TLAST` framing as it goes.

The worked example is the polynomial evaluator
([`examples/stream_inband/poly_body_impl.tpp`](../../../examples/stream_inband/poly_body_impl.tpp)):
samples stream in, each is run through a Horner-form polynomial, results stream out.  It is a
body-only kernel (`cpp_body`), so that file is the whole kernel; the lane loop below is its
per-transaction helper, `transaction()`.

## The lane loop

> This is the word-per-iteration shape of the lane loop.  For the other shape -- one element per
> iteration, a word read every `PF` iterations -- see
> [Design patterns for loop optimization](../vectorization/hls/loop_optimization.md).

`pf = float32_array_utils::pf<in_bw>()` is the number of elements packed per stream word. The loop
steps by `pf`: [`read_axi4_stream_lane`](./reference.md) pulls the next lane off `s_in`, the compute
is `UNROLL`-ed across the `pf` lanes, and [`write_axi4_stream_lane`](./reference.md) pushes the
result lane to `m_out`:

```cpp
template <int in_bw, int out_bw>
ap_uint<8> transaction(const PolyCmdHdr& cmd_hdr,             // carries the coefficients
                       hls::stream<streamutils::axi4s_word<in_bw>>& s_in,
                       hls::stream<streamutils::axi4s_word<out_bw>>& m_out) {
    ...                                                        // response header, coeffs[4]
    static const int pf = float32_array_utils::pf<in_bw>();
    float x_lane[pf], y_lane[pf];
#pragma HLS ARRAY_PARTITION variable=x_lane complete dim=1
#pragma HLS ARRAY_PARTITION variable=y_lane complete dim=1

    ap_uint<8> err = code(PolyError::NO_ERROR);
    for (int i = 0; i < cmd_hdr.nsamp; i += pf) {
        const int nrem = cmd_hdr.nsamp - i;
        const int lane_count = (nrem < pf) ? nrem : pf;          // tail lane is short
        const bool final_word = (nrem <= pf);
        streamutils::tlast_status lane_tlast = streamutils::tlast_status::no_tlast;
        float32_array_utils::read_axi4_stream_lane<in_bw>(s_in, x_lane, nrem, lane_tlast);
        const bool in_tlast = (lane_tlast == streamutils::tlast_status::tlast_at_end);

        for (int k = 0; k < pf; ++k) {
#pragma HLS UNROLL
            if (k < lane_count) y_lane[k] = eval_poly_horner(coeffs, x_lane[k]);
        }

        // TLAST on the last lane -- or on this one, if the input burst ended early.
        float32_array_utils::write_axi4_stream_lane<out_bw>(y_lane, m_out,
                                                            final_word || in_tlast, nrem);
        ...                                                    // the framing checks, below
    }
    return err;
}
```

Three things distinguish this from the [block](./block.md) pattern's resident burst:

- **No running pointer.** A stream self-sequences — each lane call advances it — so unlike the
  `m_axi` [complex](./complex.md) loop you never compute a word address.
- **The tail lane is short.** `lane_count = min(nrem, pf)`; the `UNROLL`-ed compute guards `k <
  lane_count` so the partial final lane doesn't process junk.
- **`TLAST` framing.** The read returns a `tlast_status`; the write asserts `TLAST` on the last lane,
  and also on the lane where an early input `TLAST` ends the burst, so the output burst is always
  closed. A plain FIFO with no framing uses `read_stream_lane` / `write_stream_lane` instead (no
  `TLAST` argument).

## Framing is validated, not assumed

Because the stream carries framing, the hook **checks** it and returns an error status rather than
trusting the producer.  `TLAST` arriving before the last sample word, or not arriving on it, each map
to a `PolyError`, checked word by word inside the loop:

```cpp
if (in_tlast && !final_word) {             // TLAST early: the burst was closed above;
    err = code(PolyError::TLAST_EARLY_SAMP_IN);
    break;                                 // read nothing more
}
if (final_word && !in_tlast) {             // the last sample word had no TLAST
    err = code(PolyError::NO_TLAST_SAMP_IN);
}
```

The caller turns a non-zero code into the status registers and returns at once, without draining
the input -- the example's [contract](../../examples/stream_inband/index.md#the-contract) (rules 6
and 7) and [why it drains nothing](../../examples/stream_inband/why_not.md#draining-to-tlast-on-an-error).

## The hook is a `.tpp`

The body is a **function template** over the stream widths (`in_bw`, `out_bw`) — its
`hls::stream<axi4s_word<in_bw>>&` arguments carry those `HwParam` widths. A template definition must
be visible at the include site, so the hook lives in a `.tpp` the generated header includes (see
[the `.cpp` vs `.tpp` rule](./writing.md#cpp-vs-tpp) and
[templating](../comp_codegen/templating.md)). Helpers it calls — `eval_poly_horner` — are
`static inline` so multiple translation units including the `.tpp` don't trip ODR.

## When to use it

Reach for the stream pattern when:

- the operand **arrives incrementally** and shouldn't (or can't) be fully materialized;
- you want **one beat per `pf` elements** of throughput, pipelined;
- the protocol carries **framing** (`TLAST`) you need to honor and validate.

If the operand is a bounded resident block, the [block](./block.md) pattern is simpler. If it lives
in memory at a runtime-dependent address, drive the `m_axi` port from the datapath
([complex](./complex.md)).

## See also

- [Kernel transfer reference](./reference.md) — the stream and memory lane/slice calls in one place.
- [Writing a hook](./writing.md) — the hook contract and the `.cpp` vs `.tpp` rule.
- [`examples/stream_inband`](../../../examples/stream_inband/poly_body_impl.tpp) — the worked stream example.
