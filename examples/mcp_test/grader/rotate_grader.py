"""Independent grader for ``examples/mcp_test/rotate.md``.

Each blind-test arm writes its own tests, and passes them.  This grader is the check that
neither arm wrote: it generates **hidden** transactions from a seed chosen at grading time,
runs the arm's final kernel on them in Vitis C simulation through a testbench of its own,
and compares every output sample with a fixed-point reference written here.

``rotate.md`` is silent on three things the grader therefore cannot fix:

* **the word layout** -- header fields, lane packing, how the kernel is called.  A small
  per-run *adapter* (``reference/rotate_ref_adapter.py`` is the template) supplies it, written
  from the arm's report, never from its code;
* **rounding and overflow.**  The grader scores the output against every convention in
  :data:`CONVENTIONS` and reports which ones it matches.  An arm passes when one convention
  matches **every** sample: any consistent choice is a legal reading of the spec, so the
  question is only whether the kernel computes the one it chose, bit for bit;
* **errors.**  "Halts on error and sets a status" defines no error, so error behaviour is
  not graded.

Usage::

    python -m examples.mcp_test.grader.rotate_grader --adapter path/to/adapter.py \\
        [--work DIR] [--seed N] [--word-bw 32 --word-bw 64]

The work directory (default: beside the adapter, ``rotate_grade/``) holds the stimulus, the
testbench, the Vitis projects and ``grade.json`` / ``grade.md``.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Callable

# rotate.md: cos/sin are Q10.8 (10 bits, 8 fractional), samples Q16.8.
COEF_BITS, COEF_FRAC = 10, 8
IN_BITS, IN_FRAC = 16, 8
PROD_FRAC = COEF_FRAC + IN_FRAC

DEFAULT_TIMEOUT_S = 900.0


# ---------------------------------------------------------------------------
# The reference: every legal reading of the spec's arithmetic
# ---------------------------------------------------------------------------


def _floor(v: int, k: int) -> int:
    return v >> k


def _half_up(v: int, k: int) -> int:
    return (v + (1 << (k - 1))) >> k if k else v


def _half_even(v: int, k: int) -> int:
    if k == 0:
        return v
    q, r = divmod(v, 1 << k)
    half = 1 << (k - 1)
    return q + (1 if r > half or (r == half and q & 1) else 0)


def _toward_zero(v: int, k: int) -> int:
    return -((-v) >> k) if v < 0 else v >> k


def _half_away(v: int, k: int) -> int:
    if k == 0:
        return v
    m = (abs(v) + (1 << (k - 1))) >> k
    return -m if v < 0 else m


ROUNDINGS: dict[str, Callable[[int, int], int]] = {
    "floor": _floor, "half_up": _half_up, "half_even": _half_even,
    "toward_zero": _toward_zero, "half_away": _half_away,
}


def _wrap(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


def _sat(v: int, bits: int) -> int:
    hi = (1 << (bits - 1)) - 1
    return max(-hi - 1, min(hi, v))


OVERFLOWS = {"saturate": _sat, "wrap": _wrap}


@dataclass(frozen=True)
class Convention:
    rounding: str
    overflow: str
    #: "sum": quantize x*c - y*s once; "each": quantize each product, then add.
    structure: str

    @property
    def name(self) -> str:
        return f"{self.rounding}/{self.overflow}/{self.structure}"

    def rotate(self, c: int, s: int, x: int, y: int, out_bits: int, out_frac: int) -> tuple[int, int]:
        k = PROD_FRAC - out_frac
        rnd, ovf = ROUNDINGS[self.rounding], OVERFLOWS[self.overflow]
        if self.structure == "sum":
            x1, y1 = rnd(x * c - y * s, k), rnd(x * s + y * c, k)
        else:
            x1 = rnd(x * c, k) - rnd(y * s, k)
            y1 = rnd(x * s, k) + rnd(y * c, k)
        return ovf(x1, out_bits), ovf(y1, out_bits)


CONVENTIONS = [Convention(r, o, s) for s in ("sum", "each") for o in OVERFLOWS for r in ROUNDINGS]


# ---------------------------------------------------------------------------
# Hidden stimulus
# ---------------------------------------------------------------------------


@dataclass
class Tx:
    tx_id: int
    cos: int            #: raw Q10.8
    sin: int
    x: list[int] = field(default_factory=list)   #: raw Q16.8
    y: list[int] = field(default_factory=list)
    label: str = ""

    @property
    def n(self) -> int:
        return len(self.x)


def _q(v: float, bits: int, frac: int) -> int:
    return _sat(round(v * (1 << frac)), bits)


def make_transactions(seed: int) -> list[Tx]:
    """Well-formed transactions: assorted lengths and angles, the corners included.

    The seed is chosen at grading time and nothing is stored, so there is no expected
    output anywhere for an agent to find.
    """
    rng = random.Random(seed)
    lo, hi = -(1 << (IN_BITS - 1)), (1 << (IN_BITS - 1)) - 1

    def samples(n: int, kind: str) -> tuple[list[int], list[int]]:
        if kind == "small":
            return ([rng.randint(-2048, 2047) for _ in range(n)],
                    [rng.randint(-2048, 2047) for _ in range(n)])
        if kind == "corners":
            pool = [lo, hi, 0, -1, 1, lo + 1, hi - 1]
            return [rng.choice(pool) for _ in range(n)], [rng.choice(pool) for _ in range(n)]
        return [rng.randint(lo, hi) for _ in range(n)], [rng.randint(lo, hi) for _ in range(n)]

    txs: list[Tx] = []
    angles = [0.0, 90.0, 180.0, -90.0, 45.0, -135.0, 30.0] + [rng.uniform(-180, 180) for _ in range(5)]
    lengths = [1, 2, 3, 8, 37, 64, 5, 100, 17, 4, 33, 2]
    kinds = ["small", "full", "corners"]
    for i, (deg, n) in enumerate(zip(angles, lengths)):
        th = math.radians(deg)
        c, s = _q(math.cos(th), COEF_BITS, COEF_FRAC), _q(math.sin(th), COEF_BITS, COEF_FRAC)
        x, y = samples(n, kinds[i % 3])
        txs.append(Tx(tx_id=rng.randint(1, 0xFFFE), cos=c, sin=s, x=x, y=y,
                      label=f"{deg:.1f} deg, n={n}, {kinds[i % 3]}"))
    # Exact ties: a coefficient of 0.5 (raw 128) times an odd sample leaves exactly half an
    # LSB, which is where half-up, half-even and half-away rounding part.
    odd = [v for v in (1, 3, 5, -1, -3, -5, 255, -255, 7, -7)]
    for c, s in ((128, 0), (0, 128), (-128, 128)):
        txs.append(Tx(tx_id=rng.randint(1, 0xFFFE), cos=c, sin=s, x=list(odd),
                      y=list(reversed(odd)), label=f"cos={c}, sin={s} raw (rounding ties)"))
    # The format's corners: coefficients outside the unit circle force overflow.
    cmax, cmin = (1 << (COEF_BITS - 1)) - 1, -(1 << (COEF_BITS - 1))
    for c, s in ((cmax, cmin), (cmin, cmax), (cmax, cmax)):
        x, y = samples(9, "corners")
        txs.append(Tx(tx_id=rng.randint(1, 0xFFFE), cos=c, sin=s, x=x, y=y,
                      label=f"cos={c}, sin={s} raw (overflow)"))
    return txs


# ---------------------------------------------------------------------------
# The adapter and the testbench
# ---------------------------------------------------------------------------


def load_adapter(path: str | Path) -> ModuleType:
    path = Path(path).resolve()
    spec = importlib.util.spec_from_file_location(f"_rotate_adapter_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load adapter {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ("SOURCES", "INCLUDE", "WORD_BWS", "TOPS", "OUT_BITS", "OUT_FRAC",
                 "cpp_decls", "cpp_call", "encode", "decode"):
        if not hasattr(mod, name):
            raise AttributeError(f"adapter {path} has no {name}")
    return mod


TB_TEMPLATE = r"""// Generated by the rotate grader.  Plays stimulus words into `in`, calls the kernel at
// every CALL line, drains `out`, and prints the status variables.
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <string>
#include "@INCLUDE@"

namespace grader {
template <class T> auto set_data(T& w, uint64_t v, int) -> decltype(w.data, void()) { w.data = v; }
template <class T> void set_data(T& w, uint64_t v, long) { w = v; }
template <class T> auto set_last(T& w, bool l, int) -> decltype(w.last, void()) { w.last = l; }
template <class T> void set_last(T&, bool, long) {}
template <class T> auto set_keep(T& w, int) -> decltype(w.keep, void()) { w.keep = -1; }
template <class T> void set_keep(T&, long) {}
template <class T> auto set_strb(T& w, int) -> decltype(w.strb, void()) { w.strb = -1; }
template <class T> void set_strb(T&, long) {}
template <class T> auto get_data(const T& w, int) -> decltype(w.data, uint64_t()) {
    return (uint64_t)w.data.to_uint64();
}
template <class T> uint64_t get_data(const T& w, long) { return (uint64_t)w.to_uint64(); }
template <class T> auto get_last(const T& w, int) -> decltype(w.last, int()) { return (int)w.last; }
template <class T> int get_last(const T&, long) { return -1; }
template <class T> auto as_ll(const T& v, int) -> decltype(v.to_int64(), (long long)0) {
    return (long long)v.to_int64();
}
template <class T> long long as_ll(const T& v, long) { return (long long)v; }

template <class T> void push(hls::stream<T>& s, uint64_t v, bool last) {
    T w;
    set_data(w, v, 0);
    set_last(w, last, 0);
    set_keep(w, 0);
    set_strb(w, 0);
    s.write(w);
}
template <class T> void drain(hls::stream<T>& s, FILE* f) {
    while (!s.empty()) {
        T w = s.read();
        fprintf(f, "%llx %d\n", (unsigned long long)get_data(w, 0), get_last(w, 0));
    }
}
}  // namespace grader

int main(int argc, char** argv) {
    if (argc < 3) { fprintf(stderr, "usage: grader_tb <stimulus> <output>\n"); return 2; }
    std::ifstream fin(argv[1]);
    FILE* fo = fopen(argv[2], "w");
    if (!fin || !fo) { fprintf(stderr, "cannot open %s or %s\n", argv[1], argv[2]); return 2; }
    @DECLS@
    std::string tok;
    while (fin >> tok) {
        if (tok == "CALL") {
            @CALL@
            grader::drain(out, fo);
@STATUS@            fprintf(fo, "CALL\n");
            fflush(fo);
            continue;
        }
        unsigned long long v = std::stoull(tok, nullptr, 16);
        int last = 0;
        fin >> last;
        grader::push(in, (uint64_t)v, last != 0);
    }
    fclose(fo);
    return 0;
}
"""


def write_testbench(adapter: ModuleType, word_bw: int, path: Path) -> None:
    status = "".join(
        f'            fprintf(fo, "STATUS {n} %lld\\n", grader::as_ll({n}, 0));\n'
        for n in getattr(adapter, "CPP_STATUS", []))
    text = (TB_TEMPLATE.replace("@INCLUDE@", Path(adapter.INCLUDE).as_posix())
            .replace("@DECLS@", adapter.cpp_decls(word_bw))
            .replace("@CALL@", adapter.cpp_call(word_bw))
            .replace("@STATUS@", status))
    path.write_text(text, encoding="utf-8")


def write_stimulus(calls: list[list[tuple[int, bool]]], path: Path) -> None:
    lines = []
    for call in calls:
        lines += [f"{w:x} {int(bool(last))}" for w, last in call]
        lines.append("CALL")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_output(path: Path) -> list[dict]:
    """Per call: ``{"words": [(word, last), ...], "status": {name: value}}``."""
    calls, cur = [], {"words": [], "status": {}}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "CALL":
            calls.append(cur)
            cur = {"words": [], "status": {}}
        elif parts[0] == "STATUS":
            cur["status"][parts[1]] = int(parts[2])
        else:
            cur["words"].append((int(parts[0], 16), int(parts[1])))
    return calls


TCL_TEMPLATE = """open_project -reset {project}
set_top {top}
{design_files}
add_files -tb {tb} -cflags "{cflags}"
{tb_files}
open_solution -reset sol
set_part {{xc7z020clg484-1}}
create_clock -period 10
if {{[catch {{csim_design -argv "{stim} {out}"}} res]}} {{
    puts "GRADER_CSIM_FAILED"
    puts $res
    exit 1
}}
exit 0
"""


def run_csim(adapter: ModuleType, word_bw: int, calls, work: Path,
             timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """C-simulate the arm's kernel on *calls*; return the recorded calls, or the failure."""
    from waveflow.toolchain.toolchain import _build_final_cmd, _build_vitis_hls_cmd

    # Vitis 2025.1 gets a design file's path wrong unless it lies BELOW the directory Vitis
    # runs in (measured: a nested project, or a source outside the working directory, is
    # silently left out of csim).  So Vitis runs in the adapter's ROOT -- the arm's folder --
    # with a one-level project there and the design files named relative to it.
    root = Path(getattr(adapter, "ROOT", Path(adapter.__file__).resolve().parent)).resolve()
    def under_root(srcs):
        out = []
        for src in srcs:
            p = (Path(src) if Path(src).is_absolute() else root / src).resolve()
            try:
                out.append(p.relative_to(root).as_posix())
            except ValueError:
                raise ValueError(f"adapter source {p} is not under ROOT {root}") from None
        return out

    rel = under_root(adapter.SOURCES)
    tb_rel = under_root(getattr(adapter, "TB_SOURCES", []))
    # CFLAGS may depend on the width (a kernel whose width is a -D macro).
    cflags = getattr(adapter, "CFLAGS", "")
    if callable(cflags):
        cflags = cflags(word_bw)
    # The testbench is a file Vitis compiles too, so it also goes under ROOT.
    tb = root / f"_grader_tb_w{word_bw}.cpp"
    stim, out = work / f"stim_w{word_bw}.txt", work / f"out_w{word_bw}.txt"
    write_testbench(adapter, word_bw, tb)
    write_stimulus(calls, stim)
    if out.exists():
        out.unlink()
    tcl = work / f"grade_w{word_bw}.tcl"
    tcl.write_text(TCL_TEMPLATE.format(
        project=f"_grader_w{word_bw}", top=adapter.TOPS[word_bw],
        design_files="\n".join(f'add_files {r} -cflags "{cflags}"' for r in rel),
        tb=tb.name, cflags=cflags,
        tb_files="\n".join(f'add_files -tb {r} -cflags "{cflags}"' for r in tb_rel),
        stim=stim.as_posix(), out=out.as_posix()),
        encoding="utf-8")
    cmd, _ = _build_vitis_hls_cmd(tcl)
    final, shell = _build_final_cmd(cmd)
    log = work / f"csim_w{word_bw}.log"
    t0 = time.monotonic()
    with log.open("w", encoding="utf-8") as f:
        proc = subprocess.Popen(final, cwd=root, shell=shell, stdout=f, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                proc.kill()
            return {"ok": False, "error": f"csim timed out after {timeout:.0f} s (a kernel "
                    f"waiting on input?)", "log": str(log)}
    result = {"ok": code == 0 and out.exists(), "seconds": round(time.monotonic() - t0, 1),
              "log": str(log)}
    if not result["ok"]:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
        result["error"] = f"csim exit {code}:\n" + "\n".join(tail)
        return result
    result["calls"] = read_output(out)
    return result


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def score(txs: list[Tx], got: list, out_bits: int, out_frac: int) -> dict:
    """For each convention, how many samples match; plus where the best one differs."""
    table = {}
    for conv in CONVENTIONS:
        bad, total, first = 0, 0, []
        for tx, g in zip(txs, got):
            exp = [conv.rotate(tx.cos, tx.sin, a, b, out_bits, out_frac) for a, b in zip(tx.x, tx.y)]
            total += 2 * tx.n
            if g is None:
                bad += 2 * tx.n
                if len(first) < 3:
                    first.append(f"tx {tx.tx_id} ({tx.label}): no (or short) output")
                continue
            for k, ((ex, ey), gx, gy) in enumerate(zip(exp, g[0], g[1])):
                for name, e, v in (("x1", ex, gx), ("y1", ey, gy)):
                    if e != v:
                        bad += 1
                        if len(first) < 3:
                            first.append(f"tx {tx.tx_id} ({tx.label}) sample {k} {name}: "
                                         f"got {v}, expected {e}")
        table[conv.name] = {"mismatches": bad, "samples": total, "first": first}
    matching = [n for n, r in table.items() if r["mismatches"] == 0]
    best = min(table, key=lambda n: table[n]["mismatches"])
    return {"pass": bool(matching), "matching": matching, "best": best,
            "best_mismatches": table[best]["mismatches"], "samples": table[best]["samples"],
            "best_first": table[best]["first"], "table": table}


def grade(adapter_path: str | Path, work: str | Path | None = None, *, seed: int | None = None,
          word_bws: list[int] | None = None, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    adapter = load_adapter(adapter_path)
    work = (Path(work) if work else Path(adapter_path).resolve().parent / "rotate_grade").resolve()
    work.mkdir(parents=True, exist_ok=True)
    seed = seed if seed is not None else random.SystemRandom().randrange(1 << 31)
    txs = make_transactions(seed)
    report = {"adapter": str(Path(adapter_path).resolve()), "seed": seed,
              "transactions": len(txs), "widths": {}}
    for bw in word_bws or list(adapter.WORD_BWS):
        calls = adapter.encode(txs, bw)
        run = run_csim(adapter, bw, calls, work, timeout)
        entry: dict = {"csim": {k: v for k, v in run.items() if k != "calls"}}
        if run["ok"]:
            got = adapter.decode(run["calls"], txs, bw)
            entry.update(score(txs, got, adapter.OUT_BITS, adapter.OUT_FRAC))
        else:
            entry["pass"] = False
        report["widths"][str(bw)] = entry
    report["pass"] = all(e["pass"] for e in report["widths"].values()) and bool(report["widths"])
    (work / "grade.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (work / "grade.md").write_text(render(report), encoding="utf-8")
    return report


def render(report: dict) -> str:
    lines = [f"# Rotate grade: {'PASS' if report['pass'] else 'FAIL'}", "",
             f"Adapter `{report['adapter']}`, seed {report['seed']}, "
             f"{report['transactions']} hidden transactions per width.", ""]
    for bw, e in report["widths"].items():
        lines.append(f"## WORD_BW = {bw}: {'PASS' if e['pass'] else 'FAIL'}")
        if "matching" not in e:
            lines += ["", "C simulation did not complete:", "", "```",
                      e["csim"].get("error", "?"), "```", ""]
            continue
        if e["matching"]:
            lines.append(f"\nBit-exact under: {', '.join(e['matching'])}.")
        else:
            lines.append(f"\nNo convention matches every sample.  Closest: `{e['best']}`, "
                         f"{e['best_mismatches']} of {e['samples']} samples differ:")
            lines += [f"- {m}" for m in e["best_first"]]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--work")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--word-bw", type=int, action="append")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    a = ap.parse_args(argv)
    report = grade(a.adapter, a.work, seed=a.seed, word_bws=a.word_bw, timeout=a.timeout)
    print(render(report))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
