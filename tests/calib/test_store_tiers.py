"""Store consistency (``plans/system_resources.md`` Stage 3): **a build writes the work tier; lookups
read the work tier first, then the published library.**

* a record a build files is returned by the next lookup for that key -- through the default lookup
  model, not only the raw store;
* the published library is never written, and answers for everything the work tier lacks;
* a published *fitted* model does not move when a record lands: fitted models are frozen until refit
  and republished (``load_or_fit`` loads the artifact; it does not refit on read).
"""
from __future__ import annotations

import json
import shutil

from waveflow.calib.module_key import ModuleIdentity
from waveflow.calib.platform import PLATFORM_MANIFEST, Platform
from waveflow.calib.record_store import ModuleStore, resource_record


def _seed(root, name="plat", part="xc7z020clg484-1", freq=100e6):
    d = root / name
    d.mkdir(parents=True)
    (d / PLATFORM_MANIFEST).write_text(json.dumps({"part": part, "clk_freq_hz": freq}))
    return d


def _ident(key="leaf-0000aaaa"):
    return ModuleIdentity(key=key, cls_name="Leaf", cls_module="t", params={}, signature="s" * 64)


def _rec(lut, key="leaf-0000aaaa", source="hls_estimate"):
    return resource_record(_ident(key), {"lut": lut, "ff": 1}, source=source)


def test_a_platform_from_a_fallback_writes_the_work_tier(tmp_path):
    pub = _seed(tmp_path / "pub")
    ModuleStore(pub).append(_rec(100), identity=_ident())
    p = Platform.resolve(tmp_path / "work", "plat", fallbacks=[tmp_path / "pub"])
    assert p.dir == pub and p.write_dir == tmp_path / "work" / "plat"
    store = p.module_store()
    assert store.best("leaf-0000aaaa", "resource").payload["lut"] == 100      # published answers
    assert not p.write_dir.exists()                                            # nothing written yet

    store.append(_rec(7, key="leaf-0000bbbb"), identity=_ident("leaf-0000bbbb"))
    assert (p.write_dir / PLATFORM_MANIFEST).is_file()                         # seeded on first write
    assert p.module_store().best("leaf-0000bbbb", "resource").payload["lut"] == 7
    assert ModuleStore(pub).keys() == ["leaf-0000aaaa"]                        # the library untouched

    # A re-measurement in the work tier shadows the published one; keys are the union.
    p.module_store().append(_rec(55), identity=_ident())
    s = p.module_store()
    assert s.best("leaf-0000aaaa", "resource").payload["lut"] == 55
    assert s.keys() == ["leaf-0000aaaa", "leaf-0000bbbb"]


def test_a_platform_in_the_primary_writes_itself(tmp_path):
    d = _seed(tmp_path / "work")
    p = Platform.resolve(tmp_path / "work", "plat", fallbacks=[tmp_path / "pub"])
    assert p.write_dir == p.dir == d
    p.module_store().append(_rec(3), identity=_ident())
    assert ModuleStore(d).best("leaf-0000aaaa", "resource").payload["lut"] == 3


def test_the_default_lookup_sees_what_the_build_filed(tmp_path):
    """add_rm's default model reads the layered store: a record filed under a module's key makes
    that module EXACT on the next lookup."""
    from examples.markov.markov_build import system
    from waveflow.calib.confidence import ConfidenceLevel
    from waveflow.calib.module_key import identify_instance
    from waveflow.calib.record_store import platform_store
    from waveflow.hw.hw_module import HwModule

    _seed(tmp_path / "pub")
    p = Platform.resolve(tmp_path / "work", "plat", fallbacks=[tmp_path / "pub"])
    gen = system().gen
    HwModule._RM_CACHE.clear()
    gen.add_rm(p)
    assert gen.resource_model.confidence(gen).level == ConfidenceLevel.UNCALIBRATED
    ident = identify_instance(gen, require_bound=False)
    platform_store(p).append(resource_record(ident, {"lut": 2585, "ff": 1905}, source="hls_estimate"),
                             identity=ident)
    assert gen.resource_model.confidence(gen).level == ConfidenceLevel.EXACT
    assert gen.resource_model.predict(gen)["lut"] == 2585


def test_a_published_fitted_model_does_not_move_when_a_record_lands(tmp_path):
    from examples.vecmult.vecmult import VecMult
    from examples.vecmult.vecmult_corpus import COMMITTED_CALIB
    from waveflow.build.elaborate import elaborate
    from waveflow.calib.module_key import identify_instance
    from waveflow.hw.hw_module import HwModule

    shutil.copytree(COMMITTED_CALIB, tmp_path / "pub" / COMMITTED_CALIB.name)
    p = Platform.resolve(tmp_path / "work", COMMITTED_CALIB.name, fallbacks=[tmp_path / "pub"])
    HwModule._RM_CACHE.clear()
    VecMult.get_rm(p).save_model()                       # publish the fit beside the library
    comp = elaborate(VecMult, {}, name="vec_mult")
    before = VecMult.get_rm(p).predict(comp)

    ident = identify_instance(comp, require_bound=False)
    p.module_store().append(resource_record(ident, {"lut": 10 ** 6, "ff": 10 ** 6},
                                            source="hls_estimate"), identity=ident)
    assert p.module_store().best(ident.key, "resource").payload["lut"] == 10 ** 6   # lookup sees it
    assert VecMult.get_rm(p).predict(comp) == before                                 # the fit does not
