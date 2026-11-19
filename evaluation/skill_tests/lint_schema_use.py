"""lint_schema_use.py -- point a reviewer at the lines worth reading in a skill-test trial folder.

    python lint_schema_use.py <trial folder>

It does not score anything.  It lists every line, in the code the agent wrote (generated headers
skipped), that matches a pattern the waveflow-schema skill forbids: hand packing, defensive casts,
hand-computed word counts.  A hit is a place to look, not a verdict -- a shift inside the arithmetic
of a model is fine; a shift that pulls a field out of a word is not.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

GENERATED = re.compile(r"(^|[\\/])include[\\/]|_array_utils(_tb)?\.h$|^streamutils|_tb\.h$")

PATTERNS = {
    "cpp": [
        ("range",       re.compile(r"\.range\s*\(")),
        ("shift",       re.compile(r"(<<|>>)\s*[\w(]")),     # not the `>>&` of a nested template
        ("mask",        re.compile(r"&\s*0x[0-9a-fA-F]+|&\s*\(\(1\s*<<")),
        ("raw word",    re.compile(r"\.read\(\)\s*\.data|\.data\s*=|axi4s_word<[^>]*>\s+\w+\s*;")),
        ("enum as int", re.compile(r"\b(op|kind|type|cmd_type)\s*==\s*\d")),
    ],
    "py": [
        ("shift",       re.compile(r"(<<|>>)(?!=)")),
        ("mask",        re.compile(r"&\s*0x[0-9a-fA-F]+|&\s*\(\(1\s*<<|&\s*\(2\s*\*\*")),
        ("bytes",       re.compile(r"from_bytes|to_bytes|frombuffer|struct\.(un)?pack")),
        ("int(field)",  re.compile(r"\bint\(\s*\w+\.\w+\s*\)")),
        ("asarray",     re.compile(r"np\.asarray\(")),
        ("word count",  re.compile(r"ceil\(|//\s*(word_bw|W)\b|bit_length\(\)")),
    ],
}
KIND = {".py": "py", ".cpp": "cpp", ".h": "cpp", ".hpp": "cpp", ".tpp": "cpp", ".cc": "cpp"}


def scan(folder: Path) -> int:
    hits = 0
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        kind = KIND.get(path.suffix)
        if kind is None or not path.is_file() or GENERATED.search(str(rel)):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            code = line.split("//")[0] if kind == "cpp" else line.split("#")[0]
            for name, pat in PATTERNS[kind]:
                if pat.search(code):
                    print(f"{rel}:{n}: [{name}] {line.strip()}")
                    hits += 1
    return hits


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    n = scan(Path(sys.argv[1]))
    print(f"\n{n} line(s) to look at")
