# Runner note — Virtuoso A/B (Codex driver, token efficiency)

Purpose: measure whether a lean observation profile (text-first, cropped
screenshots, state ledger, batched inputs) cuts Codex driver tokens on the
Virtuoso bench without losing accuracy. Prompt-profile only; the published
executor code is not modified.

## Bench task (identical for both runs)

Via the TigerVNC viewer on ArK2 to the VLAB host running Virtuoso
(library `ee477proj_sahebpan` in `~/work_gpdk045`): open the `fa` and
`alu8` schematics read-only, apply Fit in each, and verify both window
titles plus the `hiGetWindowList()` self-check in the CIW.

- Run A prompt: `bench/prompts/virtuoso-a.txt` (standard profile,
  screenshot-driven, verify by screenshot — same style as the
  Nov 8, 2026 baseline run: PASS, 159 s, 17 cua actions, 33,171 tokens).
- Run B prompt: `bench/prompts/virtuoso-b-lean.txt` (same task and pass
  criteria, lean profile rules added).

## Staging (deterministic, outside the measured runs)

1. Viterbi tunnel up on ArK2 (OpenConnect in netns `usc`; auth expiry
   Nov 22, 2026) and the SSH forward `10.99.0.2:5997` to the VLAB host
   listening; `vncviewer -FullScreen` connected and focused.
2. Virtuoso already running from `~/work_gpdk045` with the CIW open;
   `fa`/`alu8` editor windows NOT open (reset between runs so both runs
   do identical work).
3. Desktop free: `pgrep -f "codex exec"` on ArK2 shows no other driver.
   Only one Codex driver uses ArK2's `:0` at a time; wait, never preempt.

## Launch (per run, on ArK2)

```
setsid nohup timeout 1200 codex exec --skip-git-repo-check \
  --sandbox danger-full-access "$(cat bench/prompts/virtuoso-<run>.txt)" \
  < /dev/null > /tmp/cu-ab-<run>.log 2>&1 &
```
with `DISPLAY=:0` in the environment. Never launch in the foreground of
an ssh session; never combine a pkill pattern with a relaunch literal.

## Metrics (recorded per run in the results file)

- Wall time: launch to driver exit (log timestamps).
- Tokens: the `tokens used` total Codex prints at the end of its log.
- Actions: cua input actions as counted in the driver's final report.
- Verdict: PASS only if both editor titles verified AND the CIW
  `hiGetWindowList()` output lists both windows; note any missed
  verification or wrong read explicitly.
- Run B additionally reports full screenshots vs text observations.

## Hygiene

- Logs stay out of git (or trimmed to token/verdict lines).
- After the runs finish, truncate `~/.vnc/arktx:1.log` on ArK2.
