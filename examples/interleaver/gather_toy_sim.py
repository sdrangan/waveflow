"""gather_toy_sim.py — Pysim harness for Gate-3 verification: Fill → SOBIF → Gather.

Generates test vectors, runs the pysim golden (Fill/Gather processes + SOBIF interface),
verifies the output matches the identity-gather expectation (words in = words out, same order).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.simulation.simobj import SimObj  # noqa: E402

from examples.interleaver.gather_toy import GatherToy, DEFAULT_MEM_DW  # noqa: E402
from waveflow.hw.interface import StreamIF  # noqa: E402


@dataclass
class InputDriver(SimObj):
    """Drives the input stream with a sequence of test words."""

    gather_toy: GatherToy = field(default=None)
    test_words: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        from waveflow.hw.interface import StreamIFMaster
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim,
                                    bitwidth=DEFAULT_MEM_DW, has_tlast=False)

    def run_proc(self) -> Generator:
        """Write test words via m_out."""
        for i, w in enumerate(self.test_words):
            # Wrap each word as a single-element numpy array
            word_arr = np.array([w], dtype=np.uint64)
            yield from self.m_out.write(word_arr)
            if (i + 1) % 8 == 0:
                print(f"[Driver] Sent {i+1} words")


@dataclass
class OutputCapture(SimObj):
    """Captures output words from the gather_toy."""

    gather_toy: GatherToy = field(default=None)
    n_words: int = 0
    captured: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        from waveflow.hw.interface import StreamIFSlave
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim,
                                 bitwidth=DEFAULT_MEM_DW, has_tlast=False)

    def run_proc(self) -> Generator:
        """Read output words and record them."""
        for i in range(self.n_words):
            words = yield from self.s_in.get(nwords_max=1)  # raw words, 1 word at a time
            w = int(words[0]) if hasattr(words, '__getitem__') else int(words)
            self.captured.append(w)
            if (i + 1) % 8 == 0:
                print(f"[Capture] Got {i+1} words")


def run_sim(n_blocks: int = 4, block_n: int = 8, seed: int = 42) -> bool:
    """Run pysim: generate test vectors, run Fill → SOBIF → Gather, verify output."""
    np.random.seed(seed)
    n_words = n_blocks * block_n

    # Generate test pattern (random words in the full range)
    test_words = [int(w) for w in np.random.randint(0, 2**64, size=n_words, dtype=np.uint64)]

    # Elaborate the composite
    from waveflow.build.elaborate import elaborate
    from waveflow.hw.interface import StreamIFMaster

    gather_toy = elaborate(
        GatherToy,
        {"mem_dwidth": DEFAULT_MEM_DW, "block_n": block_n},
        name="gather_toy_sim",
    )

    # Run simulation
    from waveflow.simulation.simulation import Simulation

    with Simulation().as_current() as sim:
        # Create driver with a master endpoint that feeds the component's input
        driver = InputDriver(name="input_driver", gather_toy=gather_toy, test_words=test_words)
        capture = OutputCapture(name="output_capture", gather_toy=gather_toy, n_words=n_words)

        # Create stream interfaces to connect driver output and component input/output
        in_if = StreamIF(name="in_if", sim=sim, clk=gather_toy.clk, bitwidth=DEFAULT_MEM_DW)
        out_if = StreamIF(name="out_if", sim=sim, clk=gather_toy.clk, bitwidth=DEFAULT_MEM_DW)

        # Bind: driver output -> component input, component output -> capture input
        in_if.bind("master", driver.m_out)
        in_if.bind("slave", gather_toy.s_in)
        out_if.bind("master", gather_toy.m_out)
        out_if.bind("slave", capture.s_in)

        # Add objects and interfaces to simulation
        sim.add_obj(gather_toy)
        sim.add_obj(driver)
        sim.add_obj(capture)

        sim.run_sim()

        # Verify: output should match input (identity gather)
        if capture.captured == test_words:
            print(f"\n[PASS] Output matches input ({len(test_words)} words)")
            print(f"  First 8:  {test_words[:8]}")
            print(f"  Last 8:   {test_words[-8:]}")
            return True
        else:
            print(f"\n[FAIL] Output mismatch!")
            print(f"  Expected {len(test_words)} words, got {len(capture.captured)}")
            mismatches = sum(1 for e, g in zip(test_words, capture.captured) if e != g)
            print(f"  Mismatches: {mismatches}")
            if mismatches > 0 and mismatches <= 16:
                for i, (e, g) in enumerate(zip(test_words, capture.captured)):
                    if e != g:
                        print(f"    word[{i}]: expected {e:#x}, got {g:#x}")
            return False


if __name__ == "__main__":
    success = run_sim(n_blocks=4, block_n=8, seed=42)
    sys.exit(0 if success else 1)
