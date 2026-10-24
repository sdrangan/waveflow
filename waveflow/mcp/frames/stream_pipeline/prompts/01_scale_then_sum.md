# Kernel spec: `scale_sum`, scale then sum

Frame: `stream_pipeline` -- read [frame.md](../frame.md) first; this file adds only what is specific to
`scale_sum`.

## 1. Overview

A command carries a gain `a` (int16) and a block size `K`, and is followed by `n` int16 samples (`n` a
multiple of `K`).  Stage 1 scales each sample, `y = (a x + 128) >> 8`, saturated to int16; stage 2 sums each
block of `K` scaled samples and writes one int32 per block.  Stage 1 runs a sample a cycle; stage 2 writes
a result every `K`.

## 2. Messages

- **Command** (`s_in`, one burst): `tx_id` (uint16), `n` (uint16), `K` (uint16, 1 to 64), `a` (int16).
- **Samples** (`s_in`, one burst): `n` int16, two a 32-bit word.
- **Results** (`m_out`): `Resp(tx_id, nblk)` (one burst), then `n / K` int32 sums (one burst).
- **Status** (`m_status`): `tx_id`, `status` (uint16: 0 `OK`, 1 `TLAST_EARLY`).

## 3. Acceptance

- Golden with worked examples; each stage alone in pysim, then the composite, bit-exact.
- Each stage and the composite through the system DAG to `compare`.
