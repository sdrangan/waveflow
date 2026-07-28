"""The A1 gate: a module key is content-addressed, stable, and structural.

Four properties, each of which the resource store depends on being true (``plans/resource_model.md``):

1. **Same configuration -> same key**, however it was spelled.  This is the cache-hit property: two
   different system-level parameter vectors that induce the same module must reuse one synthesis.
2. **Different structure -> different key.**  In particular a *realization* knob like ``unroll_lane``
   must select a different model rather than becoming a regression column.
3. **Stable across processes.**  A key built from a randomized hash would never hit, and every miss
   would look like new work instead of a bug.
4. **The walk finds sub-modules**, so a system estimate can be assembled as a sum of per-module
   lookups without hand-declaring which parameters reach which module.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

import pytest

from waveflow.build.elaborate import elaborate
from waveflow.calib.module_key import (
    ModuleIdentity,
    UnboundModuleError,
    UnstableSignatureError,
    identify,
    identify_instance,
    module_key,
    resolved_params,
    signature_digest,
    snake_name,
    walk_modules,
)
from waveflow.hw.hw_module import HwModule, HwParam
from waveflow.simulation.simulation import Simulation


# ---------------------------------------------------------------------------
# Fixtures — minimal modules whose structure moves with their params
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class Leaf(HwModule):
    """A leaf whose structure depends on ``width`` and on the structural knob ``wide``."""

    width: HwParam[int] = 16
    wide: HwParam[bool] = False

    def __post_init__(self) -> None:
        super().__post_init__()
        # Structure that actually moves with the params, so the signature has something to see.
        self.lanes = [f"lane{i}" for i in range(2 if self.wide else 1)]
        self.bits = int(self.width) * len(self.lanes)


@dataclass(kw_only=True)
class Top(HwModule):
    """A composite that projects its own params onto one child."""

    width: HwParam[int] = 16
    depth: HwParam[int] = 4          # a param the child never sees

    def __post_init__(self) -> None:
        super().__post_init__()
        self.child = Leaf(name="leaf", sim=self.sim, width=self.width)
        self.add_comp(self.child)
        self.buffer = [0] * int(self.depth)


# ---------------------------------------------------------------------------
# 1. Same configuration -> same key
# ---------------------------------------------------------------------------

def test_same_config_same_key_however_spelled():
    """An explicitly-passed default and an omitted one are the same hardware, so the same key."""
    explicit = module_key(Leaf, {"width": 16, "wide": False})
    defaulted = module_key(Leaf, {})
    partial = module_key(Leaf, {"width": 16})
    assert explicit == defaulted == partial


def test_cache_hit_across_different_system_params():
    """Two system configurations differing only in a param the child never sees share the child key.

    This is the DSE cache-hit property in miniature: ``depth`` reaches the top and not the leaf, so
    sweeping it must not invalidate the leaf's synthesis.
    """
    a = walk_modules(elaborate(Top, {"width": 24, "depth": 4}))
    b = walk_modules(elaborate(Top, {"width": 24, "depth": 64}))
    leaf_a = next(i for p, _, i in a if p.endswith("leaf"))
    leaf_b = next(i for p, _, i in b if p.endswith("leaf"))
    assert leaf_a.key == leaf_b.key
    # ...while the tops themselves are genuinely different hardware.
    assert a[0][2].key != b[0][2].key


# ---------------------------------------------------------------------------
# 2. Different structure -> different key
# ---------------------------------------------------------------------------

def test_realization_knob_forks_the_key():
    """``wide`` is a different circuit, not a feature: it must select a different model."""
    assert module_key(Leaf, {"wide": False}) != module_key(Leaf, {"wide": True})


def test_width_forks_the_key():
    assert module_key(Leaf, {"width": 8}) != module_key(Leaf, {"width": 16})


def test_key_shape_is_readable_prefix_plus_digest():
    ident = identify(Leaf, {"width": 8})
    prefix, _, digest = ident.key.partition("-")
    assert prefix == "leaf"
    assert len(digest) == 8 and all(c in "0123456789abcdef" for c in digest)
    # The short key is a prefix of the full digest the store compares on load.
    assert ident.signature.startswith(digest)
    assert len(ident.signature) == 64


def test_snake_name():
    assert snake_name("FirCompute") == "fir_compute"
    assert snake_name("MemRStream") == "mem_r_stream"
    assert snake_name("Leaf") == "leaf"


# ---------------------------------------------------------------------------
# 3. Stability across processes
# ---------------------------------------------------------------------------

_SUBPROCESS_SRC = """
import sys
sys.path.insert(0, {root!r})
from tests.calib.test_module_key import Leaf
from waveflow.calib.module_key import module_key
print(module_key(Leaf, {{"width": 24, "wide": True}}))
"""


def test_key_is_stable_across_processes():
    """The digest must not come from ``hash()`` — string hashing is randomized per process.

    Run in a fresh interpreter with a *different* ``PYTHONHASHSEED`` and compare.
    """
    import os
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ, PYTHONHASHSEED="12345")
    out = subprocess.run([sys.executable, "-c", _SUBPROCESS_SRC.format(root=root)],
                         capture_output=True, text=True, env=env, cwd=root)
    assert out.returncode == 0, f"subprocess failed:\n{out.stdout}\n{out.stderr}"
    assert out.stdout.strip() == module_key(Leaf, {"width": 24, "wide": True})


class _Opaque:
    """No ``__dict__``, so structure canonicalization falls back to ``repr`` — the default one, which
    embeds a memory address."""

    __slots__ = ()


@dataclass(kw_only=True)
class Leaky(HwModule):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.thing = _Opaque()


def test_address_leak_is_caught_by_the_purity_gate_on_the_elaborate_path():
    """An address leak also makes two elaborations differ, so ``identify`` trips the existing gate.

    Recorded rather than assumed: the two defences are layered, and this is the outer one.
    """
    from waveflow.build.elaborate import ParamPurityError

    with pytest.raises(ParamPurityError):
        identify(Leaky, {})


def test_address_leak_is_rejected_loudly_on_the_instance_path():
    """``identify_instance`` is the path with no purity gate — ``walk_modules`` uses it on a live tree.

    Nothing else stands between an address leak and a key that changes every run, so the digest must
    refuse it here itself.
    """
    leaky = Leaky(name="leaky", sim=Simulation())
    with pytest.raises(UnstableSignatureError, match="object address"):
        identify_instance(leaky)


# ---------------------------------------------------------------------------
# 3b. Boundness — an undetermined structure has no storable key
# ---------------------------------------------------------------------------

def test_unbound_module_cannot_be_keyed_for_storage():
    """A leaf elaborated on its own has unwired ports, so its structure is not yet determined.

    Catching this at the key is what prevents a Phase-C **join failure**: a standalone fixture that
    filed under an unbound key would produce records no composite lookup can ever reach.
    """
    from examples.fir_block.fir_block import FirCompute

    alone = elaborate(FirCompute, {"ntap": 32, "samp_w": 16}, name="fir_compute")
    with pytest.raises(UnboundModuleError, match="queue_size=None"):
        identify_instance(alone)                       # storage path: strict
    identify_instance(alone, require_bound=False)      # inspection path: allowed


def test_bound_submodule_of_a_composite_keys_cleanly():
    """The same module wired inside its composite has a resolved depth, so it keys without complaint."""
    from examples.fir_block.fir_block import MEM_DW, FirBlock

    top = elaborate(FirBlock, {"mem_dwidth": MEM_DW, "ntap": 32, "samp_w": 16,
                               "samp_i": 2, "unroll_lane": False}, name="fir_block")
    keys = {i.cls_name: i.key for _, _, i in walk_modules(top)}    # strict by default
    assert "FirCompute" in keys


def test_walk_of_a_composite_is_strict_about_boundness():
    """``walk_modules`` is the storage path, so it must not silently key an unbound child."""

    @dataclass(kw_only=True)
    class Unwired(HwModule):
        def __post_init__(self) -> None:
            super().__post_init__()
            self.child = Leaf(name="leaf", sim=self.sim)
            self.child.endpoints["s_in"] = _FakeStreamEp()
            self.add_comp(self.child)

    with pytest.raises(UnboundModuleError):
        walk_modules(elaborate(Unwired, {}))


class _FakeStreamEp:
    """Stands in for an unbound stream endpoint: has a ``queue_size``, and it is ``None``."""

    queue_size = None


# ---------------------------------------------------------------------------
# 4. The walk, and the resolved-params record
# ---------------------------------------------------------------------------

def test_walk_finds_submodules_with_paths():
    walked = walk_modules(elaborate(Top, {"width": 24, "depth": 4}))
    paths = [p for p, _, _ in walked]
    assert len(walked) == 2
    assert paths[1].endswith(".leaf")
    assert walked[0][2].cls_name == "Top"
    assert walked[1][2].cls_name == "Leaf"


def test_walk_can_exclude_the_top():
    walked = walk_modules(elaborate(Top, {"width": 24}), include_top=False)
    assert [i.cls_name for _, _, i in walked] == ["Leaf"]


def test_resolved_params_include_defaults():
    """The record must show what shaped the hardware, not just what the caller bothered to pass."""
    ident = identify(Leaf, {"width": 8})
    assert ident.params == {"width": 8, "wide": False}


def test_identity_round_trips_through_json():
    ident = identify(Leaf, {"width": 8, "wide": True})
    assert ModuleIdentity.from_json(ident.to_json()) == ident


def test_identify_instance_matches_identify():
    """Walking a live tree and elaborating from ``(class, params)`` must agree on the key."""
    top = Top(name="top", sim=Simulation(), width=24, depth=4)
    walked = {p.rsplit(".", 1)[-1]: i for p, _, i in walk_modules(top)}
    assert walked["leaf"].key == module_key(Leaf, {"width": 24, "wide": False})


def test_signature_digest_is_deterministic_within_a_process():
    a = elaborate(Leaf, {"width": 12}, name="a")
    b = elaborate(Leaf, {"width": 12}, name="b")
    assert signature_digest(a) == signature_digest(b)
    assert resolved_params(a) == resolved_params(b)
