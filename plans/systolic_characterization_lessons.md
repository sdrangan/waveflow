# Lessons: systolic characterization

- **A long build path fails csynth with no error message.** Under the session scratchpad, csynth of
  the systolic tops ended in `Synthesis failed.` and nothing else; the flow log
  (`.autopilot/db/autopilot.flow.log`) showed `syn_report` failing on a 266-character report path
  (Windows `MAX_PATH` is 260). Long task names (`systolic_rx_task_64_8_8_32_4_4_8_1556958752_...`)
  make it likely. Build from a short root (`C:\wfsc\`). When csynth fails silently, read the flow log
  for the last `Execute` before `error code`.
- **A FIFO shallower than one message can serialize a whole pipeline.** The receiver writes a
  3-word reply header into a 2-deep channel to a store that only reads it at the start of its next
  firing; so job *n+1*'s payload waited for job *n*'s output. Ping-pong blocks everywhere else did not
  help. Size every command / header channel to at least one message, and check it in a trace: an input
  that stalls until the previous output's last word is the signature.
- **Look stream-fed first.** The memory-fed number (720, "transfers add") had been explained by the
  `m_axi` pointer FIFOs. Fed straight from streams the unit was slower (821), which moved the search
  inside the unit in one run.
- **A unit that was only ever a child is not ready to be a top.** Making `SystolicUnit` its own
  harness's DUT exposed four gaps (formats not `HwParam`s, unframed boundary endpoints, bodies not
  templated on the boundary word type, a system top named like the DUT). Harness every module early.
- **A wrong explanation can sit in a docstring for months.** The memory-fed "transfers add" was
  explained by the `m_axi` pointer FIFOs; one memory-fed run of four identical jobs after the fix gave
  390 = the stream-fed interval. Test a stated mechanism the cheap way (change one thing, rerun) before
  quoting it.
- **The RTL ping-pong is one block ahead, not two.** Vitis 2025.1's `stream_of_blocks<.., 2>` drops
  `i_full_n` after one committed block, and a commit waits for it: a writer may fill block n+1 while
  the reader holds n, but releases it only after the reader frees n. pysim's model allows two. A fit
  must exclude commits the reader held (`i_full_n` low the cycle before).
- **Collinear features fit fine and mean nothing.** Within one configuration a tile's output groups
  are a constant times the tile count; with both as features the split was arbitrary (0.28 / 2.24).
- **A DAG step that is "fresh" files nothing.** Deleting a bad record and rerunning skipped the fit
  (its report existed); force the step (`force=["calib_phases"]`).
