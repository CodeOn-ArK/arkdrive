# ArkDrive Wiki

**ArkDrive** is a model-agnostic GUI computer-use executor for Linux/X11:
a hardened runtime that sits between any tool-calling LLM and a real
desktop, and makes GUI work accountable — per-action deadlines, fallback
input ladders, structured dispatch receipts, progress sensing, and honest
structured aborts instead of silent failures.

The driving model is bring-your-own (any OpenAI-compatible, tool-calling
endpoint). Benchmarks are published **per model**, so the executor's
quality and the model's quality are never confused.

## Pages

- **[Test-Results](Test-Results)** — the full benchmark suite: all 10
  task cards, per-model results, core/smoke test status, and the fun
  runs (Doom, Blender).
- **[Architecture](Architecture)** — how the executor works: the loop,
  deadlines, input ladders and receipts, micro-agents, progress sensing,
  completion discipline.
- **[Safety-Model](Safety-Model)** — kill switch, dry-run, audit log,
  human-in-the-loop points, deployment guidance.
- **[Hardening-Story](Hardening-Story)** — how the executor was hardened:
  125 adversarial review passes, ~250 defects, one structural rebuild,
  and the rules that came out of it.

## In the repo

- `README.md` — pitch, quickstart, short benchmark table
- `docs/` — the full architecture, safety, runbook-pack, and hardening
  documents (this wiki summarizes them)
- `bench/` — task cards, runner, and machine-readable results
