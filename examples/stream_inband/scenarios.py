"""scenarios.py -- the test scenarios, their expected results, and the checker.

Each scenario is a short list of **intents**: "a DATA transaction with these samples",
"one whose sample burst ends early", "END".  From an intent, this module writes

- the stimulus, ``<data>/<scenario>/in`` -- a burst bundle that the Python model, the pysim
  testbench and the C++ testbench all read; and
- the expected response, ``<data>/<scenario>/expected`` plus ``expected_status.json``.

The expected response is computed from the intent, not by parsing the stimulus, so it is
an independent reference: a model or a kernel that misreads the protocol cannot agree with
it by sharing the mistake.  (The arithmetic is :func:`poly.poly_eval`, which the worked
examples in ``tests/examples/test_poly_demo.py`` pin down.)

:func:`check` compares any run -- the pure model, pysim, csim, cosim -- against the expected
results, word for word and TLAST for TLAST.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:
    from examples.stream_inband.poly import (
        CoeffArray, Float32, PolyCmdHdr, PolyCmdType, PolyError, PolyRespHdr, poly_eval,
    )
except ModuleNotFoundError:  # run from inside the example directory
    from poly import (  # type: ignore[no-redef]
        CoeffArray, Float32, PolyCmdHdr, PolyCmdType, PolyError, PolyRespHdr, poly_eval,
    )
from waveflow.hw.arrayutils import write_array
from waveflow.utils.burst_io import StreamBurst, read_bursts, write_bursts

WORD_BW = 32
COEFFS = np.array([1.0, -2.0, -3.0, 4.0], dtype=np.float32)


@dataclass(frozen=True)
class Tx:
    """One DATA transaction as intended.

    ``samples`` are the samples the header announces (``nsamp = len(samples)``).
    ``sent`` is how many of them the host actually sends (default: all), and ``tlast``
    whether the last word it sends carries TLAST.  ``sent < nsamp`` with ``tlast`` is an
    early TLAST; ``tlast=False`` is a missing one.
    """

    tx_id: int
    samples: np.ndarray
    sent: int | None = None
    tlast: bool = True

    @property
    def nsamp(self) -> int:
        return int(len(self.samples))

    @property
    def n_sent(self) -> int:
        return self.nsamp if self.sent is None else int(self.sent)


def _hdr(cmd: PolyCmdType, tx_id: int = 0, nsamp: int = 0) -> np.ndarray:
    h = PolyCmdHdr()
    h.cmd_type, h.tx_id, h.nsamp = cmd, tx_id, nsamp
    return h.serialize(word_bw=WORD_BW)


def _floats(x: np.ndarray) -> np.ndarray:
    return write_array(np.asarray(x, dtype=np.float32), elem_type=Float32, word_bw=WORD_BW)


def stimulus(txs: list[Tx]) -> list[StreamBurst]:
    """The input stream: each transaction's header and samples, then END."""
    bursts: list[StreamBurst] = []
    for t in txs:
        bursts.append(StreamBurst(_hdr(PolyCmdType.DATA, t.tx_id, t.nsamp)))
        if t.n_sent:
            bursts.append(StreamBurst(_floats(t.samples[:t.n_sent]), t.tlast))
    bursts.append(StreamBurst(_hdr(PolyCmdType.END)))
    return bursts


def expected(txs: list[Tx], coeffs: np.ndarray) -> tuple[list[StreamBurst], dict]:
    """The response the protocol promises, from the intent alone.

    Per transaction: the response header; then one output per sample the kernel reads,
    with TLAST on the word that completes ``nsamp``.  An early TLAST ends the transaction
    after the samples sent (no output TLAST: ``nsamp`` was never completed) and halts with
    ``TLAST_EARLY_SAMP_IN``; a missing TLAST still returns all ``nsamp`` outputs and halts
    with ``NO_TLAST_SAMP_IN``.  Nothing after a halt is answered.
    """
    out: list[StreamBurst] = []
    status = {"halted": 0, "error": int(PolyError.NO_ERROR), "tx_id": 0}
    for t in txs:
        resp = PolyRespHdr()
        resp.tx_id = t.tx_id
        out.append(StreamBurst(resp.serialize(word_bw=WORD_BW)))
        early = t.n_sent < t.nsamp
        n_out = t.n_sent if early else t.nsamp
        if n_out:
            out.append(StreamBurst(_floats(poly_eval(coeffs, t.samples[:n_out])), not early))
        err = (PolyError.TLAST_EARLY_SAMP_IN if t.nsamp and early and t.tlast
               else PolyError.NO_TLAST_SAMP_IN if t.nsamp and not t.tlast
               else PolyError.NO_ERROR)
        if err != PolyError.NO_ERROR:
            status = {"halted": 1, "error": int(err), "tx_id": t.tx_id}
            break
    return out, status


def scenarios() -> dict[str, list[Tx]]:
    """Every scenario, from fixed seeds.  ``timing`` is the one cosim measures."""
    rng = np.random.default_rng(1)

    def x(n: int) -> np.ndarray:
        return rng.uniform(-2.0, 2.0, n).astype(np.float32)

    return {
        # Back-to-back transactions of different lengths, then END.
        "nominal": [Tx(11, x(100)), Tx(12, x(7)), Tx(13, x(1))],
        # A zero-length transaction carries no sample burst and is not an error.
        "zero_len": [Tx(21, x(0)), Tx(22, x(5)), Tx(23, x(0))],
        # The sample burst ends 4 words early: the kernel answers what it read and halts.
        "early_tlast": [Tx(31, x(20)), Tx(32, x(10), sent=6), Tx(33, x(5))],
        # The last sample word has no TLAST: all outputs, then a halt.
        "no_tlast": [Tx(41, x(10), tlast=False), Tx(42, x(5))],
        # One transaction, for the cycle count in cosim and pysim.
        "timing": [Tx(51, x(100))],
    }


#: Scenarios with no malformed burst: the ones a pysim stream can express.
WELL_FORMED = ("nominal", "zero_len", "timing")


def write_scenarios(data_dir: Path, coeffs: np.ndarray = COEFFS) -> list[str]:
    """Write every scenario's stimulus and expected response under ``data_dir``."""
    data_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for name, txs in scenarios().items():
        d = data_dir / name
        write_bursts(stimulus(txs), d / "in")
        exp, status = expected(txs, coeffs)
        write_bursts(exp, d / "expected")
        (d / "expected_status.json").write_text(json.dumps(status) + "\n", encoding="utf-8")
        c = CoeffArray(coeffs)
        c.write_uint32_file(d / "coeffs.bin")
        meta = {"n_data_tx": len(txs), "well_formed": name in WELL_FORMED}
        (d / "scenario.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
        names.append(name)
    (data_dir / "scenarios.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
    return names


def check(data_dir: Path, stage: str, names: list[str] | None = None) -> dict[str, list[str]]:
    """Compare ``<scenario>/<stage>`` against ``<scenario>/expected``, for each scenario.

    Returns ``{scenario: [problems]}``; an empty list is a pass.  Words, burst boundaries,
    TLAST flags and the final register status must all match exactly.
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
        want_st = json.loads((d / "expected_status.json").read_text(encoding="utf-8"))
        st_path = got_dir / "status.json"
        if st_path.exists():
            got_st = json.loads(st_path.read_text(encoding="utf-8"))
            if got_st != want_st:
                problems.append(f"status {got_st}, expected {want_st}")
        else:
            problems.append("no status.json")
        report[name] = problems
    return report
