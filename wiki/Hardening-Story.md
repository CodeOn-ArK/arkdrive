# The Hardening Story

Summary of `docs/HARDENING.md` in the repo — read that file for the
full text.

Most GUI-agent projects demo well and fail quietly. This one was built
the other way around: write the executor, then attack it — repeatedly,
with an adversarial review process whose only job was to find ways it
could lie, hang, or loop.

## The trio

Three passes, run in order, each with a different mandate:

1. **Observer** — read-only. Catalogued every way the executor could
   misreport reality: sensors that called a successful click "no
   change", rungs that reported success on dispatch instead of effect,
   a suspected factor-of-two coordinate error in the vision rung,
   surfaces misclassified by default configuration. 15 findings,
   ranked.
2. **Optimizer** — fix-and-prove. **125 review passes**, each required
   to *reproduce* a defect before fixing it, each fix locked in with a
   regression test. Roughly **250 discrete defects** found and fixed;
   the test suite grew past 100 functions. Only 14 of the 125 passes
   came back completely clean — which is the point of running 125.
3. **Debugger** — adversarial verification of the optimizer's own
   claims. Of the final five "fixed" items it was handed, it confirmed
   three and **refuted two**, reproducing failures the previous pass
   had declared fixed (a keyboard-guard bug that let a shifted
   character through unguarded, and a modifier-binding bug that
   wrongly rejected a key combination). Both were then fixed for real
   and re-verified with new tests.

## The structural find

The passes converged everywhere except one boundary: the executor and
its browser lane are separate processes, and they negotiated "was
this input actually sent?" by matching prefixes in error-message
strings. Three consecutive passes fixed the same defect at three
adjacent layers. String-prefix classification cannot converge — every
new failure mode invents a new string.

So the boundary was rebuilt: the lane returns a **structured dispatch
receipt** (`{version, phase, dispatched, effect}`) and the executor
branches on that data alone. The claim/revoke/prefix machinery was
deleted outright, and the receipt path was verified live, end-to-end:
a real click dispatched through the new contract, its effect
independently confirmed on the page.

## The sensor find

On remote-desktop (VNC) work, two of the executor's "eyes" disagreed:
one reported the focused window, another read the visible screen —
inside a viewer those are different things, and the driving model
trusted whichever contradicted the task and stopped. The fix made the
visible screen the single sensor of record: one frame feeds
observation, OCR, and the progress hash; window metadata is demoted
to an advisory that can never override pixels. Coherence was proven
in live re-runs (observation and OCR returning identical content
under identical state hashes).

## The completion find

With sensing fixed, the remaining failures were behavioral:
concluding without acting, claiming a 2FA page that wasn't on screen,
writing conclusions so long they truncated into malformed output.
These were fixed at the executor layer — an auth-evidence guard, an
engagement floor that rejects premature completions while naming the
literal next action, and a truncation retry with a larger budget.

The honest footnote: on the hardest task in the suite (driving an
engineering application inside a nested remote desktop), the local
27B model still fails. The executor's contribution is that it fails
*measurably* — named aborts, full flight-recorder logs, zero silent
failures — and a stronger driver passed the same task on the same
desktop through the same toolset. That is why the benchmark table is
published per model.

## The rules that came out of it

- Never report an outcome from an intermediate screen.
- A "done" from the agent that built something is a claim, not a
  verification — check it against the primary source.
- Every fix gets a failing-then-passing test, or it isn't a fix.
- When a boundary keeps breaking at adjacent layers, stop patching
  and change the contract.
- Deadlines are checked before every step, not estimated per phase.
- If two sensors can disagree, one of them is the record and the
  other is a hint. Decide which, in code.
