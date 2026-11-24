# Design: three blocks

All three are `FreeRunMod`s: none is launched by anyone. Their logic reads and writes only streams;
what reaches a block over the bus is declared in its `mm_views` and built by the system, not here.

## Block A: contrast stretch

| interface (as described) | declared | outside |
|---|---|---|
| rows from upstream, each with `lo`, `g` and its pixels | `s_in`, framed slave: `RowCmd(tx_id, n, lo, g)` then the pixels (`U8`, 4 a word) | the upstream block's stream |
| processed rows to downstream | `m_out`, framed master: `RowResp(tx_id, n)` then the results | the downstream block |
| "whether each row was processed correctly" | `m_status`, framed master: `RowStatus(tx_id, code)`, one per row after its results | whoever watches status |

**Choices:**

- **The settings and the pixels share `s_in`** (each row's command carries `lo`, `g` and `n`, then its
  pixels). The description did not say how they travel; one stream keeps them in one order and the
  header says where the pixels end. A separate settings stream would be needed only if the settings
  came from another block or were too large to repeat with each row.
- `tx_id` added to the command and echoed, so downstream pairs results and status with rows.
- Status on its own stream, not mixed into the results, so downstream's data path needs no parser.
- **Errors are fatal**: `g == 0` is reported (`BAD_GAIN`, no results sent, the header already read)
  and a pixel burst that ends early is closed short and reported (`SHORT`); either way the block then
  stops reading `s_in` until reset.

## Block B: burst energy monitor

| interface (as described) | declared | outside |
|---|---|---|
| frames of 256 complex samples from the ADC | `s_samp`, framed slave of `Complex<int16>` (I low) | the ADC's stream |
| host sets `threshold`, `shift` at any time | `s_cfg`, slave: `MonCfg(threshold, shift)`, one per commit | `RegBank("regs", cfg_port="s_cfg", ...)` |
| reports to the host | `m_report`, unframed master: `Report(frame, energy)`, fixed size | `QueueOut("q_report")` |
| host woken when a report waits, no polling | nothing in the module | the queue view's `IrqIF` (level, threshold), waited on by the host's endpoint |
| host reads frames processed and last fault | `m_status`, master: `MonStatus(nframes, fault)` after every frame | the same `RegBank` (`status_port`; it keeps the latest) |
| interrupt-enable and clear-on-write status registers | **not built in Waveflow** | see below |

**Choices:**

- **No command stream.** Nothing but the ADC starts a frame; the frame is the job, numbered from
  power-up. The samples and the settings come from different sources, so they are two streams.
- **When a setting is taken:** before each frame's first sample, a non-blocking check of `s_cfg`
  (`get_schema_nb`), taking the latest committed. A frame never sees a mix. Before the first frame a
  blocking read: nothing is processed until the host has set it once (the ADC is back-pressured).
  **The window that remains:** a commit that lands while the monitor already waits for the next
  frame's samples applies one frame late. With frames back to back the window is empty; closing it
  exactly needs a check at the frame's first word, which pysim does not document (`begin_read` exists
  but is not in the guide). An HLS body closes it with `read_nb` on both streams in one loop.
- `energy` is 64-bit in the report: with a small `shift` the sum exceeds 32 bits.
- A short frame is fatal: `MonStatus(nframes, SHORT_FRAME)` and the monitor stops.
- **Not built:** Waveflow's queue interrupt is a level line, high while a threshold of words waits, the
  threshold managed by the host's endpoint; there are no interrupt-enable or clear-on-write status
  registers (`interface/axi_mm/slave.md`, "What is not built yet"). I used the level interrupt and did
  not imitate the registers.

## Block C: correlator

| interface (as described) | declared | outside |
|---|---|---|
| templates from the controller | `s_tmpl`, framed slave: `TmplHdr(tmpl_id, ntaps)` then the taps | the controller |
| jobs from the controller | `s_job`, framed slave: `JobCmd(tx_id, tmpl_id, n)` then the samples | the controller |
| results to downstream | `m_out`, framed master: `JobResp(tx_id, tmpl_id, nout)` then `S32` results | downstream |
| (status, added) | `m_status`, framed master: `JobStatus(tx_id, code)` per job | whoever watches status |

**Choices:**

- **Two input streams, as asked.** They have no order between them, so **a template carries an id and
  a job names the id it needs**; the correlator reads templates only until it holds that one, and the
  response echoes the id used. A template sent while earlier jobs wait stays queued in `s_tmpl`. A
  count of templates at both ends was rejected: it drifts silently on the first lost message.
- Template ids increase; a job naming a replaced template is the sender's bug.
- Errors fatal: `n < L` (`TOO_SHORT`, no results) and a short sample burst (`SHORT_BURST`).
