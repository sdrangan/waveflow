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
import sys

#: The top-level packages an install without the ``sionna`` extra does not have.
BLOCKED = ("sionna", "mitsuba", "drjit")


class SionnaBlocker(importlib.abc.MetaPathFinder):
    """Refuse every import of :data:`BLOCKED` and their submodules."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ImportError(
                f"{fullname} is blocked by tests.rfenv._block_sionna (an install without the "
                f"'sionna' extra)"
            )


def install() -> None:
    """Drop already-imported copies and put the blocker first.  Idempotent."""
    for name in list(sys.modules):
        if name.split(".")[0] in BLOCKED:
            del sys.modules[name]
    if not any(isinstance(f, SionnaBlocker) for f in sys.meta_path):
        sys.meta_path.insert(0, SionnaBlocker())


def pytest_configure(config) -> None:
    install()
