---
title: Tagged unions on a stream
parent: Python
grand_parent: Data Schemas
nav_order: 4.5
audience: python
snippets: run
api: [DataList, EnumField]
summary: "Several kinds of message on one stream -- START, one DET per result, END -- as one DataList per type, each beginning with the same EnumField tag. The way to carry a variable number of results (the count is not known until the end, so the end is in band); the writer serializes each message with its schema, the reader reads the tag and then the rest by that type's schema. Runnable. Each message keeps its own length, unlike a fixed-size DataUnion."
---

# Tagged unions on a stream

When the number of results is not known until a job is done -- detections, matches, peaks -- the results
cannot be headed by a count, so the stream carries **several kinds of message** and the end is **in band**
([Variable-length output](../../systems/command_response.md#variable-length-output-a-tagged-union-ending-in-end)):

```text
m_out : START(tx_id) | DET(index, peak) ... | END(tx_id, count)        one job's messages, one burst
```

Waveflow's form for this is **one `DataList` per message type, each beginning with the same tag**, an
`EnumField`. Each message keeps its own length -- a one-word `START` is one word -- and a reader tells them
apart by the tag, which is always the low bits of a message's first word. (A
[`DataUnion`](./dataunion.md) is the same idea as one schema, fixed-size: every message padded to the longest.)

## The schemas

```python
from enum import IntEnum

import numpy as np

from waveflow.hw.dataschema import DataList, EnumField, IntField

U16 = IntField.specialize(bitwidth=16, signed=False)
U32 = IntField.specialize(bitwidth=32, signed=False)


class MsgType(IntEnum):
    START = 0
    DET = 1
    END = 2


MsgTypeField = EnumField.specialize(enum_type=MsgType, bitwidth=16)


class Start(DataList):
    elements = {"type": MsgTypeField, "tx_id": U16}


class Det(DataList):
    elements = {"type": MsgTypeField, "index": U16, "peak": U32}


class End(DataList):
    elements = {"type": MsgTypeField, "tx_id": U16, "count": U16}


class Tag(DataList):                 # what a reader decodes first: the tag alone
    elements = {"type": MsgTypeField}


SCHEMA = {MsgType.START: Start, MsgType.DET: Det, MsgType.END: End}
```

The tag is the **first field** of every type, so it starts at bit 0 of a message's first word, whatever
the type.

## Writing

Each message is serialized by its own schema, and a job's messages make one burst:

```python
W = 32
msgs = [Start(type=MsgType.START, tx_id=7),
        Det(type=MsgType.DET, index=2, peak=15),
        Det(type=MsgType.DET, index=7, peak=40),
        End(type=MsgType.END, tx_id=7, count=2)]
burst = np.concatenate([m.serialize(word_bw=W) for m in msgs])
print([type(m).__name__ for m in msgs], len(burst), "words")
```

```text
['Start', 'Det', 'Det', 'End'] 7 words
```

`Start` is one word; each `Det` and the `End` are two at 32 bits -- seven in all. In a kernel's `run_iter` the burst is one
`write` of the words (a Python `write` is one burst, and it takes a word array as well as a message):
`yield from self.m_out.write(burst)`. In its HLS body each message goes out as it is made, with
`streamutils::write_msg(msg, s, last)`, `last` true only on the `END`.

## Reading

Read the tag, then the rest by that type's schema, until `END`:

```python
def read_job(words, word_bw=W):
    out, i = [], 0
    while i < len(words):
        t = MsgType(int(Tag().deserialize(words[i:i + 1], word_bw=word_bw).type))
        cls = SCHEMA[t]
        n = cls.nwords_per_inst(word_bw)
        out.append(cls().deserialize(words[i:i + n], word_bw=word_bw))
        i += n
        if t == MsgType.END:
            break
    return out


for m in read_job(burst):
    print(type(m).__name__, {k: int(getattr(m, k)) for k in m.elements if k != "type"})
```

```text
Start {'tx_id': 7}
Det {'index': 2, 'peak': 15}
Det {'index': 7, 'peak': 40}
End {'tx_id': 7, 'count': 2}
```

Every word is decoded by a schema -- the tag by `Tag`, the message by its own -- never by hand. A
reader behind a bus does the same with sized reads: one word for the tag, then the type's
`nwords_per_inst` less one.
