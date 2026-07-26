"""Tests for the ``HwConst`` marker and ``discover_hw_const`` helper."""
from __future__ import annotations

from waveflow.hw.hw_module import HwConst, HwParam, discover_hw_const


class _OneConst:
    ncoeff: HwConst[int] = 4


def test_discover_returns_single_field():
    assert discover_hw_const(_OneConst) == {"ncoeff": 4}


class _Empty:
    pass


def test_discover_empty_class():
    assert discover_hw_const(_Empty) == {}


class _Parent:
    ncoeff: HwConst[int] = 4


class _Child(_Parent):
    ncoeff: HwConst[int] = 8


def test_subclass_override_wins():
    assert discover_hw_const(_Child) == {"ncoeff": 8}


class _Plain:
    ncoeff: int = 4


def test_plain_field_excluded():
    assert discover_hw_const(_Plain) == {}


class _Param:
    in_bw: HwParam[int] = 32


def test_hw_param_excluded():
    assert discover_hw_const(_Param) == {}


class _Mixed:
    ncoeff: HwConst[int] = 4
    in_bw: HwParam[int] = 32
    proc_latency: int = 10


def test_mixed_class_returns_only_consts():
    assert discover_hw_const(_Mixed) == {"ncoeff": 4}
