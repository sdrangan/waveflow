"""Tests for ``HwModule.param_supports`` + ``validate_param_supports``."""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest

from waveflow.build.hwcodegen import SynthesisError
from waveflow.hw.hw_module import (
    HwModule,
    HwParam,
    _hw_param_names,
    _resolve_variant_values,
    validate_param_supports,
)


@dataclass
class _ParamComp(HwModule):
    in_bw: HwParam[int] = 32
    out_bw: HwParam[int] = 32


def test_no_param_supports_passes():
    validate_param_supports(_ParamComp)  # default is None


def test_valid_param_supports_passes():
    @dataclass
    class _Variants(HwModule):
        in_bw: HwParam[int] = 32
        out_bw: HwParam[int] = 32
        param_supports: ClassVar[dict[str, dict[str, Any]] | None] = {
            "bw64": {"in_bw": 64},
        }

    validate_param_supports(_Variants)  # does not raise


def test_non_dict_value_rejected():
    @dataclass
    class _Bad(HwModule):
        in_bw: HwParam[int] = 32
        param_supports: ClassVar[Any] = [{"in_bw": 64}]

    with pytest.raises(SynthesisError, match="must be a dict"):
        validate_param_supports(_Bad)


def test_bad_key_rejected():
    @dataclass
    class _Bad(HwModule):
        in_bw: HwParam[int] = 32
        param_supports: ClassVar[dict[str, dict[str, Any]] | None] = {
            "bad-key": {"in_bw": 64},
        }

    with pytest.raises(SynthesisError, match="valid C identifier"):
        validate_param_supports(_Bad)


def test_empty_entry_rejected():
    @dataclass
    class _Bad(HwModule):
        in_bw: HwParam[int] = 32
        param_supports: ClassVar[dict[str, dict[str, Any]] | None] = {
            "special": {},
        }

    with pytest.raises(SynthesisError, match="non-empty dict"):
        validate_param_supports(_Bad)


def test_unknown_param_rejected():
    @dataclass
    class _Bad(HwModule):
        in_bw: HwParam[int] = 32
        param_supports: ClassVar[dict[str, dict[str, Any]] | None] = {
            "v1": {"nonexistent": 1},
        }

    with pytest.raises(SynthesisError, match="unknown parameter 'nonexistent'"):
        validate_param_supports(_Bad)


def test_duplicate_resolved_configs_warn():
    @dataclass
    class _Dup(HwModule):
        in_bw: HwParam[int] = 32
        param_supports: ClassVar[dict[str, dict[str, Any]] | None] = {
            # Both variants set in_bw=32 — same as the default; pairs collide.
            "a": {"in_bw": 32},
            "b": {"in_bw": 32},
        }

    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always")
        validate_param_supports(_Dup)
    matched = [r for r in records if "resolves to the same configuration" in str(r.message)]
    assert matched, "expected a warning about duplicate resolved configurations"


def test_resolve_variant_values_fills_defaults():
    resolved = _resolve_variant_values(_ParamComp, {"in_bw": 64})
    assert resolved == {"in_bw": 64, "out_bw": 32}


def test_hw_param_names_returns_declared_params():
    assert _hw_param_names(_ParamComp) == {"in_bw", "out_bw"}
