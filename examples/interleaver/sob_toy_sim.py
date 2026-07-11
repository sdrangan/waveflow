"""sob_toy_sim.py — pysim golden harness for the :class:`~examples.interleaver.sob_toy.SobToy`
composite (Phase 3, Gate 3).

Wires the pure-AXIS ``Fill ->SOBIF-> Gather`` composite to three stream endpoints (``x_in`` / ``p_in``
drivers, ``y_out`` sink) and checks the functional golden ``Y[j][i] = X[j][P[i]]`` bit-exact, plus the
**ping-pong overlap**: the total run is ~``(NJ+1)·N`` (Fill fills block j+1 while Gather random-reads
block j), well under the ``NJ·2N`` serial bound — the whole point of the depth-2 SOBIF.
"""
from __future__ import annotations

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF
from waveflow.simulation.simulation import Simulation

from examples.interleaver.sob_toy import SobToy
from examples.interleaver.mem_stream_sim import WordDriver, WordSink


def _Pidx(n: int) -> np.ndarray:
    """The interleave permutation P[i] = (i*13+5) % N (matches the sandbox BFM)."""
    return ((np.arange(n) * 13 + 5) % n).astype(np.uint32)


def _Xval(j: int, n: int) -> np.ndarray:
    """Per-job source block X[j] (distinct 32-bit patterns; matches the sandbox up to bit layout)."""
    return ((np.arange(n, dtype=np.uint64) * 2654435761 + 12345 + j * 7919)
            & 0xFFFFFFFF).astype(np.uint32)


def run_sob(nj: int = 4, block_n: int = 256, elem_bw: int = 32) -> "SobToy":
    """Run the SobToy composite over *nj* blocks and check Y[j][i]=X[j][P[i]] bit-exact + overlap.
    Returns the composite (``gather.job_end_cyc`` carries the per-block completion timeline)."""
    sim = Simulation()
    clk = Clock(freq=100e6)

    P = _Pidx(block_n)
    X = [_Xval(j, block_n) for j in range(nj)]
    Y = [X[j][P] for j in range(nj)]                      # golden gather per job

    toy = SobToy(name="toy", sim=sim, elem_bw=elem_bw, block_n=block_n)
    x_drv = WordDriver(sim=sim, bitwidth=elem_bw, bursts=[x.astype(np.uint64) for x in X])
    p_drv = WordDriver(sim=sim, bitwidth=elem_bw, bursts=[P.astype(np.uint64) for _ in range(nj)])
    y_sink = WordSink(sim=sim, bitwidth=elem_bw)

    x_if = StreamIF(sim=sim, clk=clk, bitwidth=elem_bw)
    x_if.bind(ep_name="master", endpoint=x_drv.stream_ep)
    x_if.bind(ep_name="slave", endpoint=toy.x_in)

    p_if = StreamIF(sim=sim, clk=clk, bitwidth=elem_bw)
    p_if.bind(ep_name="master", endpoint=p_drv.stream_ep)
    p_if.bind(ep_name="slave", endpoint=toy.p_in)

    y_if = StreamIF(sim=sim, clk=clk, bitwidth=elem_bw)
    y_if.bind(ep_name="master", endpoint=toy.y_out)
    y_if.bind(ep_name="slave", endpoint=y_sink.stream_ep)

    sim.run_sim()

    got = np.concatenate(y_sink.words) if y_sink.words else np.array([], dtype=np.uint32)
    exp = np.concatenate(Y).astype(np.uint32)
    ok = np.array_equal(got.astype(np.uint32), exp)

    total_cyc = sim.env.now / clk.period
    overlap_floor = (nj + 1) * block_n
    serial = nj * 2 * block_n
    print(f"[sob] nj={nj} n={block_n} ok={ok} total={total_cyc:.0f}cyc "
          f"(overlap_floor~{overlap_floor}, serial~{serial})")
    assert ok, f"SobToy mismatch:\n got={got[:8]}\n exp={exp[:8]}"
    # Overlap: the run must be near the (NJ+1)*N floor, well under the NJ*2N serial bound.
    assert total_cyc < 1.5 * overlap_floor, \
        f"no ping-pong overlap: total {total_cyc} not ~ {overlap_floor} (serial {serial})"
    return toy


def run_and_check() -> bool:
    run_sob()
    print("sob_toy pysim golden: PASSED")
    return True


if __name__ == "__main__":
    run_and_check()
