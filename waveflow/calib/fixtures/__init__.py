"""waveflow.calib.fixtures — the per-component calibration fixtures, self-registering on import.

Importing this package registers every shipped fixture (:func:`waveflow.calib.fixture.register`), so a
caller can enumerate them via :func:`~waveflow.calib.fixture.all_fixtures` without a hardcoded list.
Add a component's fixture by dropping a module here and importing it below.

A whole-platform ``retime`` command — sweep every registered fixture and refit the library end to end
— is **planned, not built**.  The registry exists for it; the driving is per-example today.
"""
from __future__ import annotations

from waveflow.calib.fixtures import mem_r_stream  # noqa: F401  (import for its register() side effect)
from waveflow.calib.fixtures import mem_w_stream  # noqa: F401  (import for its register() side effect)

__all__ = ["mem_r_stream", "mem_w_stream"]
