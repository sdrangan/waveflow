"""scenarios.py -- the test scenarios, their expected results, and the checker.

Each scenario is one kernel run (one ``ap_start``), written as a short list of **intents**:
"a DATA command with these samples and these coefficients", "one whose sample burst ends
early", "then END".  From the intents, this module writes, at each word width,

- the stimulus, ``<data>/w<bw>/<scenario>/in`` -- a burst bundle that the Python model, the
  pysim testbench and the C++ testbench all read; and
- the expected response, ``<data>/w<bw>/<scenario>/expected`` plus ``expected_status.json``.

The expected response is computed from the intent, not by parsing the stimulus, so it is
an independent reference: a model or a kernel that misreads the protocol cannot agree with
it by sharing the mistake.  (The arithmetic is :func:`poly.poly_eval`, which the worked
examples in ``tests/examples/test_poly_demo.py`` pin down.)

The error scenarios keep sending commands after the bad burst, as a host with commands in
flight would.  The kernel must leave them unread (contract rule 6); the host's reset, which
discards them (rule 7), is the end of the scenario.

:func:`check` compares any run -- the pure model, pysim, csim, cosim -- against the expected
results, word for word and TLAST for TLAST.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

try:
    from examples.stream_inband.poly import (
        WORD_BW_SUPPORTED, CoeffArray, Float32, PolyCmdHdr, PolyCmdType, PolyError,
        PolyRespHdr, poly_eval, samples_per_word,
    )
except ModuleNotFoundError:  # run from inside the example directory
    from poly import (  # type: ignore[no-redef]
        WORD_BW_SUPPORTED, CoeffArray, Float32, PolyCmdHdr, PolyCmdType, PolyError,
        PolyRespHdr, poly_eval, samples_per_word,
    )
from waveflow.hw.arrayutils import write_array
from waveflow.utils.burst_io import StreamBurst, read_bursts, write_bursts

#: Two coefficient sets, constant term first.
A = np.array([1.0, -2.0, -3.0, 4.0], dtype=np.float32)
B = np.array([0.5, 0.25, -1.0, 2.0], dtype=np.float32)
COEFFS = A


@dataclass(frozen=True)
class Tx:
    """One DATA command as intended.

    ``samples`` are the samples the header announces (``nsamp = len(samples)``), evaluated
    with ``coeffs``.  ``sent`` is how many of them the host actually sends (default: all),
    and ``tlast`` whether the last word it sends carries TLAST.  ``sent < nsamp`` with
    ``tlast`` is an early TLAST; ``tlast=False`` is a missing one.  An early TLAST ends on a
    word boundary, so ``sent`` must fill whole words at every width (a multiple of 2).
    """

    tx_id: int
    samples: np.ndarray
    coeffs: np.ndarray = field(default_factory=lambda: A)
    sent: int | None = None
    tlast: bool = True

    @property
    def nsamp(self) -> int:
        return int(len(self.samples))

    @property
    def n_sent(self) -> int:
        return self.nsamp if self.sent is None else int(self.sent)


@dataclass(frozen=True)
class Scenario:
    """One kernel run: its DATA commands, whether END follows, and which stages run it."""

    txs: list[Tx]
    end: bool = True          # False: nothing after the last command (it must be an error)
    pysim: bool = True        # a pysim stream cannot omit TLAST
    cosim: bool = False       # cosim runs the timing scenario and the error waveform


def _hdr(cmd: PolyCmdType, word_bw: int, tx_id: int = 0, nsamp: int = 0,
         coeffs: np.ndarray | None = None) -> np.ndarray:
    h = PolyCmdHdr()
    h.cmd_type, h.tx_id, h.nsamp = cmd, tx_id, nsamp
    h.coeffs = CoeffArray(np.zeros(4, np.float32) if coeffs is None else coeffs)
    return h.serialize(word_bw=word_bw)


def _floats(x: np.ndarray, word_bw: int) -> np.ndarray:
    return write_array(np.asarray(x, dtype=np.float32), elem_type=Float32, word_bw=word_bw)


def stimulus(sc: Scenario, word_bw: int) -> list[StreamBurst]:
    """The input stream: each command's header and samples, then END."""
    bursts: list[StreamBurst] = []
    for t in sc.txs:
        if t.n_sent < t.nsamp and t.n_sent % 2:
            raise ValueError(f"tx {t.tx_id}: an early TLAST must fill whole words (sent even)")
        bursts.append(StreamBurst(_hdr(PolyCmdType.DATA, word_bw, t.tx_id, t.nsamp, t.coeffs)))
        if t.n_sent:
            bursts.append(StreamBurst(_floats(t.samples[:t.n_sent], word_bw), t.tlast))
    if sc.end:
        bursts.append(StreamBurst(_hdr(PolyCmdType.END, word_bw)))
    return bursts


def expected(sc: Scenario, word_bw: int) -> tuple[list[StreamBurst], dict]:
    """The response the contract promises, from the intent alone.

    Per DATA command: the response header; then one result per sample the kernel reads,
    computed with that command's coefficients, as one burst closed with TLAST -- also when
    the input burst ended early (rule 6).  An early TLAST answers the samples sent and halts
    with ``TLAST_EARLY_SAMP_IN``; a missing TLAST answers all ``nsamp`` and halts with
    ``NO_TLAST_SAMP_IN``.  Nothing after a halt is answered.  The status starts clear.
    """
    out: list[StreamBurst] = []
    status = {"halted": 0, "error": int(PolyError.NO_ERROR), "tx_id": 0}
    for t in sc.txs:
        resp = PolyRespHdr()
        resp.tx_id = t.tx_id
        out.append(StreamBurst(resp.serialize(word_bw=word_bw)))
        early = t.n_sent < t.nsamp
        n_out = t.n_sent if early else t.nsamp
        if n_out:
            out.append(StreamBurst(_floats(poly_eval(t.coeffs, t.samples[:n_out]), word_bw)))
        err = (PolyError.TLAST_EARLY_SAMP_IN if early and t.tlast
               else PolyError.NO_TLAST_SAMP_IN if t.nsamp and not t.tlast
               else PolyError.NO_ERROR)
        if err != PolyError.NO_ERROR:
            status = {"halted": 1, "error": int(err), "tx_id": t.tx_id}
            break
    else:
        if not sc.end:
            raise ValueError("a scenario without END must end in an error, or the kernel blocks")
    return out, status


def scenarios() -> dict[str, Scenario]:
    """Every scenario, from fixed seeds.  ``timing`` is the one whose cycle count is checked."""
    rng = np.random.default_rng(1)

    def x(n: int) -> np.ndarray:
        return rng.uniform(-2.0, 2.0, n).astype(np.float32)

    shared = x(16)
    return {
        # Back-to-back commands of different lengths, then END.
        "multi_data": Scenario([Tx(11, x(100)), Tx(12, x(7)), Tx(13, x(1))]),
        # The same samples under A, B, A: each command uses its own coefficients, and
        # nothing carries over from the one before (rule 4).
        "coeff_change": Scenario([Tx(21, shared, A), Tx(22, shared, B), Tx(23, shared, A)]),
        # A zero-length command has a response header and no sample burst; not an error.
        "zero_len": Scenario([Tx(31, x(0)), Tx(32, x(5)), Tx(33, x(0))]),
        # The sample burst ends 4 samples early: the kernel answers what it read, closes its
        # output burst with TLAST, and halts.  Command 43 and END stay unread.
        "early_tlast": Scenario([Tx(41, x(20)), Tx(42, x(10), sent=6), Tx(43, x(5))]),
        # The last sample word has no TLAST: all results, then a halt.
        "no_tlast": Scenario([Tx(51, x(10), tlast=False), Tx(52, x(5))], pysim=False),
        # One command, for the cycle count in cosim and pysim.
        "timing": Scenario([Tx(61, x(100))], cosim=True),
        # early_tlast with nothing queued behind the bad burst: nothing is left unread, so
        # cosim can record it.  The error-path waveform in the docs is drawn from this run.
        "early_tlast_vcd": Scenario([Tx(71, x(8)), Tx(72, x(10), sent=6)], end=False,
                                    cosim=True),
    }


def write_scenarios(data_dir: Path, word_bw: int) -> list[str]:
    """Write every scenario's stimulus and expected response under ``data_dir``."""
    if word_bw not in WORD_BW_SUPPORTED:
        raise ValueError(f"word_bw must be one of {WORD_BW_SUPPORTED}")
    data_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for name, sc in scenarios().items():
        d = data_dir / name
        write_bursts(stimulus(sc, word_bw), d / "in")
        exp, status = expected(sc, word_bw)
        write_bursts(exp, d / "expected")
        (d / "expected_status.json").write_text(json.dumps(status) + "\n", encoding="utf-8")
        meta = {"n_data_tx": len(sc.txs), "word_bw": word_bw, "samples_per_word":
                samples_per_word(word_bw), "pysim": sc.pysim, "cosim": sc.cosim}
        (d / "scenario.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
        names.append(name)
    (data_dir / "scenarios.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
    return names


def check(data_dir: Path, stage: str, names: list[str] | None = None) -> dict[str, list[str]]:
    """Compare ``<scenario>/<stage>`` against ``<scenario>/expected``, for each scenario.

    Returns ``{scenario: [problems]}``; an empty list is a pass.  Words, burst boundaries,
    TLAST flags and the final register status must all match exactly.  For a scenario that
    ends in an error, rule 6's TLAST close gets its own message as well.
    """
    names = names or (data_dir / "scenarios.txt").read_text(encoding="utf-8").split()
    report: dict[str, list[str]] = {}
    for name in names:
        d = data_dir / name
        problems: list[str] = []
        got_dir = d / stage
        if not (got_dir / "words.bin").exists():
            report[name] = [f"no {stage} output in {got_dir}"]
            continue
        want, got = read_bursts(d / "expected"), read_bursts(got_dir)
        want_st = json.loads((d / "expected_status.json").read_text(encoding="utf-8"))
        if want_st["halted"] and got and not got[-1].tlast:
            problems.append("rule 6: the output burst in progress at the error was not "
                            "closed with TLAST")
        if len(want) != len(got):
            problems.append(f"{len(got)} bursts, expected {len(want)}")
        for k, (w, g) in enumerate(zip(want, got)):
            if w.tlast != g.tlast:
                problems.append(f"burst {k}: tlast={g.tlast}, expected {w.tlast}")
            if not np.array_equal(np.asarray(w.words, np.uint64), np.asarray(g.words, np.uint64)):
                n = min(len(w.words), len(g.words))
                bad = next((i for i in range(n) if int(w.words[i]) != int(g.words[i])), n)
                problems.append(f"burst {k}: {len(g.words)} words, expected {len(w.words)}; "
                                f"first difference at word {bad}")
        st_path = got_dir / "status.json"
        if st_path.exists():
            got_st = json.loads(st_path.read_text(encoding="utf-8"))
            if got_st != want_st:
                problems.append(f"status {got_st}, expected {want_st}")
        else:
            problems.append("no status.json")
        report[name] = problems
    return report
