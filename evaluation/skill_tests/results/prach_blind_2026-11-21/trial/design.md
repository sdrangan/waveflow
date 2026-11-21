# Design notes -- the `stream_pipeline` frame's checklist

The frame (`waveflow_get_process("stream_pipeline")`) was chosen because the searcher is one
accelerator of several stages streaming to each other, one input stream, results and a status out, no
memory and no host on a bus.  The plan behind it is `plans/` (spec, arch, eval, blocks).

**The stages and why the split is where it is.**  front | fft1024 | extract | correlate | fft256 |
detect.  The FFTs are library kernels run by count; extract must see a whole job before scaling (the
block exponent); correlate runs at another rate (one frame per root and antenna) and holds Y while the
next job streams in; detect needs a whole profile per root and answers.  Details: `plans/arch.md`.

**Where this design departs from the frame.**  The frame's references join stages with framed links
only; two of this design's links on each side of each FFT are **unframed** (the FFT's `RadixWord`
ports carry no TLAST and count words), so the job's identity travels on a framed side link around
each FFT (cmd, cor) and the stages beside an FFT move exact frame counts announced in that side link's
header (`FwdHdr.nframes`; `CorHdr.n_roots x n_ant`).  An early TLAST therefore cannot shorten an FFT
frame: front zero-fills to the count and reports the error in `FwdTrl`.

**Decided by the spec** (one line each):

- *each internal link*: cmd framed 128, depth 16 (a job's `FwdTrl` + the next `FwdHdr` + 8 root words);
  win / bins / zc / pdp unframed 128 / 216 / 128 / 200, depth 4 (consumed every cycle); y framed 128,
  depth 32 (correlate buffers Y whole, so the link need not hold a job); cor framed 128, depth 4 (one
  header a job, two jobs' worth).  Layouts: the schemas in `prach/schemas.py`.
- *the boundary messages, the operation bit-exactly, the errors and which stage detects each*:
  `plans/spec.md` sections 2-3, `prach/golden.py`; BAD_PARAM, BAD_ROOT, TLAST_EARLY and TLAST_LATE
  all detected by front, carried in `FwdTrl` -> `ExtHdr.code` -> `CorHdr.code`, reported by detect.
- *the word width and elements per word*: 128 bits; 4 complex int16, 16 root bytes, or one header /
  record a word; the FFT links at their own widths.
- *the part and the clock*: xczu48dr-ffvg1517-2-e at 4 ns (`RFSOC4X2_PART`, `RFSOC4X2_PERIOD_NS`) --
  the FFTs' measured platform.
- *the scenarios, their expected outputs, any throughput target*: `prach/scenario.py` ("test",
  "early", "badroot"), every internal link's messages included; throughput: one B4 4-antenna
  occasion every 0.5 ms (`plans/spec.md` section 5), met by the input bound of 12,290 cycles.
  Also "late", "lateroots", "latecmd" (TLAST_LATE, decision 20).
