---
title: Extractor
parent: Module Code Generation
nav_order: 10
audience: hls
api: [HwStmtExtractor, extract_kernel, extract_testbench, SynthesisError, check]
summary: "Extraction reads a component's entry method as source (it never runs it), parses it, and translates it into HwStmt — a small hardware IR of ~25 statement types that sits between the Python and the C++. Only a limited set of Python statements and a fixed vocabulary of endpoint operations can be extracted today; anything else raises SynthesisError rather than emitting doubtful C++. check(source, target) is the same rules as a predicate."
---

# Extractor

## What extraction is

**Extraction is reading your Python and rewriting it as hardware.** The generator takes the
component's entry method — `on_start`, `run_iter`, or a testbench's `main` — and translates it
statement by statement into a form the C++ emitter understands.

The important part: **extraction never runs your method.** It reads the *source text*, parses it, and
works on the syntax tree. So the generator sees `y = self.compute(x)` as a *shape* — an assignment
whose value is a call — and never as a value. This is why the accepted subset is small and why it is
defined syntactically: the generator only knows what it can *read*.

## The sequence: Python → HwStmt → C++

Extraction is the middle of a three-step pipeline:

```
your method  ──parse──>  Python AST  ──extract──>  HwStmt  ──emit──>  C++
                                        (rules)     (IR)
```

1. **Parse.** `inspect.getsource(method)` reads the method's text and `ast.parse` turns it into a
   Python **AST** (abstract syntax tree) — Python's own structural representation of the code.
2. **Extract.** [`HwStmtExtractor`](../../../waveflow/build/hwcodegen.py) walks that tree and builds an
   **`HwStmt`** tree. This is where every rule is enforced: anything it cannot translate raises
   `SynthesisError` here, before a line of C++ exists.
3. **Resolve.** A second pass replaces leftover AST nodes with the real Python values they refer to and
   types each variable — turning a *reference* to `MAX_NDATA` into the number `1024`.
4. **Emit.** [Codegen](./codegen.md) walks the resolved `HwStmt` tree and writes C++.

### What an IR is, and why there is one

An **IR** — *intermediate representation* — is a deliberately small language sitting between the input
and the output. `HwStmt` is Waveflow's: about 25 statement types
([`waveflow/hw/hwstmt.py`](../../../waveflow/hw/hwstmt.py)) such as `SeqStmt` (do these in order),
`WhileStmt`, `CaseStmt`, `FunctionStmt` (call a hook), `MMArrayReadStmt` (burst-read from memory), and
`KernelCallStmt` (a testbench invoking the DUT).

It is **neither Python nor C++** — it is the set of things hardware can do, which both can be mapped
onto. Two reasons it earns its place:

- **The rules are checked exactly once.** By the time the emitter runs, the tree is already known to be
  translatable, so the emitter contains no validation and cannot disagree with the extractor about what
  is legal.
- **One input, many outputs.** The same `HwStmt` tree is what lets one component lower to more than one
  [target](./index.md) without re-reading the Python.

## What can be extracted today

**Only a limited set of Python, plus a fixed vocabulary of endpoint operations.** This is today's
extent, not a ceiling — it grows as targets and patterns need it. Everything below is verified against
the current extractor.

The mental model that matters: this is **not "Python minus a few features"**. The extractor recognizes
a *fixed list of shapes*. If a statement is not on the list, it is rejected — not attempted.

### Statements

| Python | Status |
|---|---|
| `y = <call>` — plain assignment | **accepted** |
| `x: T = yield from <endpoint call>` — annotated binding | **accepted** (annotation allowed *only* with `yield from`) |
| a bare call, e.g. `self.regmap.set("y", y)` | **accepted** |
| `if` / `elif` / `else` | **accepted** (see conditions) |
| `return`, `return <local>` | **accepted** |
| `while True:` + `continue` | **accepted** — the free-running loop |
| a docstring | **accepted** (ignored) |
| `while <condition>:` | rejected — only `while True:` |
| `for` | rejected |
| `break` | rejected |
| `pass` | rejected |
| `try` / `except`, `with`, `assert`, `raise`, `del` | rejected |
| `y += 1` — augmented assignment | rejected |
| `x: T = <call>` — annotation without `yield from` | rejected |

Two of these surprise people. **`pass` is rejected** — it is not a no-op to the extractor, it is an
unrecognized statement type. And **`break` is rejected**, so a `while True:` body has no exit: that is
the point, it is a block that runs forever, re-firing per job.

### Conditions

Conditions lower to a `CaseStmt`, which today handles **equality against a constant only**:

| Condition | Status |
|---|---|
| `if x == 0:` / `if x != 0:` | **accepted** |
| `if x == SomeEnum.MEMBER:` | **accepted** |
| `if x > 0:` / `if x >= 0:` | rejected — only `==` and `!=` |
| `if a and b:` / `if not a:` | rejected |

This is why kernels are written to compare a status against a constant rather than range-check a value.
Richer conditions need an expression IR, which is deliberate future work — `hist` works around it by
making a read unconditional (a zero-length burst is a no-op) rather than guarding it with `>`.

### Endpoint operations

Beyond plain statements, the extractor recognizes a fixed vocabulary of **operations on endpoints** —
each in a rigid call shape, because each maps to one IR node:

- **Streams** — `yield from ep.get(Schema)`, `yield from ep.get(Schema, count=n)`, `yield from
  ep.write(value)`
- **Register maps** — `self.regmap.get("field")`, `self.regmap.set("field", value)`
- **Memory** — `read_array` / `write_array` on an `m_axi` master, which lower to burst reads and writes
- **Hooks** — a call to any [`@synthesizable`](../custom_hooks/) method
- **Testbench-only** — `dut.run()`, `dut.run_once(...)`, `ep.push(v)` / `ep.pop(v)`, schema file I/O,
  `mem.alloc_array(...)`

Their argument shapes are strict — `mem.alloc_array(buf, ElemT, count=…)` and nothing else — and most
of the extractor's error messages are about exactly this. The strictness is the price of each call
mapping onto one IR node with no inference.

## The rules that are not about syntax

Four rules are about *meaning* rather than statement shape. These are the ones worth internalising:

**No reads of *undeclared* mutable `self.X`.** A kernel body may read its arguments, its endpoints, its
reg-map, its `HwParam` values, `DataSchema` types, and anything declared with `add_state`. It may **not**
read other mutable instance state:

> `Implicit capture of 'self.gain' at line 2. Reads of self.X inside a synthesizable method are
> forbidden unless 'X' is @sim_only, an endpoint, or a RegMap. Mark the value @sim_only, pass it
> explicitly, or — if it is storage that must persist across firings — declare it with
> self.add_state(self.gain).`

The reason is that `self.gain` is a Python value at *elaboration* time; in hardware it is either a
constant baked into the design or a register someone must write. Silently choosing one would be
guessing, so the extractor makes you say which.

**Declaring state.** `add_state` is how you say "this one is a register file":

```python
self.taps = HwState(TapArray())       # TapArray: a DataArray with cpp_storage="raw"
self.add_state(self.taps)
```

The rule is not relaxed — an undeclared `self.X` is still rejected. What changes is that there is now a
way to answer it. A declared object may be read at a hook call site, where it lowers to its bare
attribute name, and codegen emits persistent storage for it: a `static` at the top of the kernel
function for a [control-driven kernel](../flows/sequential.md), and at the top of the generated
`hls::task` body for a [free-running](../flows/concurrent.md) leaf — where it is the only place
persistent storage *can* live, since a task has no "before the loop".

One detail worth knowing: the C++ type comes from the **registered instance**, not from the hook's
annotation, so state whose element format was built per instance (a `FixedField` specialized off a
`HwParam`) emits the format it actually has. That is why a hook argument can simply be annotated
`HwState`.

[`HwState`](../memory/hwstate.md) is the full story — what it emits at each site, partitioning, and
why read/write permission is *not* declared on the storage.

**Only `@synthesizable` calls.** A call to a plain method is rejected — mark it `@synthesizable` to make
it a hook, or `@sim_only` to have its calls stripped from the kernel entirely (that is how
`self.timeout(...)` models latency in simulation and emits nothing).

**No concurrency.** Spawning a SimPy process (`env.process(...)`) is rejected, because every target this
extractor feeds is sequential — there is no straight-line lowering for a fork. The message points at the
[concurrent (free-running)](../flows/concurrent.md) flow, whose XSI BFM drives every port each cycle.
Honest limit: this is a **gate, not a proof**. It rejects the
syntax that certainly implies concurrency; it cannot certify that a body is sequential.

**A leaf must be structurally flat.** A component that lowers to one kernel function may not own
sub-components or internal interfaces — a single function has nowhere to put them. See the
[contract](./structure.md).

## Asking without generating

Every rule above raises `SynthesisError` during extraction, which is what makes generation fail-loud.
To ask the same question *without* generating, use [`check`](./index.md):

```python
>>> from waveflow.build.codegen_check import check
>>> check(SimpFun)
(True, None)
```

`check` runs this same extractor and turns the raise into a verdict. It carries no rules of its own, so
it cannot report a rule the generator does not enforce, nor miss one it does — **a rule added here is
reported by `check` for free.**

## See also

- [Module structure](./structure.md) — the contract for when a component lowers at all.
- [Custom Hooks](../custom_hooks/) — writing the bodies the extractor deliberately does not translate.
- [Codegen](./codegen.md) — what the emitter does with the resolved `HwStmt` tree.
