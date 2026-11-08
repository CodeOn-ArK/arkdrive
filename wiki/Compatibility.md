# Compatibility

ArkDrive is bring-your-own-model and bring-your-own-agent. Two
integration surfaces, both documented in full in
[docs/DRIVERS.md](https://github.com/CodeOn-ArK/arkdrive/blob/main/docs/DRIVERS.md).

## Executor-loop models (provider presets)

Set `CU_MODEL_PROVIDER`; `CU_MODEL_SERVER` / `CU_MODEL` override any
preset default. API keys come from `CU_MODEL_API_KEY` — environment
only, never config files, never logged (`--check-config` prints
set/not-set and validates the preset, exiting 1 on problems).

| Preset | Endpoint | Wire format | Default model |
|---|---|---|---|
| `llama` (default) | local llama.cpp server (:8080) | OpenAI-compatible + llama.cpp extensions | `local-model` |
| `openai` | api.openai.com | OpenAI chat completions | set `CU_MODEL` |
| `deepseek` | api.deepseek.com | OpenAI chat completions | `deepseek-chat` |
| `openrouter` | openrouter.ai/api | OpenAI chat completions | set `CU_MODEL` |
| `anthropic` | api.anthropic.com | Anthropic Messages, translated in the executor | set `CU_MODEL` |

Notes:

- Anthropic's API is not OpenAI-shaped; the executor's single HTTP
  call site translates (system prompt → top-level `system` field,
  `x-api-key` auth, content blocks → text, `stop_reason` →
  `finish_reason` spellings).
- Any other OpenAI-compatible endpoint (vLLM, SGLang, Ollama,
  gateways) works via `CU_MODEL_SERVER` + `CU_MODEL` — no preset
  needed.
- The vision rung stays on its own OpenAI-compatible endpoint
  (`CU_VISION_SERVER`, default a local server), independent of the
  driving preset.

## Agentic drivers (external coding agents)

An agent that can run shell commands and view images can skip the
executor loop and drive the tool layer (`src/cua`, `src/cua-lane`)
directly. The contract is small: one JSON object per command,
downscaled screenshots with **native-pixel coordinates** (image ×
`screen_size / shot_size`), structured dispatch **receipts** on the
CDP input commands, shot → act → shot discipline, and the kill-file
convention.

| Driver | Status |
|---|---|
| Codex (GPT-6.1 Sol) | **Proven** — passed the flagship nested-desktop card first attempt (159 s); Doom and Blender fun runs completed |
| Claude Code | By contract — same shell + image-view pattern as Codex |
| Generic agents | By contract — needs shell exec, image viewing (or OCR/CDP-text fallback), JSON parsing, multi-step planning |

Agents landing in the repo should start from `AGENTS.md` at the
root, which points at the full driver contract.
