# Design: the adaptive peak picker

```text
s_in --> [StreamBroadcast(2)] --copy_stats (16)--> [Stats] --thr (4)--> [Detect] --> m_out, m_status
                              --copy_detect (16)------------------------^
```

`PeakPicker` is a composite kernel: three children, three framed internal links, no body. Each stage
is a leaf `FreeRunMod`, one firing per job.

| link | from -> to | depth (words) | why |
|---|---|---|---|
| `copy_stats` | broadcast -> Stats | 16 | smooths the rates; Stats reads its copy as it comes |
| `copy_detect` | broadcast -> Detect | 16 | smooths the rates; Detect drains its copy before it waits (below), so it need not hold a job |
| `thr` | Stats -> Detect | 4 | a token is one word; a few let Stats run ahead by a job or two |

## Choices where the spec left one open

- **Detect drains its copy before it waits for `T`.** `T` exists only once Stats has read the whole
  job, and the broadcast's outputs move in lockstep: if Detect waited for `T` with the job's samples
  still in its copy, the copy would fill, the broadcast would stall, and Stats would never finish. So
  Detect reads the job's samples into a local buffer (at most 1024, 2 KB) first. The alternative, a
  `copy_detect` deep enough for a whole job (513 words), costs a FIFO of that size instead of the
  buffer; either works, the depth must not be left at a default. Note: pysim does not show this
  deadlock (a long burst is admitted to a short link), so the order is checked by reading the code,
  and a strict-xfail test records the gap.
- **A token per job.** Stats sends `PkThr(tx_id, thr, code)` for every job, including a bad request
  (`BAD_LEN`) and a short burst (`SHORT`), so Detect always has one to wait for. Detect checks the
  token's `tx_id` against its own copy of the command.
- **The last stage answers.** Detect writes `PkResp(tx_id, count)`, then the indices as one burst only
  when `count > 0` (a zero-length burst cannot be sent), then `PkStatus(tx_id, code)`.
- **Errors are fatal.** Both stages stop after an error; Detect answers it with `count = 0` and the
  code.
