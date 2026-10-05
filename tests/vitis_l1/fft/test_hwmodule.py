"""S1 gate — ``VitisFft`` as a pysim ``HwModule`` reproduces the vendor goldens.

The model is already gated against the library elsewhere in this directory.  What *this* file
gates is the **wrapper**: that a real simulation, driving ``R`` AXI-Stream ports and collecting
``R`` more, lands the same stored integers the vendor produced — so the port group, the
``n % R`` lane layout, the packing and the derived output width are all right, not just the
arithmetic.

No toolchain needed, so this stays in the fast suite.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.hw_module import HwModule
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.simulation.simulation import Simulation
from waveflow.vitis_l1.hw import (
    OutputOrder,
    ScalingMode,
    VitisFft,
    _pack_complex,
    _unpack_complex,
)

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def _sgn(bits, w: int) -> np.ndarray:
    """Raw stored bits -> signed stored integers, as every other gate here does."""
    v = np.asarray(bits, dtype=np.int64)
    return np.where(v & (1 << (w - 1)), v - (1 << w), v)


@dataclass
class _Lanes(HwModule):
    """A test-local peer for an ``R``-wide port group: ``R`` masters, or ``R`` slaves.

    Deliberately dumb — it plays words and records words.  Anything cleverer here would risk
    testing the harness instead of the module.
    """

    n_lanes: int = 4
    bitwidth: int = 32
    as_master: bool = True
    #: frames to play, each ``[lane][word]``; the sink fills this instead.
    frames: list = field(default_factory=list)
    #: sink only — sim time at which each frame finished arriving.  This is what makes II and
    #: latency observable; without it a timing gate could only check that bits still match.
    stamps: list = field(default_factory=list)
    words_per_lane: int = 4
    n_frames: int = 1
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        cls = StreamIFMaster if self.as_master else StreamIFSlave
        self.eps = [cls(sim=self.sim, name=f"{self.name}_{i}", bitwidth=self.bitwidth,
                        has_tlast=True) for i in range(self.n_lanes)]
        for ep in self.eps:
            self.add_endpoint(ep)

    def run_proc(self):
        # Every lane moves concurrently, because they are parallel ports.  A harness that walked
        # them in turn would need R x (L/R) cycles per frame and would cap throughput itself, so
        # any II measured through it would be the testbench's, not the module's.  Measured while
        # building this: a serialized harness reported II = 16 where the module offered 8.
        if self.as_master:
            for frame in self.frames:
                def wr(ep, words):
                    yield from ep.write(np.asarray(words, dtype=np.uint64))
                yield self.env.all_of([self.env.process(wr(e, w))
                                       for e, w in zip(self.eps, frame)])
            return
        for _ in range(self.n_frames):
            got: dict[int, np.ndarray] = {}

            def rd(j, ep):
                have = []
                while sum(w.size for w in have) < self.words_per_lane:
                    part = yield from ep.get(
                        nwords_max=self.words_per_lane - sum(w.size for w in have))
                    have.append(np.asarray(part).reshape(-1))
                got[j] = np.concatenate(have)

            yield self.env.all_of([self.env.process(rd(j, e)) for j, e in enumerate(self.eps)])
            self.frames.append([got[j] for j in range(len(self.eps))])
            self.stamps.append(self.now)          # when this frame finished arriving


def _run_pysim(length: int, vectors: list[tuple[np.ndarray, np.ndarray]],
               scaling_mode: ScalingMode = ScalingMode.NO_SCALING, **timing):
    """Drive every vector through one ``VitisFft`` and return ``[(re, im), ...]`` per vector.

    Inputs and outputs are interleaved across lanes exactly as the goldens' ``stream_layout``
    says: sample ``n`` on lane ``n % R`` at time ``n // R``.
    """
    sim, clk = Simulation(), Clock(freq=100e6)
    dut = VitisFft(name="dut", sim=sim, clk=clk, L=length, scaling_mode=scaling_mode, **timing)
    r, per_lane = int(dut.R), length // int(dut.R)
    in_w, out_w = int(dut.in_w), int(dut.out_fmt.W)

    frames = [[_pack_complex(x_re[j::r], x_im[j::r], in_w) for j in range(r)]
              for x_re, x_im in vectors]
    drv = _Lanes(name="drv", sim=sim, clk=clk, n_lanes=r, bitwidth=in_w * 2,
                 as_master=True, frames=frames)
    snk = _Lanes(name="snk", sim=sim, clk=clk, n_lanes=r, bitwidth=out_w * 2,
                 as_master=False, words_per_lane=per_lane, n_frames=len(vectors))

    for j in range(r):
        ch = StreamIF(name=f"in_{j}", sim=sim, clk=clk, bitwidth=in_w * 2)
        ch.bind("master", drv.eps[j])
        ch.bind("slave", dut.s_in[j])
        ch2 = StreamIF(name=f"out_{j}", sim=sim, clk=clk, bitwidth=out_w * 2)
        ch2.bind("master", dut.m_out[j])
        ch2.bind("slave", snk.eps[j])
    sim.run_sim()

    assert len(snk.frames) == len(vectors), (
        f"collected {len(snk.frames)} frame(s) for {len(vectors)} vector(s) — the simulation "
        f"ended early, which would make every comparison below vacuous")
    out = []
    for lanes in snk.frames:
        y_re = np.zeros(length, dtype=np.int64)
        y_im = np.zeros(length, dtype=np.int64)
        for j, words in enumerate(lanes):
            y_re[j::r], y_im[j::r] = _unpack_complex(words, out_w)
        out.append((y_re, y_im))
    return out, snk.stamps, dut


def _golden_case(name: str):
    g = json.loads((GOLDEN_DIR / name).read_text())
    vectors = [(_sgn([e["re"] for e in v["input"]], g["in_W"]),
                _sgn([e["im"] for e in v["input"]], g["in_W"])) for v in g["vectors"]]
    want = [(np.array([e["re"] for e in v["output"]], dtype=np.int64),
             np.array([e["im"] for e in v["output"]], dtype=np.int64)) for v in g["vectors"]]
    return g, vectors, want


def _compare(got, want, out_w: int) -> int:
    mask = (1 << out_w) - 1
    bad = 0
    for (g_re, g_im), (w_re, w_im) in zip(got, want):
        bad += int(((g_re & mask) != w_re).sum() + ((g_im & mask) != w_im).sum())
    return bad


# -- the gate ------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,length", [
    ("fft_L16_R4_noscale_natural.json", 16),
    ("fft_L64_R4_noscale_natural.json", 64),
])
def test_hwmodule_pysim_matches_the_vendor_golden(name, length):
    """A simulated ``VitisFft`` lands the vendor's stored integers, every vector, both parts."""
    g, vectors, want = _golden_case(name)
    assert g["L"] == length and g["R"] == 4
    # Only the richer golden records the layout; the L=64 file carries metadata alone.  Checking
    # it when present guards against the vendor convention changing under us — but the real proof
    # is the comparison below, which a wrong interleave could not pass.
    if "stream_layout" in g:
        assert g["stream_layout"] == "sample n -> stream (n % R) at time (n / R)", (
            "the golden's lane layout changed; _run_pysim's interleave encodes the old one")
    got, _, _ = _run_pysim(length, vectors)
    bad = _compare(got, want, g["out_W"])
    assert bad == 0, f"L={length}: {bad} of {2 * len(vectors) * length} stored integers differ"


def test_hwmodule_pysim_matches_the_cosim_output_at_L1024():
    """The same wrapper against **synthesized hardware** output, not another golden.

    ``verifyFFT1024`` is not checked in as a golden, so this skips when it has not been run —
    the same contract ``test_general.py`` uses for the same files.
    """
    d = Path(__file__).resolve().parent / "verifyFFT1024"
    if not (d / "results" / "output_cosim.txt").exists():
        pytest.skip("run ../verifyFFT1024 first (see its README)")

    def rows(p):
        return [ln.split() for ln in p.read_text().splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]

    ni = rows(d / "data" / "input.txt")
    n_vec, n_samp = int(ni[0][0]), int(ni[0][1])
    ir = np.array([int(a) for a, _ in ni[1:]], dtype=np.int64).reshape(n_vec, n_samp)
    ii = np.array([int(b) for _, b in ni[1:]], dtype=np.int64).reshape(n_vec, n_samp)
    vectors = [(_sgn(ir[v], 16), _sgn(ii[v], 16)) for v in range(n_vec)]

    got, _, _ = _run_pysim(n_samp, vectors)
    no = rows(d / "results" / "output_cosim.txt")
    out_w = int(no[0][0])
    gr = np.array([int(a) for a, _ in no[1:]], dtype=np.int64).reshape(n_vec, n_samp)
    gi = np.array([int(b) for _, b in no[1:]], dtype=np.int64).reshape(n_vec, n_samp)
    bad = _compare(got, list(zip(gr, gi)), out_w)
    assert bad == 0, f"L={n_samp} vs cosim: {bad} of {2 * n_vec * n_samp} differ"


# -- the interface itself --------------------------------------------------------------------
@pytest.mark.parametrize("length,out_w", [(16, 21), (64, 23), (1024, 27)])
def test_port_group_is_R_wide_and_the_output_width_is_derived(length, out_w):
    """``R`` ports per side, and the output width comes from the model, not from a constant.

    The library's own ``OUTPUT_WL = in_W + log2(L) + 1``; restating it here would let the two
    drift, so the module derives it and this pins the result.
    """
    m = VitisFft(name="m", sim=Simulation(), clk=Clock(freq=100e6), L=length)
    assert len(m.s_in) == len(m.m_out) == int(m.R) == 4
    assert [m.s_in[i] for i in range(4)] == [getattr(m, f"s_in_{i}") for i in range(4)]
    assert len(m.endpoints) == 8
    # OUTPUT_WL = in_W + log2(L) + 1, and log2(L) == 2*S for R=4.
    assert m.out_fmt.W == out_w == int(m.in_w) + 2 * int(m.n_stages) + 1
    assert m.s_in[0].bitwidth == 2 * int(m.in_w)
    assert m.m_out[0].bitwidth == 2 * out_w


def test_all_three_scaling_modes_are_available_at_L16():
    """``fft16`` validates all three, so the wrapper must not refuse them — and ``SCALE`` keeps
    the width fixed, which is the visible difference."""
    widths = {}
    for sm in ScalingMode:
        m = VitisFft(name="m", sim=Simulation(), clk=Clock(freq=100e6), L=16, scaling_mode=sm)
        widths[sm.name] = m.out_fmt.W
    assert widths["NO_SCALING"] == widths["GROW_TO_MAX_WIDTH"] == 21
    assert widths["SCALE"] == 16, "SCALE holds the width; if this moved, the dispatch is wrong"


@pytest.mark.parametrize("kwargs,match", [
    ({"R": 8}, "only R=4"),
    ({"output_order": OutputOrder.DIGIT_REVERSED_TRANSPOSED}, "NATURAL"),
    ({"L": 1024, "scaling_mode": ScalingMode.SCALE}, "L=16 only"),
])
def test_refuses_what_the_model_does_not_cover(kwargs, match):
    """A loud refusal is the feature: silently approximating would produce confident wrong bits."""
    with pytest.raises(NotImplementedError, match=match):
        VitisFft(name="m", sim=Simulation(), clk=Clock(freq=100e6), **kwargs)


def test_refuses_a_length_that_is_not_a_power_of_the_radix():
    """32, 128 and 512 take the library's forked architecture, which is not modelled."""
    with pytest.raises(ValueError, match="not a power"):
        VitisFft(name="m", sim=Simulation(), clk=Clock(freq=100e6), L=32)


def test_scaling_mode_stays_an_enum_rather_than_becoming_an_int():
    """It is a plain field, not an ``HwParam``, precisely so the name survives for ``run_iter``.

    ``HwModule.__post_init__`` rewrites every ``HwParam`` as ``HwParamValue(int(value))``; if
    ``scaling_mode`` were one, this attribute would be a bare int and the model dispatch would
    fail to find its mode.  The ``HwParam``s are checked alongside, so the contrast is explicit.
    """
    m = VitisFft(name="m", sim=Simulation(), clk=Clock(freq=100e6), L=16,
                 scaling_mode=ScalingMode.SCALE)
    assert isinstance(m.scaling_mode, ScalingMode)
    assert isinstance(m.output_order, OutputOrder)
    assert int(m.L) == 16 and int(m.R) == 4


def test_kernel_task_hands_the_body_over_with_every_port_named():
    """S2's hook. The signature must name endpoints by **attribute**, which ``s_in[0]`` is not."""
    m = VitisFft(name="m", sim=Simulation(), clk=Clock(freq=100e6), L=1024)
    kt = m.kernel_task()
    assert kt.signature == ("s_in_0", "s_in_1", "s_in_2", "s_in_3",
                            "m_out_0", "m_out_1", "m_out_2", "m_out_3")
    assert all(hasattr(m, nm) for nm in kt.signature)
    assert kt.template_args == (1024, 4, 16, 2, 18, 2, 0, 0, 27)   # ..., OUT_W
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in kt.template_args), (
        "template_args is typed tuple[int, ...] and feeds the task instance name; an IntEnum "
        "member would leak its repr into generated C++")


# -- S6: latency and II --------------------------------------------------------------------------
#
# The numbers below are *declared*, not measured — S3's RTL gate is what replaces them with
# measurements.  What these gates check is that the module expresses latency and II as two
# independent quantities, which a single sequential process cannot do.
_LAT, _II = 41, 8           # II deliberately > L/R = 4, so the module paces, not the ports


def _timed_run(length, vectors, **kw):
    kw = {"latency_cycles": _LAT, "ii_cycles": _II, **kw}
    out, stamps, dut = _run_pysim(length, vectors, **kw)
    return out, [t / dut.clk.period for t in stamps], dut


def test_timing_is_opt_in_and_the_pair_is_required():
    """Either number alone is a plausible-looking wrong model, so neither is accepted alone."""
    plain = VitisFft(name="m", sim=Simulation(), clk=Clock(freq=100e6), L=16)
    assert plain._timed is False, "timing must be off unless asked for"

    for kw in ({"latency_cycles": 41}, {"ii_cycles": 8}):
        with pytest.raises(ValueError, match="one pair"):
            VitisFft(name="m", sim=Simulation(), clk=Clock(freq=100e6), L=16, **kw)
    with pytest.raises(ValueError, match="max_inflight only means something"):
        VitisFft(name="m", sim=Simulation(), clk=Clock(freq=100e6), L=16, max_inflight=4)
    with pytest.raises(ValueError, match="ii_cycles > 0"):
        VitisFft(name="m", sim=Simulation(), clk=Clock(freq=100e6), L=16,
                 latency_cycles=10, ii_cycles=0)


def test_frames_in_flight_are_bounded_by_latency_over_II():
    """The queue capacity is the model's third number, and it is not cosmetic.

    Unbounded, the module would accept frames forever while its consumer is blocked, which no
    hardware does. A finite queue plus explicit ready-times is also the *paced* form of a
    free-running chain, which is what keeps this clear of the recorded deadlock.
    """
    m = VitisFft(name="m", sim=Simulation(), clk=Clock(freq=100e6), L=16,
                 latency_cycles=_LAT, ii_cycles=_II)
    assert m.max_inflight == -(-_LAT // _II) == 6
    assert m._inflight.capacity == 6 and m._inflight.capacity != float("inf")


def test_two_frames_back_to_back_are_II_paced_not_serialized():
    """⚠️ The gate this stage exists for.

    A single sequential ``run_iter`` is read-frame → delay → write-frame, so frame *k+1* waits for
    frame *k* to be written and II collapses into latency. **One frame cannot tell a correct II
    from a serialized one** — only back-to-back frames can, which is why this drives three.
    """
    rng = np.random.default_rng(7)
    vectors = [(rng.integers(-2000, 2000, 16), rng.integers(-2000, 2000, 16)) for _ in range(3)]
    _, cycles, _ = _timed_run(16, vectors)
    deltas = [round(cycles[i + 1] - cycles[i], 6) for i in range(len(cycles) - 1)]

    assert deltas == [float(_II)] * len(deltas), (
        f"frames completed {deltas} cycles apart; the declared II is {_II}")
    assert _II != _LAT, "pick II != latency or this gate cannot distinguish them"
    assert all(d != _LAT for d in deltas), (
        f"frames are {_LAT} cycles apart — that is the LATENCY, so the model has serialized and "
        f"II has collapsed into it. This is the failure the two-process design prevents.")


def test_latency_is_observable_on_the_first_frame():
    """Latency shows up once, on the first frame: read it in, wait, write it out.

    With the lanes concurrent a frame costs ``L/R`` cycles to move, so the first output lands at
    ``L/R + latency + L/R``. That it is *not* also paid per frame is what the II gate above shows.
    """
    rng = np.random.default_rng(11)
    vectors = [(rng.integers(-2000, 2000, 16), rng.integers(-2000, 2000, 16)) for _ in range(2)]
    _, cycles, dut = _timed_run(16, vectors)
    per_lane = 16 // int(dut.R)
    assert round(cycles[0], 6) == per_lane + _LAT + per_lane
    untimed_cycles = _run_pysim(16, vectors)[1]
    assert round(untimed_cycles[0] / dut.clk.period, 6) == per_lane + per_lane, (
        "the untimed module must carry no latency at all — otherwise 'opt-in' is not true")


def test_timing_changes_when_data_appears_and_never_what_it_is():
    """The bits are computed once, at intake; the emit side only releases them."""
    g, vectors, want = _golden_case("fft_L16_R4_noscale_natural.json")
    timed, _, _ = _timed_run(16, vectors)
    assert _compare(timed, want, g["out_W"]) == 0, "timing perturbed the arithmetic"


def test_cycles_seed_is_offered_as_a_seed_not_a_measurement():
    """``cycles_seed`` gives the plan's ``II ~ L/R`` estimate and deliberately no latency."""
    seed = VitisFft.cycles_seed(1024)
    assert seed == {"ii_cycles": 256}
    assert "latency_cycles" not in seed, (
        "a latency nobody measured would be the one number most likely to be believed")
