"""The RX capture buffer at RTL — the four command cases through real Verilog.

``plans/adc_model.md`` staging item 3 (RX).  What xsim elaborates is the **wrapper**
(``rf_samp_buf_rx_top``): the kernel plus its ``bram_t2p`` buffer, so the testbench sees only
AXI-Stream and the converter model drives it exactly as it drives any other design.

Three things are gated here, and they fail in different ways:

* **The values**, bit-exact against the same prediction the pysim golden is checked against — so the
  two backends are compared to one statement rather than to each other.
* **``ADC_DROPPED == 0``**, with the converter attached.  This design exists to satisfy condition 3
  of the fidelity contract; a nonzero count means the ingress stalled and the point was lost.  It is
  read from a counter on the model, because loss at a converter has no protocol event to observe.
* **The memory's own assertion never fired.**  ``bram_t2p.v`` ``$error``\\ s when the reader touches
  the address the writer is writing that cycle, which is precisely the horizon logic failing.  A
  clean log is positive evidence that ``rd`` trailed ``wr`` for the whole run.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.rf_samp_buf_rx.rf_samp_buf_rx import (
    WORD_BW,
    RxResp,
    expected_capture,
    write_scenario,
)
from examples.rf_samp_buf_rx.rf_samp_buf_rx_build import RTL_FILES, TOP, WRAPPER
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, rtl_staleness, xsi_runner_cmd

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_samp_buf_rx"
#: The hand-written main: the generated one runs and dumps, this one also prints the model counters,
#: and two of this design's most important numbers live only on the models.
TB = "rf_samp_buf_rx_counters"

#: Cycle the last captured sample reached the sink.  Recorded 2026-08-15 on the first green run.
#: Exact, not a bound — it moves only if the design's timing changes, and both directions are worth a
#: human.  Most of it is the scenario waiting for the converter: command 3 cannot finish before
#: sample 3899 has been produced, which at 64 MSPS on a 300 MHz fabric is ~18300 cycles.
#: 15441, re-recorded 2026-08-18 when the RF fabric moved 300 -> 250 MHz.  The decomposition is
#: what makes it a re-record rather than a shrug: a run is part CONVERTER-paced (a fixed wall-clock
#: wait, so its cycle count scales with f_axis) and part FABRIC-paced (a fixed cycle count, which
#: does not).  Solving ``15441 = (18411 - x) * 250/300 + x`` gives ``x = 591`` cycles of
#: clock-independent fabric work -- the capture loop serving its 100 samples -- and 17820 cycles of
#: waiting for the converter.  Both halves behave exactly as they should.
WANT_LAST_CYCLE = 15441

#: Words the ADC delivers: 16 blocks x 256 samples, every one of which must be accepted.
WANT_ADC_WORDS = 4096


def _require(cond: bool, why: str) -> None:
    """Skip loudly — a silent skip on a gate this expensive reads as 'passed' in a summary line."""
    if not cond:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


def _counters(out: str) -> dict[str, int]:
    """The ``KEY=VALUE`` lines the counters main prints."""
    vals = {}
    for line in out.splitlines():
        if "=" in line and line.split("=", 1)[0].isupper():
            k, v = line.split("=", 1)
            try:
                vals[k.strip()] = int(v.strip())
            except ValueError:
                pass
    return vals


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> tuple[dict[str, int], str]:
    """One RTL run, shared by the assertions below."""
    xsi = ROOT / "xsi"
    _require((xsi / XSI_RUNNER).exists(), f"{xsi / XSI_RUNNER}")
    proj = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
    _require(proj.is_dir(), f"no csynth RTL at {proj} — run rf_samp_buf_rx_build.py --through csynth")
    # SECOND INSTANCE OF THIS CLASS: `*_proj/` is gitignored build output, and a gate that
    # compares a cycle count against RTL it did not produce reports "a real behaviour change"
    # when the truth is a stale artifact. See rtl_staleness().
    _require(rtl_staleness(ROOT, 'rf_samp_buf_rx') is None, rtl_staleness(ROOT, 'rf_samp_buf_rx') or "")
    for f in RTL_FILES:
        _require((xsi / f).is_file(), f"{xsi / f} — run rf_samp_buf_rx_build.py --through codegen_dut")

    # Regenerate the file list from the RTL actually on disk; never trust the committed .f.
    (xsi / f"rtl_{WRAPPER}.f").write_text(render_rtl_f(TOP, ROOT, extra=RTL_FILES), encoding="utf-8")
    # Force a clean elaboration of the WRAPPER: a cached snapshot proves nothing about this design.
    shutil.rmtree(xsi / "xsim.dir" / WRAPPER, ignore_errors=True)
    for stale in (f"{TB}.exe", f"{TB}.bin", f"{TB}.o"):
        (xsi / stale).unlink(missing_ok=True)
    for od in ("out", "resp"):
        shutil.rmtree(xsi / "vectors" / od, ignore_errors=True)
    write_scenario(xsi)

    r = subprocess.run(xsi_runner_cmd(WRAPPER, TB), cwd=str(xsi),
                       capture_output=True, text=True, timeout=1800)
    out = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in out, f"the RTL run did not complete cleanly:\n{out[-3000:]}"
    return _counters(out), out


def _captured(xsi: Path) -> np.ndarray:
    from waveflow.utils.burst_io import read_burst_bundle
    d = xsi / "vectors" / "out"
    if not d.is_dir():
        return np.zeros(0, dtype=np.uint64)
    return np.concatenate(read_burst_bundle(d)).astype(np.uint64)


@pytest.mark.xsi
def test_the_rtl_captures_the_predicted_windows(run):
    """All four cases, bit-exact — the same prediction the pysim golden is held to."""
    got = _captured(ROOT / "xsi")
    want, _ = expected_capture()
    assert got.size == want.size, f"RTL captured {got.size} samples, predicted {want.size}"
    if not np.array_equal(got, want):
        bad = int(np.argmax(got != want))
        raise AssertionError(
            f"RTL sample {bad}: got {int(got[bad])}, predicted {int(want[bad])}. A ramp is used "
            f"precisely so a wrong WINDOW is visible; an off-by-one here is a horizon or index bug, "
            f"not a data one.")


@pytest.mark.xsi
def test_every_command_is_answered_with_the_predicted_status(run):
    """Including the refusal: ``(4, TOO_OLD, 0)`` is as much a result as the three captures."""
    from waveflow.utils.burst_io import read_burst_bundle

    d = ROOT / "xsi" / "vectors" / "resp"
    assert d.is_dir(), "the run dumped no response bundle"
    flat = np.concatenate(read_burst_bundle(d)).astype(np.uint64)
    n = RxResp.nwords_per_inst(WORD_BW)
    got = [tuple(int(v) for v in flat[i:i + n]) for i in range(0, flat.size, n)]
    _, want = expected_capture()
    assert got == want


@pytest.mark.xsi
def test_the_converter_never_had_a_sample_refused(run):
    """THE gate for this design: the ingress writes a BRAM port, which cannot back-pressure it.

    The counter is what makes this evidence rather than an assertion. It was **not** zero the first
    time: at 256 MSPS this design dropped 1695 of 4096 samples because the ingress fires every two
    cycles, and pysim reported none — see ``RfSampBufRxTB.check_rate``, which now refuses that pairing
    at build time.
    """
    c, out = run
    assert c["ADC_WORDS_SENT"] == WANT_ADC_WORDS, (
        f"the converter delivered {c['ADC_WORDS_SENT']} words, expected {WANT_ADC_WORDS}")
    assert c["ADC_DROPPED"] == 0, (
        f"the ADC offered {c['ADC_DROPPED']} words the fabric would not take (last at cycle "
        f"{c.get('ADC_LAST_DROP_CYCLE')}) — the ingress stalled:\n{out[-2000:]}")


@pytest.mark.xsi
def test_the_memorys_read_during_write_assertion_never_fired(run):
    """``rd`` trailed ``wr`` for the whole run — checked by the hand-written memory, not by us.

    This is the horizon logic's real gate. If the capture ever read the address the ingress was
    writing that cycle, the data would be whatever the BRAM's read-during-write mode happens to be
    and nothing else in the flow would notice.
    """
    _c, out = run
    assert "read-during-write collision" not in out, (
        f"bram_t2p's assertion fired — the capture read the address being written:\n{out[-3000:]}")


@pytest.mark.xsi
def test_the_completion_cycle_is_the_recorded_one(run):
    """Time to last completion — a result, distinct from the run's loop bound."""
    c, _out = run
    assert c["OUT_LAST_CYCLE"] == WANT_LAST_CYCLE, (
        f"the last captured sample landed at cycle {c['OUT_LAST_CYCLE']}, gate expects "
        f"{WANT_LAST_CYCLE}. That is a real behaviour change: either a regression or an improvement "
        f"worth re-recording.")


@pytest.mark.xsi
def test_the_whole_command_stream_was_consumed(run):
    """Four commands in, four answered — so 'nothing came out' can never be read as 'nothing went
    in', which is exactly how the first failing run was diagnosed."""
    c, _out = run
    assert c["CMD_SENT"] == c["CMD_TOTAL"]
    assert c["RESP_WORDS"] == c["CMD_TOTAL"], "one response per command, same three-word shape"
