"""sw_host_gen.py — the generated half of a software host's C++ realization (plans/host_runtime.md).

A :class:`~waveflow.sw.SwHost`'s C++ twin is a class the user writes -- thread bodies only -- deriving
from ``<Host>_endpoints``, which this module generates from the **live, wired** host:

* one ``SwIrq`` per interrupt input (``add_irq``), named as in Python, bound to a pin of the top;
* one blocking endpoint per program endpoint, named as in Python and constructed on its view -- the
  absolute address, sizes and poll period read off the endpoint's interface, the interrupt it waits on
  matched to the host's own ``IrqIFSink`` -- or a ``BusRw`` per ``BusReader``;
* the bus master (``add_bus_master``) and the constructor the harness calls.

So the C++ never names an address, a pin binding or a threshold, and the two realizations cannot
disagree about which view an endpoint reaches: both are read off the same objects.
"""
from __future__ import annotations

from waveflow.build.hwcodegen import LoweringError

_CPP_KINDS = {"queue_in": "QueueIn", "queue_out": "QueueOut", "regbank": "RegBank", "bram": "Bram",
              "credit_in": "CreditIn"}


def endpoints_class(host) -> str:
    """The generated base class's name: ``<HostClass>_endpoints``."""
    return f"{type(host).__name__}_endpoints"


def endpoints_header(host) -> str:
    return f"{endpoints_class(host)}.h"


def _registered(host, ep) -> bool:
    return any(e is ep for e in host.endpoints.values())


def host_layout(host) -> dict:
    """Classify *host*'s attributes: ``{"bus": attr, "irqs": [attr...], "endpoints": [(attr, kind,
    iface_or_reader, irq_attr)...]}`` -- in attribute order."""
    from waveflow.hw.irq import IrqIFSink
    from waveflow.hw.memif import MMIFMaster
    from waveflow.hw.mm_host import (
        BusReader,
        MmQueueInIF,
        MmQueueOutIF,
        MmRegBankCfgIF,
        MmStatusIF,
    )

    kinds = {MmQueueInIF: "QueueWriter", MmQueueOutIF: "QueueReader", MmRegBankCfgIF: "RegCfg",
             MmStatusIF: "StatusReader"}
    bus, irqs, eps = [], [], []
    irq_attr = {}
    for attr, v in vars(host).items():
        if isinstance(v, MMIFMaster) and _registered(host, v):
            bus.append(attr)
        elif isinstance(v, IrqIFSink) and _registered(host, v):
            irqs.append(attr)
            irq_attr[id(v)] = attr
    for attr, v in vars(host).items():
        if isinstance(v, BusReader):
            eps.append((attr, "BusRw", v, None))
            continue
        iface = getattr(v, "interface", None)
        kind = next((k for c, k in kinds.items() if isinstance(iface, c)), None)
        if kind is None:
            continue
        irq = getattr(iface, "irq", None)
        if irq is not None and id(irq) not in irq_attr:
            raise LoweringError(f"{type(host).__name__}.{attr} waits on an interrupt the host does not "
                                f"own -- create it with add_irq()")
        eps.append((attr, kind, iface, irq_attr.get(id(irq)) if irq is not None else None))
    if len(bus) != 1:
        raise LoweringError(f"{type(host).__name__}: a software host has exactly one bus master "
                            f"(add_bus_master); found {bus}")
    return {"bus": bus[0], "irqs": irqs, "endpoints": eps}


def traced_endpoints(host) -> list[tuple[str, object]]:
    """``(attribute, endpoint)`` of every endpoint whose trace a host dumps -- the generated header's
    ``traced(...)`` list, so both realizations name their traces alike."""
    return [(attr, getattr(host, attr)) for attr, *_ in host_layout(host)["endpoints"]]


def host_ports(host) -> tuple[str, ...]:
    """The C++ model's ports, in constructor order: the bus master, then every interrupt input."""
    lay = host_layout(host)
    return (lay["bus"], *lay["irqs"])


def _view_literal(v) -> str:
    if not v.name.isidentifier():
        raise LoweringError(f"view name {v.name!r} is not a C++ identifier")
    return (f'MmView{{"{v.name}", MmKind::{_CPP_KINDS[v.kind]}, 0x{v.base:x}ull, {v.window}u, '
            f"{v.bytes_per_word}u, {v.depth or 0}u, {v.ncfg or 0}u, {v.nstat or 0}u, {v.nelem or 0}u}}")


def render_host_endpoints_h(host) -> str:
    """``<Host>_endpoints.h`` for the wired *host* -- see the module docstring."""
    lay = host_layout(host)
    cls = endpoints_class(host)
    bus = getattr(host, lay["bus"])
    bpw = int(bus.bitwidth) // 8
    guard = f"WAVEFLOW_GEN_{cls.upper()}_H"
    params = [f"Dut& d_{lay['bus']}, const char* p_{lay['bus']}"]
    params += [f"Dut& d_{a}, const char* p_{a}" for a in lay["irqs"]]
    inits = [f"SwHostModel(d_{lay['bus']}, p_{lay['bus']}, {bpw})"]
    inits += [f"{a}(sched_, d_{a}, p_{a})" for a in lay["irqs"]]
    members = [f"    SwIrq {a};" for a in lay["irqs"]]
    for attr, kind, src, irq in lay["endpoints"]:
        if kind == "BusRw":
            members.append(f"    BusRw {attr};")
            inits.append(f"{attr}(sched_, bus_)")
            continue
        members.append(f"    {kind} {attr};")
        args = [ "sched_", "bus_", _view_literal(src.view), str(int(src.poll_cycles))]
        if kind in ("QueueWriter", "QueueReader") and irq is not None:
            args.append(f"&{irq}")
        inits.append(f"{attr}({', '.join(args)})")
    body = [f"        add_pin(&{a});" for a in lay["irqs"]]
    body += [f'        traced("{attr}", &{attr});' for attr, *_ in lay["endpoints"]]
    lines = [
        f"#ifndef {guard}",
        f"#define {guard}",
        f"// {cls}.h -- GENERATED by waveflow (build/sw_host_gen.py) from the wired "
        f"{type(host).__name__}.",
        "// DO NOT EDIT: regenerate.  The host's endpoints, named as in Python, each on its view (the",
        "// address, sizes and poll period read off the Python endpoint) with its interrupt; derive the",
        "// host program from this class and override main().",
        '#include "xsi_sw.h"',
        "",
        "namespace wfbfm {",
        "",
        f"class {cls} : public SwHostModel {{",
        "public:",
        *members,
        "",
        f"    {cls}(" + ", ".join(params) + ")",
        "        : " + ",\n          ".join(inits) + " {",
        *body,
        "    }",
        "};",
        "",
        "}  // namespace wfbfm",
        "",
        f"#endif  // {guard}",
    ]
    return "\n".join(lines) + "\n"


__all__ = ["endpoints_class", "endpoints_header", "host_layout", "host_ports",
           "render_host_endpoints_h", "traced_endpoints"]
