# Benchmark results — per driving model

Same cards (`bench/cards.json`), same machine profile, one row per
driving model. The executor's promise is the last column: **silent
failures = 0 for every model**, including the models that fail tasks.
A "fail" here is a task the model did not complete; the run still
ended in DONE or a named structured abort, inside its budget, with a
full timing summary.

## Nov 8, 2026 — first suite run (ArK2 desktop, live state)

| Model | B1 browser | N1 native | V2 vnc (flagship) | Silent failures |
|---|---|---|---|---|
| Qwen3.8-27B local, via this executor | fail (37.0s) | fail (37.2s) | fail (44.8s, ABORTED_CAP) | **0** |
| GPT-6.1 Sol via Codex as agentic driver (cua toolset, not this executor's loop) | — | — | **PASS (159s, first attempt)** | 0 |
| Human-driven reference (operator driving cua directly) | pass | pass | pass | — |

Notes on this run — read before quoting the local-model row:

- The local model's three failures were all the same shape: the model listed the
  working directory in the shell, then concluded "no explicit task
  steps provided" and stopped, despite the task text being in its
  prompt. The **untouched production executor produced the identical
  surrender** in a same-day A/B control run, so this is a model /
  harness-state result, not a release-build regression.
- The same model passed B1-class and N1 tasks on Nov 7, 2026 on the
  same machine (browser 8.7–9.4s correct; native 17.3s, file
  byte-exact) under a fresher desktop state. Its GUI reliability is
  high-variance; the table prints the run we measured, not the best
  run we have seen. One unverified suspicion, flagged as such: the
  long-lived local model server had served dozens of runs that day,
  and its prompt-prefix cache may be part of the story.
- The Codex row is from a parallel experiment the same day (Codex
  driving the desktop directly with the cua toolset, vision input,
  17 cua actions, both V2 editor titles independently verified). It is
  evidence for the per-model thesis: the executor's job is to make any
  model's work accountable; a stronger driver passes the flagship card
  on the same desktop.
- The human reference row is the operator's own cua driving during
  the project's Phase-1 GUI work (schematic captures verified frame
  by frame).

Machine-readable runs: `bench/results/qwen38-27b-local.json`,
`bench/results/qwen38-27b-local-v2.json` (from this run; the V2 card used a
site runbook pack kept outside this repo, as packs are meant to be).
