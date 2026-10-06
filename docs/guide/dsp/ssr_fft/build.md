---
title: Including the SSR FFT in your design
parent: The SSR FFT
grand_parent: DSP Blocks
nav_order: 3
audience: hls
api: [SsrFft, write_sources, composite_top_spec, render_tcl, render_rtl_f]
summary: "The build steps that put an SsrFft into a synthesized design, and where every file goes: the generic task bodies copied from waveflow/dsp/ssr_fft/src, the per-configuration header and task wrappers and every edge's array utils generated into include/ by hls.write_sources, the top and its tcl in gen/, the RTL file list in xsi/. No vendor headers and no include path. Covers the FFT as a design's top and inside your own composite, the one solution setting it needs (config_rtl -reset state), and the reference build in waveflow/dsp/ssr_fft/rtl.py."
---

# Including it in your design

`SsrFft` is synthesized like any free-running composite -- the generator writes an `ap_ctrl_none`
top with one `hls::task` per child -- and, unlike `VitisFft`, needs **nothing outside the
repository**: no vendor headers, no include path.

## Where every file goes

For a design rooted at `<root>` (authored files tracked, generated directories deletable):

| file | comes from | lands in | how |
|---|---|---|---|
| `ssr_fft_tasks.h` | `waveflow/dsp/ssr_fft/src/` (framework, hand-written) | `<root>/include/` | **copied** by `hls.write_sources` |
| `ssr_fft_<config>.h` | generated from `Geometry` | `<root>/include/` | edge adaptors, stage formats, twiddle ROMs |
| `ssr_fft_<config>_tasks.h` | generated | `<root>/include/` | one wrapper function per task |
| `complex__fixed<W>_<I>_array_utils.h` | generated, one per edge format | `<root>/include/` | `gen_array_utils` -- the only packing code |
| `streamutils_hls.h`, `memmgr.hpp` | `waveflow/build/` | `<root>/include/` | `StreamUtilsStep`, `MemMgrStep` |
| your top, `<top>.cpp` | your module graph | `<root>/gen/` | `composite_top_spec` + `render_top` |
| `<top>.tcl` | generated | `<root>/gen/` | `render_tcl(..., solution_config=("config_rtl -reset state",))` |
| `rtl_<top>.f` | csynth's output | `<root>/xsi/` | `render_rtl_f`, after csynth |

`<config>` is `L<L>_<in_w>_<in_i>_<tw_w>_<tw_i>`, so two FFTs of different lengths in one design have
separate headers and separately named tasks. Nothing of the FFT's is authored in your design.

## The steps

**1. Headers.** One call writes everything the task bodies include, for one configuration:

```python
from waveflow.build.composite_gen import INCLUDE_DIR
from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.streamutils import MemMgrStep
from waveflow.dsp.ssr_fft import hls

hls.write_sources(fft.geo, root, INCLUDE_DIR, reorder=fft.reorder)
dag = BuildDag()
dag.add(MemMgrStep(output_dir=INCLUDE_DIR))     # every generated top includes memmgr.hpp
dag.run(BuildConfig(root_dir=root, params={}), force=True)
```

Pass the module's own `geo` and `reorder`: the wrappers the top instantiates are generated for that
configuration, and a mismatched header is a compile error at best.

**2. The top and its script.**

```python
from waveflow.build.composite_gen import composite_top_spec, render_tcl, render_top, tcl_path
from waveflow.build.composite_gen import RFSOC4X2_PART, RFSOC4X2_PERIOD_NS

spec = composite_top_spec(design, width=word_bits)          # design: your FreeRunMod
(root / "gen" / f"{spec.top_name}.cpp").write_text(render_top(spec))
tcl_path(root, spec.top_name).write_text(
    render_tcl(spec.top_name, part=RFSOC4X2_PART, period_ns=RFSOC4X2_PERIOD_NS,
               solution_config=("config_rtl -reset state",)))
```

`config_rtl -reset state` makes the tasks' `static` state registers reset with `ap_rst`; without it
they are only power-on values. The bodies are written so that nothing advances at reset anyway
(nothing is written that was not read), but the setting costs nothing and removes the question.

**3. Synthesize, and list the RTL.** `run_vitis_hls(tcl, work_dir=root)` with an **absolute** script
path, then `render_rtl_f(top, root)` writes `xsi/rtl_<top>.f`. Build in a **short directory**: csynth
fails silently past the Windows path limit.

## The FFT as your top, or inside it

**As the top** (the reference build does this), its eight lanes are the boundary, and the output lanes
are wider than the input lanes:

```python
widths = {f"s_in_{j}": ep.bitwidth for j, ep in enumerate(fft.s_in)}
widths |= {f"m_out_{j}": ep.bitwidth for j, ep in enumerate(fft.m_out)}
spec = composite_top_spec(fft, width=int(fft.s_in[0].bitwidth), port_widths=widths)
```

**Inside your own composite**, it is one child. `hls::task` has no hierarchy, so the generator
flattens composites to their leaves (`kernel_tasks`): your top gets the FFT's tasks (18 at
`L = 1024`) beside your own. Lanes between the FFT and its neighbours are internal channels, sized
from their `StreamIF`s. A sketch -- **not yet exercised**: every build so far has the FFT as the top,
so the first design that nests it is also the first test of the flattened channel names:

<!-- snippet: skip -->
```python
@dataclass
class Spectrum(FreeRunMod):
    def __post_init__(self):
        super().__post_init__()
        self.fft = SsrFft(name="fft", sim=self.sim, clk=self.clk, L=1024, reorder="pingpong")
        self.mag = Magnitude(name="mag", sim=self.sim, clk=self.clk)   # your module
        for c in (self.fft, self.mag):
            self.add_comp(c)
        for j in range(4):                       # FFT lanes -> magnitude: internal channels
            ch = StreamIF(name=f"x_{j}", sim=self.sim, clk=self.clk,
                          bitwidth=self.fft.m_out[j].bitwidth)
            ch.bind("master", self.fft.m_out[j])
            ch.bind("slave", self.mag.s_in[j])
            self.add_if(ch)
        for j in range(4):                       # the FFT's input lanes are the design's input
            setattr(self, f"s_in_{j}", self.fft.s_in[j])
        self.m_out = self.mag.m_out
        self.boundary = [f"s_in_{j}" for j in range(4)] + ["m_out"]
```

## The reference build

`waveflow/dsp/ssr_fft/rtl.py` is the whole flow for an `SsrFft` top in one place: `generate` (steps
1-2, plus the XSI workspace and the testbench harness), `synth` (step 3), `run_xsi`, `check_bits`
(every frame against the golden) and the port-timing readers (`capture_beats`, `frame_times`), which
time the RTL from the BFMs' own capture bundles with no waveform. `examples/ssr_fft` and the RTL gate
`tests/dsp/ssr_fft/test_xsi.py` both build through it.
