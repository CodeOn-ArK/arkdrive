# Runner note — Local A/B (Codex driver, standard vs lean profile)

**Date:** Mon Nov 9, 2026 · **Machine:** ArK2 desktop (:0), Codex `gpt-6.1-sol`, cua toolset.
**Why:** the Virtuoso A/B (see `runner-note-virtuoso-ab.md`) is blocked — scf02 SSH key
auth broke over Nov 8–9 and the Viterbi servers were reported down. This is the same
profile comparison on a local GUI task instead.

## Task (identical for both runs)

In `/home/ark/ab-bench/` on ArK2 (cleaned before each run):

1. Open GNOME Text Editor (`gnome-text-editor`; verified present at `/usr/bin`).
2. New document with exactly 10 lines (see either prompt), Enter after the last line too.
3. Save as `/home/ark/ab-bench/result.txt`.
4. In Files (Nautilus): create subfolder `verified` in ab-bench, move `result.txt` into it.
5. Close both apps.

Prompts: `bench/prompts/local-a.txt` (standard: screenshot-driven, verify by screenshot,
one action per turn) and `bench/prompts/local-b-lean.txt` (lean: text before vision,
cropped shots, state ledger, batched bursts, state-based verification, one final
full screenshot gate).

## Ground truth (checked by the runner from the shell, never by the driver)

Expected content = the 10 lines, each `\n`-terminated (incl. the last).
Expected sha256: `375ac9c8fe264f17f652b581e9d27a7568058274b56482c54f9618184b5e440e`

PASS = all three: `/home/ark/ab-bench/verified/result.txt` exists · its sha256 equals the
expected hash · no `result.txt` remains in the ab-bench root.

## Launch discipline

- Desktop serialization: `pgrep -f "codex exec"` on ArK2 must be empty before each
  launch; wait, never preempt another task's driver.
- Detached launch: `setsid nohup timeout 1200 codex exec --skip-git-repo-check
  --sandbox danger-full-access "$(cat <prompt>)" < /dev/null > <log> 2>&1 &`, `DISPLAY=:0`.
- ab-bench is wiped between runs so run B inherits nothing from run A.

## Metrics (per run)

- **Wall time:** launch timestamp to driver process exit.
- **Actions:** the driver's own count from its final `ACTIONS:` summary line.
- **Tokens:** the `tokens used` total Codex prints at the end of its log.
- **Verdict:** PASS/FAIL against the ground truth above, plus notes on misreads,
  missed verifications, and retries from the driver log.
