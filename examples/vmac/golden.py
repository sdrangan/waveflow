"""Backwards-compatible shim — ``VmacAccel`` now lives in :mod:`examples.vmac.vmac` as a
synthesizable ``HwModule`` (the golden is the Python body of its ``vmac_compute`` hook).

This module is retired in the VMAC-kernel-consolidation Phase 3; until then it re-exports
``VmacAccel`` so the existing golden / numeric / conformance imports keep working unchanged.
"""
from __future__ import annotations

from examples.vmac.vmac import VmacAccel

__all__ = ["VmacAccel"]
