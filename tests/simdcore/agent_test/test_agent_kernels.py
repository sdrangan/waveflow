"""The agent test's harness is sound (``plans/simdcore_agent_test.md``): every kernel's reference
program passes on seeds 0-4, and a wrong one fails with the message an agent would get."""

from __future__ import annotations

import pytest

from tests.simdcore.agent_test.kernels import KERNELS, score
from tests.simdcore.agent_test.solutions import SOLUTIONS


def test_eight_kernels():
    assert len(KERNELS) == 8 and set(KERNELS) == set(SOLUTIONS)


@pytest.mark.parametrize("name", sorted(KERNELS))
def test_reference_solution_passes(name):
    ok, msg = score(name, SOLUTIONS[name])
    assert ok, msg


def test_wrong_programs_fail_with_a_message():
    ok, msg = score("rotate", SOLUTIONS["rotate"].replace("vssra.vi v3, v4, 1", "vsra.vi v3, v4, 1"))
    assert not ok and msg.startswith("output mismatch at element")
    ok, msg = score("argmin", SOLUTIONS["argmin"].replace("vfirst.m", "vcpop.m"))
    assert not ok and msg.startswith("a0 mismatch")
    ok, msg = score("horner-4", "foo a0\n")
    assert not ok and msg.startswith("assembly failed")
    ok, msg = score("normalize-peak", SOLUTIONS["normalize-peak"].replace("vrsub.vi v2, v1, 0", "vrsub.vi v1, v1, 0"))
    assert not ok and "ILLEGAL" in msg  # an input window is read-only
