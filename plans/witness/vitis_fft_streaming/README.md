# Witness: the Vitis SSR FFT's frame interval, connected and patched four ways

Evidence for [`plans/ssr_fft.md`](../../ssr_fft.md) F0.  `L = 1024`, `R = 4`, input
`complex<ap_fixed<16,2>>`, twiddles `<18,2>`, `SSR_FFT_NO_SCALING`, natural order, part
`xczu48dr-ffvg1517-2-e` (RFSoC 4x2) at 4 ns, Vitis HLS 2025.1.  The testbench queues 16 frames and
calls the top 16 times; cosim reports per-transaction latency and interval.

| run | top | result (steady interval) |
|---|---|---|
| `run_nonstream.tcl` | `fft<P>(in, out)`, the guide's "non-streaming connection" | 2,556 |
| `run_stream.tcl` | `innerFFT` in DATAFLOW between producer/consumer procs, the "streaming connection" | ~1,420 (649 / 2,191 alternating) |
| `run_deep.tcl` | + `STREAM depth = L/R` on the core's FIFOs | same |
| `run_chain.tcl` | + `ap_ctrl_chain` | same |
| `run_patch.tcl` | + `patch.py` (commutators run until one frame is out) | **878**, bit-exact vs `run_ref.tcl` |
| `run_patch2.tcl` | + `patch2.py` (flat stage loop) + `patch3.py` (DATAFLOW reorder) | RTL deadlock, 0/16 frames (csim passes) |

`run_ref.tcl` is csim only: the unpatched library's outputs, written to `out_ref.txt`, which the
patched runs compare against (`tb.cpp` dumps every output).

## Rerunning

The patch runs build against a local copy of the library, not the installed one:

```bash
mkdir lib && cp -r $XILINX/2025.1/Vitis/tps/xf_dsp/L1/include/hw/vitis_fft/fixed/vitis_fft lib/
python patch.py                      # for run_patch.tcl
python patch2.py && python patch3.py # additionally, for run_patch2.tcl
vitis-run --mode hls --tcl run_patch.tcl
```

The Tcl files and `tb.cpp` carry the absolute paths of the machine they ran on
(`C:/Xilinx/2025.1/...`, the output file paths); edit them for another machine.
