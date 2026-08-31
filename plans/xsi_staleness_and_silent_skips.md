# The XSI staleness guard — and the silent-skip class it belongs to

**Status:** proposed.

A gate that skips looks like a gate that passed.  Three times in one arc a session reported a green
XSI run having measured almost nothing, and each time it was caught by a human noticing a number was
implausible rather than by anything failing.  This plan fixes the specific cause and the general
shape.

## The guard, and why it is right

`waveflow/build/trace_steps.py::rtl_staleness` compares **mtimes**:

```python
newest_rtl = max(mtime of *.v in <top>_proj/solution1/syn/verilog)
sources    = gen/<top>.cpp  +  include/*.{h,hpp,cpp}
if any source mtime > newest_rtl + 1.0:   ->  "stale"
```

and its callers do `_require(rtl_staleness(...) is None, ...)`, which **skips**.

**Skipping is correct and must stay.**  `<top>_proj/` is gitignored build output, so which build
produced it is decided by whatever ran csynth last — possibly on another branch.  The docstring
cites two real incidents: a `<Latency>undef</Latency>` read from a branch that pipelined the bodies,
and three XSI gates reporting off-by-one counts (4211 vs 4210, 15442 vs 15441) against RTL three days
old, where rebuilding restored the recorded numbers exactly.  Both times **the number was right and
the artifact was wrong**.  A cycle gate that shouts "behaviour change, re-record it" while looking at
someone else's RTL invites the one edit that must never be made casually.

So: do **not** make it fail.  The problem is elsewhere.

## Defect 1 — mtime answers the wrong question

The real question is *"was this RTL built from this source?"*  mtime is only a proxy for it, and the
proxy is wrong in both directions that matter:

| | mtime today | a content hash |
|---|---|---|
| `--force` regeneration, byte-identical output | **stale (WRONG)** | not stale |
| branch switch restoring identical content | **stale (WRONG)** | not stale |
| branch switch changing content | stale | stale |
| genuinely edited source | stale | stale |

The first row is the one that bit twice.  Regenerating artifacts with `--force` rewrites
`gen/*.cpp` with **identical bytes and new mtimes**, so:

* content is unchanged -> `git status` clean -> nothing looks wrong
* mtime is newer -> guard fires -> every affected gate **skips**
* pytest prints `40 passed, 23 skipped` -> reads as success

Which is perverse in a specific way worth naming: **proving byte-identity is what invalidates the
gates that would prove correctness.**  The artifact gate and the XSI gate disable each other, and
the run still looks green.

(`git checkout` also stamps files with checkout time, so today a branch switch alone can trigger it.)

## Defect 2 — a skip is indistinguishable from a pass

`23 skipped` in a summary line is not a signal anybody reads under time pressure.  Nothing asserts
that a `-m xsi` run actually ran anything.

## Defect 3 — most gates are not guarded at all

Measured: **9 XSI gate files, 4 guarded.**

```
GUARDED      rf_blk_delay  rf_circ_play  rf_samp_buf_rx  rf_samp_buf_tx
UNGUARDED    bram_access  fir_block  rf_loopback  rf_relayout  rf_shot_buf
```

So five gates will happily measure against RTL they did not produce and report a cycle count as a
behaviour change — the exact failure the guard exists to prevent.

**These three defects interact.**  An mtime guard that false-positives on a no-op regeneration is one
you would hesitate to apply broadly, so fixing Defect 1 is what makes fixing Defect 3 safe.

---

## S1 — hash the sources, do not stat them

Stamp a digest of the generating sources into the build output at csynth time; compare digests.

**One hook, not fifteen.**  There are 15 per-example build scripts with their own `CSynthStep`, but
**11 call the shared `render_rtl_f`** in `waveflow/build/composite_gen.py`, which already runs after
csynth to re-emit `rtl_<wrapper>.f` from the RTL on disk.  That is the place: emit a sibling digest
file there and every example gets it at once.

Design notes:

* Hash the **same source set the guard reads today** — `gen/<top>.cpp` plus `include/*.{h,hpp,cpp}` —
  so behaviour changes only where mtime and content disagree.
* Missing digest (an older build tree) must mean **fall back to the mtime check**, not "clean".
  A tree built before this change must not silently become unguarded.
* The refusal message keeps its current shape and its "do NOT re-record a cycle count against RTL you
  did not produce" line, which is the sentence that does the work.

## S2 — a floor assertion for the whole silent-skip class

Four lines, and it catches the next variant rather than this instance: a `-m xsi` session must run at
least *N* gates, where *N* is recorded the way `WANT_CYCLES` is.  If gates skip, the run **fails** —
not because the skip is wrong, but because a session that measured nothing must not report success.

This is deliberately separate from S1.  S1 removes the *false* skips; S2 makes a *true* skip visible.
Both are needed: a genuinely stale tree should still skip, and still not look green.

## S3 — extend the guard to the five unguarded gates

Safe only after S1.  Adding today's mtime check to `bram_access` would make it skip constantly, which
is presumably why it was never added.

---

## Gates

1. **The reproduction, first.**  Before changing anything: regenerate with `--force`, confirm
   `git status` is clean, and confirm the XSI gates skip.  That is the bug; it must be demonstrated
   before it is fixed and gone afterwards.
2. **After S1** the same sequence must leave the gates **running**, and the recorded cycle counts
   must be unchanged.  `WANT_CYCLES` is not to be touched.
3. **A genuinely stale tree must still skip.**  Edit a source, do not re-synthesize, confirm the
   guard fires with its message intact.  S1 must not weaken the guard, only sharpen it.
4. **After S2**, a run in which anything skips must FAIL, and the failure must name what skipped.
5. Baseline 6, no collection errors.

## Related

The same class, elsewhere in this arc:

* `--force` regeneration silently skipping every `rf_*` gate (twice).
* A suite gate written as `grep -c "^FAILED"` reporting **zero** failures against a baseline of six,
  because a collection error produces no `FAILED` lines at all.  Any future gate that counts failures
  must also assert that collection succeeded.
