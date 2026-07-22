"""
tests/utils/test_timing.py - unit tests for waveflow.utils.timing and the
canonical example in examples/timing/basic_timing_diagram.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless / CI-safe backend – must be set before pyplot import

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_example_module():
    """Dynamically import examples/timing/basic_timing_diagram.py."""
    example_path = (
        Path(__file__).parent.parent.parent
        / "examples" / "timing" / "basic_timing_diagram.py"
    )
    spec = importlib.util.spec_from_file_location(
        "basic_timing_diagram", example_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Tests for core timing classes
# ---------------------------------------------------------------------------

class TestSigTimingInfo:
    def test_two_level_binary(self):
        from waveflow.utils.timing import SigTimingInfo

        sig = SigTimingInfo("clk", [0, 5, 10, 15], ['0', '1', '0', '1'])
        assert sig.two_level is True

    def test_two_level_false_when_unknown(self):
        from waveflow.utils.timing import SigTimingInfo

        sig = SigTimingInfo("bus", [0, 5, 10], ['x', '4', 'x'])
        assert sig.two_level is False

    def test_attributes_stored(self):
        from waveflow.utils.timing import SigTimingInfo

        sig = SigTimingInfo("a", [0, 10], ['0', '1'], is_clock=True)
        assert sig.name == "a"
        assert list(sig.times) == [0, 10]
        assert sig.values == ['0', '1']
        assert sig.is_clock is True


class TestClkSig:
    def test_period_and_ncycles(self):
        from waveflow.utils.timing import ClkSig

        clk = ClkSig(period=10, ncycles=4)
        # 4 cycles -> 8 transitions
        assert len(clk.times) == 8
        assert len(clk.values) == 8

    def test_start_rising(self):
        from waveflow.utils.timing import ClkSig

        clk = ClkSig(period=10, ncycles=2, start_rising=True)
        assert clk.values[0] == '1'

    def test_start_falling(self):
        from waveflow.utils.timing import ClkSig

        clk = ClkSig(period=10, ncycles=2, start_rising=False)
        assert clk.values[0] == '0'

    def test_clk_periods_rising_edges(self):
        from waveflow.utils.timing import ClkSig

        clk = ClkSig(period=10, ncycles=4, start_rising=True)
        edges = clk.clk_periods()
        # Rising edges at t = 0, 10, 20, 30
        assert edges == pytest.approx([0, 10, 20, 30])

    def test_is_clock_flag(self):
        from waveflow.utils.timing import ClkSig

        clk = ClkSig()
        assert clk.is_clock is True


class TestTimingDiagram:
    def _build_diagram(self):
        from waveflow.utils.timing import ClkSig, SigTimingInfo, TimingDiagram

        clk = ClkSig(period=10, ncycles=4)
        sig = SigTimingInfo("x", [0, 5, 15, 25], ['x', '1', '0', 'x'])
        td = TimingDiagram()
        td.add_signal(clk)
        td.add_signal(sig)
        return td

    def test_add_signal_stores_by_name(self):
        td = self._build_diagram()
        assert 'clk' in td.sig_info
        assert 'x' in td.sig_info

    def test_add_signals_multiple(self):
        from waveflow.utils.timing import SigTimingInfo, TimingDiagram

        td = TimingDiagram()
        sigs = [
            SigTimingInfo("a", [0, 5], ['0', '1']),
            SigTimingInfo("b", [0, 5], ['1', '0']),
        ]
        td.add_signals(sigs)
        assert set(td.sig_info.keys()) == {'a', 'b'}

    def test_plot_signals_returns_axes(self):
        import matplotlib.pyplot as plt
        from waveflow.utils.timing import ClkSig, TimingDiagram

        td = TimingDiagram()
        td.add_signal(ClkSig(period=10, ncycles=4))
        ax = td.plot_signals()
        assert ax is not None
        plt.close("all")

    def test_plot_signals_no_crash_with_trange(self):
        import matplotlib.pyplot as plt

        td = self._build_diagram()
        ax = td.plot_signals(trange=[0, 40])
        assert ax is not None
        plt.close("all")


class TestActivityDiagram:
    """The reusable activity-band renderer: a sibling of TimingDiagram.

    These exercise the whole surface without a waveform -- lanes are plain ``(label, events,
    colour)`` triples -- which is the point of the class: any design with per-cycle event lists can
    draw these views, trace or not.
    """

    def _lanes(self):
        import numpy as np
        return [
            ("in",   np.array([2, 3, 4, 20, 21]), "#4C78A8"),
            ("mid",  np.array([10]),              "#E45756"),  # 1-cycle run
            ("out",  np.array([]),                "#54A24B"),  # idle lane
        ]

    def test_runs_collapse_and_gap(self):
        from waveflow.utils.timing import ActivityDiagram

        # 1..4 contiguous, then a gap of 6 (> default 3) opens a new run; 20,21 merge.
        runs = ActivityDiagram.runs([1, 2, 3, 4, 10, 20, 21])
        assert runs == [(1, 3), (10, 1), (20, 1)]

    def test_runs_gap_parameter_merges(self):
        from waveflow.utils.timing import ActivityDiagram

        # With a wider gap, the 10 folds into the first run.
        assert ActivityDiagram.runs([1, 2, 3, 4, 10], gap=6) == [(1, 9)]

    def test_runs_empty(self):
        import numpy as np
        from waveflow.utils.timing import ActivityDiagram
        assert ActivityDiagram.runs(np.array([])) == []

    def test_band_mode_axes_and_limits(self):
        import matplotlib.pyplot as plt
        from waveflow.utils.timing import ActivityDiagram

        ad = ActivityDiagram(self._lanes())
        fig, ax, ax2 = ad.plot(mode="band", trange=(0, 100), title="t")
        assert ax2 is None                       # no occupancy panel
        assert ax.get_xlim() == (0.0, 100.0)
        # Labels are drawn top-to-bottom, so the ytick labels are the lanes reversed.
        assert [t.get_text() for t in ax.get_yticklabels()] == ["out", "mid", "in"]
        plt.close(fig)

    def test_band_mode_default_hi(self):
        import matplotlib.pyplot as plt
        from waveflow.utils.timing import ActivityDiagram

        ad = ActivityDiagram(self._lanes())
        fig, ax, _ = ad.plot(mode="band")        # no trange -> max_event + 40
        assert ax.get_xlim() == (0.0, 21 + 40)
        plt.close(fig)

    def test_beat_mode_requires_trange(self):
        from waveflow.utils.timing import ActivityDiagram

        ad = ActivityDiagram(self._lanes())
        with pytest.raises(ValueError, match="trange"):
            ad.plot(mode="beat")

    def test_occupancy_panel_present(self):
        import matplotlib.pyplot as plt
        import numpy as np
        from waveflow.utils.timing import ActivityDiagram

        ad = ActivityDiagram(self._lanes())
        level = np.zeros(60, dtype=int)
        level[10:20] = 2
        ret = ad.set_occupancy(level, 2, colour="#F58518", ylabel="q")
        assert ret is ad                          # chainable
        fig, ax, ax2 = ad.plot(mode="beat", trange=(0, 50))
        assert ax2 is not None                    # occupancy sub-panel drawn
        assert ax2.get_xlim() == (0.0, 50.0)
        assert ax2.get_ylabel() == "q"
        plt.close(fig)

    def test_occupancy_none_still_draws_panel(self):
        import matplotlib.pyplot as plt
        from waveflow.utils.timing import ActivityDiagram

        # A design that exposes no counters keeps the (empty) panel rather than changing shape.
        ad = ActivityDiagram(self._lanes()).set_occupancy(None, 0, colour="#F58518")
        fig, ax, ax2 = ad.plot(mode="beat", trange=(0, 30))
        assert ax2 is not None
        plt.close(fig)

    def test_bad_mode_raises(self):
        from waveflow.utils.timing import ActivityDiagram
        with pytest.raises(ValueError, match="mode"):
            ActivityDiagram(self._lanes()).plot(mode="bogus")

    def test_from_trace_walks_spec(self):
        """from_trace turns a port/channel spec into event lanes, in order, via the trace's own
        handshake reader."""
        import numpy as np
        from waveflow.utils.timing import ActivityDiagram

        class FakeTrace:
            manifest = {"channels": [
                {"id": "cmd",
                 "write": {"write": "cmd_w", "full_n": "cmd_fn"},
                 "read": {"empty_n": "cmd_en", "read": "cmd_r"}},
            ]}

            def port(self, pid):
                return {"signals": {"tvalid": f"{pid}_v", "tready": f"{pid}_r"}}

            def _handshakes(self, a, b):
                # Encode which signals were asked for, so the test can assert the wiring.
                return np.array([hash((a, b)) % 1000])

        bt = FakeTrace()
        spec = [
            ("cmd_in", ("port", "s_cmd", "tvalid", "tready"), "#111"),
            ("cmd_wr", ("chan", "cmd", "write"),              "#222"),
            ("cmd_rd", ("chan", "cmd", "read"),               "#333"),
        ]
        ad = ActivityDiagram.from_trace(bt, spec)
        assert [lbl for lbl, _, _ in ad.lanes] == ["cmd_in", "cmd_wr", "cmd_rd"]
        assert [c for _, _, c in ad.lanes] == ["#111", "#222", "#333"]
        # The write side reads (write, full_n); the read side reads (empty_n, read).
        assert ad.lanes[0][1][0] == hash(("s_cmd_v", "s_cmd_r")) % 1000
        assert ad.lanes[1][1][0] == hash(("cmd_w", "cmd_fn")) % 1000
        assert ad.lanes[2][1][0] == hash(("cmd_en", "cmd_r")) % 1000

    def test_from_trace_rejects_bad_side(self):
        from waveflow.utils.timing import ActivityDiagram

        class FakeTrace:
            manifest = {"channels": [{"id": "cmd", "write": {}, "read": {}}]}

            def _handshakes(self, a, b):
                return []

        with pytest.raises(ValueError, match="side"):
            ActivityDiagram.from_trace(FakeTrace(), [("x", ("chan", "cmd", "sideways"), "#000")])


# ---------------------------------------------------------------------------
# Smoke test: example figure generation
# ---------------------------------------------------------------------------

def test_save_timing_figures_creates_png(tmp_path):
    """Import save_timing_figures from the example script and verify output."""
    mod = _load_example_module()
    saved = mod.save_timing_figures(tmp_path)

    assert len(saved) >= 1, "Expected at least one output file"
    for p in saved:
        p = Path(p)
        assert p.exists(), f"Expected output file not found: {p}"
        assert p.stat().st_size > 0, f"Output file is empty: {p}"
        assert p.suffix == ".png", f"Expected PNG, got: {p.suffix}"