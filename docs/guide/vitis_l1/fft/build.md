---
title: Including the FFT in your design
parent: The Vitis FFT
grand_parent: Vitis L1 Blocks
nav_order: 3
audience: hls
api: [VitisFft, VitisL1Step, vitis_fft_include_dir, composite_top_spec, render_tcl, render_rtl_f]
summary: "The build steps that put a VitisFft into a synthesized design, and where every file goes: the body copied from waveflow/build into your include/ by VitisL1Step, the vendor headers left in the Vitis install and reached by an include path, the generated top and its tcl in gen/, the RTL file list and ROM data in xsi/. Covers the FFT as a design's top and as a child of your own composite, and the reference build in waveflow/vitis_l1/rtl.py."
---

# Including it in your design

`VitisFft` is synthesized like any free-running module -- the generator writes an `ap_ctrl_none` top
with one `hls::task` per child -- with two differences: its task body is a **copied** file, not a
generated one, and it needs the **vendor headers**, which stay in the Vitis install.

## Where every file goes

For a design rooted at `<root>` (the layout of `plans/source_layout.md`: authored files tracked,
generated directories deletable):

| file | comes from | lands in | how |
|---|---|---|---|
| `vitis_fft_task.h` | `waveflow/build/vitis_fft_task.h` (framework) | `<root>/include/` | **copied** verbatim by `VitisL1Step` |
| `vitis_fft/hls_ssr_fft.hpp` and the rest of the vendor FFT | the Vitis install, `tps/xf_dsp/L1/include/hw/vitis_fft/fixed/` | **not copied** | `-I` path in the csynth script |
| `memmgr.hpp` | `waveflow/build/` | `<root>/include/` | `MemMgrStep` (every generated top includes it) |
| your top, `<top>.cpp` | generated from your module graph | `<root>/gen/` | `composite_top_spec` + `render_top` |
| `<top>.tcl` | generated | `<root>/gen/` | `render_tcl(..., include_dirs=(vendor dir,))` |
| `rtl_<top>.f`, ROM `.dat` files | csynth's output | `<root>/xsi/` | `render_rtl_f`, after csynth |

Nothing of the FFT's is authored in your design: `include/`, `gen/` and `xsi/` are build output, and
deleting them is a clean build.

## The steps

**1. Headers.** Add the two copy steps to your build DAG, beside your schema headers:

```python
from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.composite_gen import INCLUDE_DIR
from waveflow.build.streamutils import MemMgrStep
from waveflow.build.vitis_l1_step import VitisL1Step

dag = BuildDag()
dag.add(MemMgrStep(output_dir=INCLUDE_DIR))     # include/memmgr.hpp
dag.add(VitisL1Step(output_dir=INCLUDE_DIR))    # include/vitis_fft_task.h, copied verbatim
dag.run(BuildConfig(root_dir=root, params={}), force=True)
```

**2. The vendor include path.** `vitis_fft_include_dir()` finds the vendor headers: an explicit
argument first, then `$WF_VITIS_LIBS`, then the Vitis install itself. It raises with the paths it
tried rather than handing csynth a path that fails later.

**3. The top and its script.** Generate the top from your module graph, and give the script the
vendor path:

```python
from waveflow.build.composite_gen import composite_top_spec, render_tcl, render_top, tcl_path
from waveflow.build.composite_gen import RFSOC4X2_PART, RFSOC4X2_PERIOD_NS
from waveflow.build.vitis_l1_step import vitis_fft_include_dir

spec = composite_top_spec(design, width=word_bits)          # design: your FreeRunMod
(root / "gen" / f"{spec.top_name}.cpp").write_text(render_top(spec))
tcl_path(root, spec.top_name).write_text(
    render_tcl(spec.top_name, include_dirs=(str(vitis_fft_include_dir()),),
               part=RFSOC4X2_PART, period_ns=RFSOC4X2_PERIOD_NS))
```

The include path is written with forward slashes: it sits inside a Tcl string, where a Windows
path's backslashes would be escapes.

**4. Synthesize, and list the RTL.** Run the script with `run_vitis_hls(tcl, work_dir=root)` -- pass
an **absolute** script path, since Vitis resolves a relative one from its work directory -- then
`render_rtl_f(top, root)` writes `xsi/rtl_<top>.f` and copies csynth's ROM `.dat` files beside it.
At `L >= 1024` the twiddles are a ROM: without those files XSI loads nothing and simulates with zeros,
silently.

**5. Calibrate, if this configuration is new.** A timed `VitisFft` refuses a configuration its
platform has not measured, and says which command to run. See
[Latency and II for a vendor block](timing.md).

## The FFT as your top, or inside it

**As the top** (the example does this), its eight lanes are the boundary. The output lanes are wider
than the input lanes, so tell the generator each boundary port's width:

```python
widths = {f"s_in_{j}": ep.bitwidth for j, ep in enumerate(fft.s_in)}
widths |= {f"m_out_{j}": ep.bitwidth for j, ep in enumerate(fft.m_out)}
spec = composite_top_spec(fft, width=int(fft.s_in[0].bitwidth), port_widths=widths)
```

**Inside your own composite**, it is one child. Lanes between the FFT and its neighbours are internal
channels, whose widths the generator reads off their `StreamIF`s; only lanes that cross *your*
boundary need `port_widths`, and only if they differ from the design width. A sketch:

<!-- snippet: skip -->
```python
@dataclass
class Spectrum(FreeRunMod):
    def __post_init__(self):
        super().__post_init__()
        self.fft = VitisFft(name="fft", sim=self.sim, clk=self.clk, L=256, platform_dir=...)
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

`waveflow/vitis_l1/rtl.py` is the whole flow for a `VitisFft` top, in one place: `generate` (steps
1-3, plus the XSI workspace), `synth` (step 4), `run_xsi`, the port-timing readers, and
`record_resources`, which files the synthesis report onto the platform. `examples/vitis_fft` and the
calibration fixture both build through it; a design of your own copies its shape.
