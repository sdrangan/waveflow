"""waveflow.calib.fixtures — the per-component calibration fixtures, self-registering on import.

Importing this package registers every shipped fixture (:func:`waveflow.calib.fixture.register`), so
:mod:`waveflow.calib.retime` can enumerate them via
:func:`~waveflow.calib.fixture.all_fixtures`.  Add a component's fixture by dropping a module here and
importing it below.
"""
from __future__ import annotations

from waveflow.calib.fixtures import mem_w_stream  # noqa: F401  (import for its register() side effect)

__all__ = ["mem_w_stream"]
