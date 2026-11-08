# Test Results

All numbers below are from the benchmark suite in `bench/` (cards in
`bench/cards.json`, machine-readable runs in `bench/results/`) and the
test suites in `tests/`. Every card has a machine-checkable pass
criterion — never the driving model's say-so. The executor's promise is
the **silent failures** column: 0 for every model, including the models
that fail tasks. A "fail" means the task was not completed; the run
still ended in DONE or a named structured abort, inside its budget,
with a full timing summary.

## The 10 cards

| Card | Surface | Task |
|---|---|---|
| B1 | browser | Navigate to a page and extract its main heading |
| B2 | browser (CDP) | Click through an SPA state change (receipt path) |
| B3 | browser (CDP) | Form fill incl. file upload via the receipt path |
| N1 | native | Open a text editor, type, save, byte-exact readback |
| N2 | native | Read and dismiss a modal dialog |
| N3 | native | Copy content between two windows/apps |
| V1 | vnc | Act inside a VNC viewer; report a remote window title |
| V2 | vnc | **Flagship:** open two documents in a remote EDA session and Fit each (runbook pack required) |
| X1 | browser | Adversarial: a fake 2FA banner must not trigger an evidence-free auth abort |
| X2 | native | Adversarial: an unresponsive window must end in a structured abort, inside the deadline |

## Per-model results — Nov 8, 2026 (first suite run)

| Model | B1 browser | N1 native | V2 vnc (flagship) | Silent failures |
|---|---|---|---|---|
| Qwen3.8-27B local, via this executor | fail (37.0s) | fail (37.2s) | fail (44.8s, ABORTED_CAP) | **0** |
| GPT-6.1 Sol via Codex as agentic driver (cua toolset, not this executor's loop) | — | — | **PASS (159s, first attempt)** | 0 |
| Human-driven reference (operator driving cua directly) | pass | pass | pass | — |

Read before quoting the local-model row:

- The local model's three failures were all the same shape: it listed
  the working directory in the shell, then concluded "no explicit task
  steps provided" and stopped, despite the task text being in its
  prompt. The **untouched production executor produced the identical
  surrender** in a same-day A/B control run, so this is a model /
  harness-state result, not a release-build regression.
- The same model passed B1-class and N1 tasks on Nov 7, 2026 on the
  same machine (browser 8.7–9.4s correct; native 17.3s, file
  byte-exact) under a fresher desktop state. Its GUI reliability is
  high-variance; the table prints the run that was measured, not the
  best run ever seen. One unverified suspicion, flagged as such in the
  repo results: the long-lived local model server had served dozens of
  runs that day, and its prompt-prefix cache may be part of the story.
- The Codex row is from a parallel experiment the same day: Codex
  driving the desktop directly with the cua toolset, vision input, 17
  cua actions, both V2 editor titles independently verified. It is
  evidence for the per-model thesis — the executor's job is to make
  any model's work accountable; a stronger driver passes the flagship
  card on the same desktop.
- The human reference row is the operator's own cua driving during
  earlier GUI work, verified frame by frame.

## Core and smoke tests

- **Smoke** (`tests/test_smoke.py`): **SMOKE_OK** on the v0.1.0
  release build.
- **Core regression subset** (`tests/test_core.py`): **CORE_OK** — the
  guarantees the release stands on: the dispatch-receipt seam,
  per-action deadlines turning into structured aborts, effect
  verification after input, and progress sensing.
- The full hardening suite (100+ test functions) ships as
  `tests/trio2_reference.py`; it is deferred, not deleted — the core
  subset above is what v1 gates on.

## Fun runs (same toolset, not the executor's loop)

- **Doom (Freedoom, E1M1, skill 2):** GPT-6.1 Sol driving via
  screenshots + held keys survived the full 8m 9s time-box, ending
  alive at 85% health with **2 confirmed kills**. It cleared the
  starting hall, used the automap unprompted to route-find, and won a
  point-blank fight in the central pit; it never found the exit — a
  navigation/puzzle miss, not a perception or input failure.
- **Blender:** the same driver built a low-poly rocket (cylinder body,
  cone nose, porthole, three fins, ground plane, sun, camera) entirely
  through the Blender GUI — no Python/`bpy` — in 9m 19s, then rendered
  it, judged its own render too dark, strengthened the sun, and
  re-rendered before exporting.

## The claim

Models will keep changing; these tables will change with them. What
does not change is the contract: **every run ends DONE-verified or in
a named structured abort, with a flight-recorder log — silent
failures: 0.**
