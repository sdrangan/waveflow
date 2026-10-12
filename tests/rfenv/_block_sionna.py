"""A pytest plugin that makes Sionna, Mitsuba and Dr.Jit unimportable: an install without the extra.

``pip install -e ".[sionna]"`` is optional, and everything outside the ``sionna`` marker must run
without it (AC9 of ``plans/sionna_rt/sionna_rt.md``).  Testing that in a venv that *has* Sionna needs
the import to fail on purpose, so this plugin puts a finder at the front of ``sys.meta_path`` that
refuses the three packages and drops any copy already imported::

    python -m pytest -p tests.rfenv._block_sionna -m "not sionna" tests/rfenv/

Loading it as a plugin rather than from a conftest is the point: it is active before collection, so
a test module that imported Sionna at module scope fails to collect instead of quietly passing.
"""

from __future__ import annotations

import importlib.abc
import importlib.metadata
import sys

#: The top-level packages an install without the ``sionna`` extra does not have.
BLOCKED = ("sionna", "mitsuba", "drjit")


class SionnaBlocker(importlib.abc.MetaPathFinder):
    """Refuse every import of :data:`BLOCKED` and their submodules."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            # ModuleNotFoundError, as a real install without the package raises, so code that
            # catches it behaves the same here.  (importlib.util.find_spec still raises rather than
            # returning None; probe for Sionna with try/import, as waveflow.rfenv does.)
            raise ModuleNotFoundError(
                f"No module named {fullname!r} (blocked by tests.rfenv._block_sionna: an "
                f"install without the 'sionna' extra)",
                name=fullname,
            )


#: The distributions an install without the extra does not have (package metadata names).
BLOCKED_DISTS = ("sionna-rt", "sionna", "mitsuba", "drjit")


def _hidden(name: str) -> bool:
    return name.lower().replace("_", "-") in BLOCKED_DISTS


def _wrap(fn):
    def wrapped(name, *a, **kw):
        if _hidden(name):
            raise importlib.metadata.PackageNotFoundError(name)
        return fn(name, *a, **kw)

    wrapped._sionna_blocker = True
    return wrapped


def install() -> None:
    """Drop already-imported copies, put the blocker first, and hide the packages' metadata.

    Hiding the metadata matters as much as refusing the import: code that asks
    ``importlib.metadata.version("sionna-rt")`` (the cache does, to decide staleness) must see what a
    real install without the extra shows -- no such package.  Idempotent.
    """
    for name in list(sys.modules):
        if name.split(".")[0] in BLOCKED:
            del sys.modules[name]
    if not any(isinstance(f, SionnaBlocker) for f in sys.meta_path):
        sys.meta_path.insert(0, SionnaBlocker())
    for attr in ("version", "distribution", "metadata"):
        fn = getattr(importlib.metadata, attr)
        if not getattr(fn, "_sionna_blocker", False):
            setattr(importlib.metadata, attr, _wrap(fn))


def pytest_configure(config) -> None:
    install()
