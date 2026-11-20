"""The two devices, as the system imports them: ``from devices import ScaleDev, Checksum``.

**Import them from here, never from ``scale_dev`` / ``checksum`` directly.**  The reviewer runs your
tests a second and a third time with ``WF_VARIANT`` set, and this module then exports a broken version
of one device; your tests must fail on each.  Do not read ``WF_VARIANT`` yourself.
"""
from __future__ import annotations

import os

from checksum import Checksum
from scale_dev import ScaleDev

_VARIANT = os.environ.get("WF_VARIANT", "")

if _VARIANT:
    from _variants import variant as _variant

    ScaleDev, Checksum = _variant(_VARIANT, ScaleDev, Checksum)

__all__ = ["ScaleDev", "Checksum"]
