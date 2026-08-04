"""Phase F: models attach to modules, and the vocabulary belongs to the platform.

Three properties, each of which replaces something that was hand-written:

* **selection** — ``top.add_rm(platform)`` recurses the hierarchy, so no registry maps components to
  models. The hierarchy is already in ``sub_comps``; a registry would be a second copy that rots the
  first time a module is renamed.
* **the default is a lookup, and an honest one** — a module measured once needs no author code, and a
  module never measured reports ``UNCALIBRATED`` rather than contributing a silent zero. A missing
  contribution makes a design read as *cheaper* than it is, turning "does not fit" into "fits".
* **the counter vocabulary is the platform's** — so a typo raises instead of being dropped when
  counters are summed, and an ASIC flow enters by declaring a platform rather than by reworking the
  model layer.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from waveflow.build.elaborate import elaborate
from waveflow.calib.confidence import ConfidenceLevel
from waveflow.calib.module_key import identify_instance
from waveflow.calib.platform import (
    VITIS_RES_TYPES,
    Platform,
    UnknownCounterError,
    packaged_platforms_dir,
)
from waveflow.calib.record_store import ModuleStore, resource_record
from waveflow.calib.resource_model import (
    LookupResourceModel,
    PriorResourceModel,
    compose,
)
from waveflow.hw.hw_module import HwModule, HwParam


@dataclass(kw_only=True)
class Leaf(HwModule):
    width: HwParam[int] = 16

    def __post_init__(self) -> None:
        super().__post_init__()
        self.bits = int(self.width) * 4


@dataclass(kw_only=True)
class Top(HwModule):
    width: HwParam[int] = 16

    def __post_init__(self) -> None:
        super().__post_init__()
        self.a = Leaf(name="a", sim=self.sim, width=self.width)
        self.b = Leaf(name="b", sim=self.sim, width=self.width * 2)
        self.add_comp(self.a)
        self.add_comp(self.b)


@pytest.fixture()
def platform(tmp_path):
    return Platform.resolve(tmp_path, "test_fpga", part="xc7z020clg484-1", clk_freq=100e6)


# ---------------------------------------------------------------------------
# F1 — the counter vocabulary belongs to the platform
# ---------------------------------------------------------------------------

def test_default_vocabulary_is_the_fpga_one(platform):
    assert platform.res_types == VITIS_RES_TYPES


def test_a_shipped_platform_loads_unchanged():
    """No ``res_types`` in the manifest means the FPGA default — existing platforms are untouched."""
    p = Platform.resolve(packaged_platforms_dir(), "zynq7020_bfm_100mhz")
    assert p.res_types == VITIS_RES_TYPES


def test_a_platform_may_declare_its_own_counters():
    """The seam an ASIC flow enters through: declare a technology, do not rework the model layer."""
    root = Path(tempfile.mkdtemp())
    made = Platform.resolve(root, "asic45", part="tsmc45", clk_freq=1e9,
                            res_types=("cell_area", "macros", "regs"))
    assert made.res_types == ("cell_area", "macros", "regs")

    reloaded = Platform.resolve(root, "asic45", part="tsmc45", clk_freq=1e9)
    assert reloaded.res_types == ("cell_area", "macros", "regs")   # persisted in the manifest
    with pytest.raises(UnknownCounterError):
        reloaded.check_counters(["lut"])                            # an FPGA counter, on an ASIC


def test_a_mistyped_counter_is_refused_not_dropped(platform):
    """Previously ``{"dspp": ...}`` predicted fine and vanished when summed, contributing zero."""
    with pytest.raises(UnknownCounterError, match="dspp"):
        PriorResourceModel(formulas={"dspp": lambda f: 32}, platform=platform)


def test_a_correct_counter_is_accepted(platform):
    PriorResourceModel(formulas={"dsp": lambda f: 32}, platform=platform)


def test_no_platform_means_no_validation():
    """A model built outside any platform (a test, an inspection) is not policed."""
    PriorResourceModel(formulas={"anything": lambda f: 1})


# ---------------------------------------------------------------------------
# F2 — attaching a model must be invisible to the key
# ---------------------------------------------------------------------------

def test_attaching_a_model_does_not_move_the_module_key(platform):
    """Load-bearing: the key is a digest of the structure signature, and ``add_rm`` needs the key to
    *choose* the model it is about to attach.  If attaching moved it, every store lookup would miss."""
    comp = elaborate(Leaf, {"width": 16}, name="leaf")
    before = identify_instance(comp, require_bound=False).key
    comp.add_rm(platform)
    assert identify_instance(comp, require_bound=False).key == before


def test_a_timing_model_is_equally_invisible():
    """Same exclusion covers ``add_timing_model``, which was latently exposed to this."""
    comp = elaborate(Leaf, {"width": 16}, name="leaf")
    before = identify_instance(comp, require_bound=False).key
    comp._timing_model, comp.firing_records = object(), [{"n": 1}]
    assert identify_instance(comp, require_bound=False).key == before


# ---------------------------------------------------------------------------
# F3 — add_rm recurses, and the default is an honest lookup
# ---------------------------------------------------------------------------

def test_add_rm_reaches_every_module(platform):
    top = elaborate(Top, {"width": 16}, name="top")
    assert top.resource_model is None and top.a.resource_model is None
    top.add_rm(platform)
    assert top.resource_model is not None
    assert top.a.resource_model is not None and top.b.resource_model is not None


def test_the_default_is_a_lookup(platform):
    top = elaborate(Top, {"width": 16}, name="top")
    top.add_rm(platform)
    assert isinstance(top.a.resource_model, LookupResourceModel)


def test_an_unmeasured_module_is_uncalibrated_not_silently_free(platform):
    """The property that keeps a missing model from making a design look cheap."""
    top = elaborate(Top, {"width": 16}, name="top")
    top.add_rm(platform)                      # empty store: nothing measured
    est = compose(top)
    assert est.level is ConfidenceLevel.UNCALIBRATED
    assert est.total["lut"] == 0              # zeros, but flagged rather than believed


def test_a_measured_module_needs_no_author_code(platform):
    """Put a record in the store and the default lookup finds it — no model was written."""
    leaf = elaborate(Leaf, {"width": 16}, name="a")
    ident = identify_instance(leaf, require_bound=False)
    store = ModuleStore(platform.dir)
    store.append(resource_record(ident, {"LUT": 833, "FF": 472}, source="hls_estimate"),
                 identity=ident)

    leaf.add_rm(platform)
    assert leaf.resource_model.confidence(leaf).level is ConfidenceLevel.EXACT
    assert leaf.resource_model.predict(leaf)["lut"] == 833


def test_add_rm_self_can_be_overridden(platform):
    """The author-facing hook: only the modules that need something else define one."""
    class Special(Leaf):
        def add_rm_self(self, plat):
            self._resource_model = PriorResourceModel(
                name="special", formulas={"dsp": lambda f: f["width"] // 4}, platform=plat)

    comp = elaborate(Special, {"width": 16}, name="s")
    comp.add_rm(platform)
    assert comp.resource_model.predict(comp) == {"dsp": 4}


def test_recursion_is_children_first(platform):
    """Post-order, so a composite's override can read what its children installed."""
    seen = []

    class Watched(Top):
        def add_rm_self(self, plat):
            seen.append(("top", self.a.resource_model is not None))
            super().add_rm_self(plat)

    elaborate(Watched, {"width": 16}, name="w").add_rm(platform)
    assert seen == [("top", True)]


# ---------------------------------------------------------------------------
# compose reads what is attached
# ---------------------------------------------------------------------------

def test_compose_needs_no_resolver(platform):
    leaf = elaborate(Leaf, {"width": 16}, name="a")
    ident = identify_instance(leaf, require_bound=False)
    ModuleStore(platform.dir).append(
        resource_record(ident, {"LUT": 100}, source="hls_estimate"), identity=ident)

    top = elaborate(Top, {"width": 16}, name="top")
    top.add_rm(platform)
    est = compose(top)                                   # no model_for
    assert est.total["lut"] == 100                       # only `a` matches the stored key


def test_an_explicit_resolver_still_overrides(platform):
    """Kept for what-ifs and for swapping one module's model without touching the rest."""
    top = elaborate(Top, {"width": 16}, name="top")
    top.add_rm(platform)
    est = compose(top, lambda c: None)
    assert est.level is ConfidenceLevel.UNCALIBRATED
