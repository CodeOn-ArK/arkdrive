# Virtuoso A/B — Codex token efficiency — Mon Nov 9, 2026

**Outcome: BOTH RUNS BLOCKED before launch. No A/B numbers were produced.**
The bench environment (Virtuoso on viterbi-scf02, reached through the
TigerVNC viewer on ArK2) is unreachable today: SSH key auth to scf02 is
broken, and the only substitute host cannot run Cadence for this account.
Evidence below. The prompts and runner note are committed and ready; the
experiment can run as soon as scf02 access is restored.

## Results table

| Run | Profile | Wall time | Actions | Tokens | Verdict |
|---|---|---|---|---|---|
| Historical baseline (Nov 8, 2026) | Standard (A) | 159 s | 17 cua | 33,171 | PASS |
| Run A (Nov 9, 2026) | Standard | — | — | — | NOT RUN — blocked (scf02 SSH auth) |
| Run B (Nov 9, 2026) | Lean | — | — | — | NOT RUN — blocked (same) |

Token delta: not measurable this round.

## What was checked (Nov 9, ~11:40–11:55 PST)

- **Viterbi tunnel: UP.** OpenConnect (`oc-usc2.service`, netns `usc`,
  tunnel IP 10.49.134.28) running, keepalive running; auth valid to
  Sun Nov 22, 2026, 10:03 AM PT. Traffic reaches scf02's sshd.
- **Desktop: free.** No other `codex exec` driver on ArK2's `:0` (the
  social-accounts walkthrough from the other chat had finished).
- **VNC viewer: found disconnected** (readTLS dialog, the same failure
  as Nov 8). Reconnect was attempted; the viewer then exited because
  its SSH forward was dead (next point).
- **`vnc-fwd.service` (the scf02 SSH forward) died Nov 8, 19:39 PST**
  ("Read from remote host … Connection timed out") and, being a
  transient unit, never restarted.
- **scf02 SSH auth is broken.** Restarting the forward and direct
  attempts both fail: the server presents the expected host key
  (SHA256:ZzheAogU8…, matches `known_hosts` on ArK2), but the
  `vlab_ed25519` public key — which worked through Nov 8 — is now
  rejected ("Offering public key …" → "Permission denied
  (publickey,…)"). Password auth exists on the server but the USC
  password is not available to this run (it lives only in ArK2's
  Chromium saved logins). A TCP probe of scf02:5997 from scf01
  returned no RFB banner, so there is no unauthenticated path to the
  VNC server either.
- **scf01 is not a substitute.** Key auth works there and the shared
  home has `~/work_gpdk045` + the `ee477proj_sahebpan` library, but
  `/tools` on scf01 is ACL-locked (`drwx--x--- estevezd
  ee536AStudents`, account not a member, verified with `namei -l`), so
  Virtuoso cannot execute there. A VNC :97 staged on scf01 for this
  test was removed again; nothing was left running.

## To unblock

Any one of: (1) re-add the `vlab_ed25519` public key to
`~/.ssh/authorized_keys` as effective on scf02 (e.g. from an interactive
scf02 session via the USC View desktop pool), (2) restore whatever
changed on scf02 over Nov 8–9 (reboot/reimage/sshd or home-mount
change), or (3) grant the account execute access to a host whose
`/tools` it can traverse (e.g. the ee536AStudents-gated scf01, if that
ACL is the intended one for EE-477 — it gated scf02 differently in
Phase 1). After that: restart `vnc-fwd.service`, reconnect the viewer,
stage Virtuoso, and run A then B per `bench/runner-note-virtuoso-ab.md`.

## Accuracy notes

No runs, so no accuracy comparison. Nothing on either VLAB host was
modified: no design data opened or touched; the only remote actions
were read-only probes, one `vncserver` start/kill on scf01, and one
forward-unit restart attempt on ArK2. ArK2 `~/.vnc/arktx:1.log`
truncated at the end of the session, per standing order.
