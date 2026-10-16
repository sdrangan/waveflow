---
title: Decisions your spec must make
parent: Streaming polynomial
nav_order: 4
summary: "A checklist for adapting the command-response contract to your own accelerator: where configuration lives, what zero-length data means, which error wins, how fields pack into words at each width, rounding and saturation for fixed point, what tx_id means in the status, TLAST on fixed-length bursts, and unused opcode values -- each with the answer this example chose and why."
---

# Decisions your spec must make

What must your own spec decide before you build it?  The [contract](./index.md#the-contract) fixes
the shape of the protocol.  It leaves some choices to each design, and a spec that leaves them
unstated gets them decided by accident, differently in the Python model and in the C++.  Decide each
one in writing before Stage 2.  Each item below gives the question, this example's answer, and why.

**1. Where does the configuration live?**
*Here:* in every `DATA` header (four float32 coefficients).
*Why:* it is small, so repeating it costs little, and it keeps every command self-contained (rule 4).
*Choose otherwise when* the configuration is large compared with a command's data.  Then use a
`CONFIG` command, and add an error for `DATA` before any `CONFIG`.  See
[Configuration that persists](./why_not.md#configuration-that-persists).

**2. Does every command get a response?**
*Here:* `DATA` gets `RespHdr | results`.  `END` gets nothing.
*Why:* the host counts its `DATA` commands to know how many responses to expect, so it never waits
for a response that will not come.

**3. What does zero-length data mean?**
*Here:* `nsamp = 0` is legal.  The command has no sample burst, and the response is the header alone.
*Why:* a host that splits a stream into commands may produce an empty one at a boundary, and treating
it as an error would make the host special-case it.  Either answer is defensible, but you must pick
one, and say whether the empty burst is omitted or sent as a single word.

**4. Which error wins when several apply?**
*Here:* the first one detected, in stream order, and it ends the run.  The two framing errors cannot
both happen in one burst.
*Why:* "first detected" is what the hardware does naturally.  If your kernel validates parameters
(for example `lo > hi`), say whether that check happens before any output is written for the command.
Then a bad command produces no output at all.

**5. How are fields packed into words, at each width?**
*Here:* by the schemas, through `serialize()`.  The command header is 6 words at 32 bits and 3 at
64.  Samples are one per word at 32 bits and two at 64, element 0 in the low half.  An odd `nsamp`
leaves the high half of the last word zero.
*Why:* the layout must come from the code that packs it, not from reasoning about the schema.  Write
the layout table by serializing instances, as [Protocol and interfaces](./protocol.md#word-layouts)
does.  Say what fills unused halves, and whether the kernel ignores them.

**6. Rounding and saturation (fixed point).**
*Here:* not applicable.  The arithmetic is float32 in Horner order, one rounding per operation, and
the C++ keeps each multiply and add a separate statement so they cannot fuse.
*In a fixed-point design, say exactly:* the rounding mode (round half up? half to even? truncate?),
where it is applied, whether results saturate or wrap, and the width of every intermediate.  The
Python model and the C++ agree bit for bit only if all of these are written down.

**7. What does `tx_id` mean in the status?**
*Here:* the `tx_id` of the `DATA` command that failed.  It is 0 after a successful run.
*Why:* it tells the host where to resume after the reset.  If a command that is not `DATA` can fail
(a `CONFIG` with illegal parameters, an error before any `DATA`), every command must carry a `tx_id`,
or the spec must say what the status holds instead.

**8. Is TLAST checked on fixed-length bursts?**
*Here:* no.  Headers have a schema-fixed length and are read without looking at TLAST.  Only the
variable-length sample burst is checked.
*Why:* TLAST is what frames a variable-length burst.  For a fixed-length one, the schema already
does.  Checking it costs logic and adds two error codes that a well-behaved host never triggers.

**9. What do unused opcode values do?**
*Here:* there are none.  With two commands, `cmd_type` is a 1-bit field, and both values are valid.
*Otherwise:* if the opcode field has values that name no command, decide whether they are an error
(with its own code) or treated as `END`.  Note that a Waveflow schema refuses to build or parse an
out-of-range enum value.  A host built from the same schema cannot send one, and a scenario cannot
test one without packing words by hand.

**10. Is there a footer?**
*Here:* no.  Nothing about the response is known only after the data.
*Otherwise:* see [Adding a footer](./protocol.md#adding-a-footer).  Prefer the header, send the footer
only on success, and make it a fixed-length schema in its own burst.

## Check your understanding

1. Your kernel computes a running sum that must reset at every command.  Which item does that
   belong to, and what does rule 4 say about it?
2. A gain-and-clip kernel takes `lo` and `hi` bounds.  Write the sentence your spec needs for item 4.
3. At 64 bits, how many words does a `DATA` command with `nsamp = 5` send, and what is in the
   unused half?

---

Next: [Python model →](./01_python_golden_model.md)
