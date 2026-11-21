# fft1024 and fft256 -- the library's `SsrFft` (`waveflow.dsp.ssr_fft`)

Nothing of either is authored here: `SsrFft(L=1024)` and `SsrFft(L=256)`, input `ap_fixed<16,2>`,
twiddles `<18,2>`, `reorder="pingpong"` (the configuration whose resources are measured), forward,
unscaled (`NO_SCALING`: out `<27,13>` and `<25,11>`).  Their bit-exact model
(`waveflow.dsp.ssr_fft.model.ssr_fft`) is the golden's steps 1 and 4.

| | interval | first-frame latency | DSP | BRAM18 | LUT | FF | source |
|---|---|---|---|---|---|---|---|
| L = 1024 | 256 cycles | 1,276 | 48 | 50 | 21,400 | 24,713 | `dsp/ssr_fft/index.md`, `examples/ssr_fft/resource.md` (RTL / csynth, xczu48dr, 250 MHz, Vitis 2025.1) |
| L = 256 | 64 cycles | 350 | 36 | 14 | 15,005 | 13,335 | same |

Ports: one `RadixWord` stream each way (4 complex samples a beat), frames by count, no TLAST -- so the
stages beside them send and take exactly `L/4` words a frame, and the job's identity travels beside
the FFT on a framed link (cmd, cor).  The forward FFT computes the correlation (`conj` of the IDFT of
`Y conj(R)` has the same magnitude at the same index), so the inverse is not needed -- decision 7.

Inside `PrachSearcher` the FFTs are children like any other; `composite_top_spec` flattens their 17
and 13 tasks into the top (`gen/prach_searcher.cpp`, 34 tasks).  The guide calls a nested `SsrFft`
"not yet exercised" at csynth (`dsp/ssr_fft/build.md`): this design is that first test.
