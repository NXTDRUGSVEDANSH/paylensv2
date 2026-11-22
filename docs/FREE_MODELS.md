# Choosing the free models

PayLens runs on OpenRouter's **free tier** — no credit card, no spend. There are 13
usable free models, and they are not interchangeable: several cannot produce valid
JSON at all, and one returns nothing but its own internal monologue.

So the choice was made by benchmarking all of them on the two jobs the system
actually needs, rather than by reading context windows.

## The two tasks

| Task | Pass condition |
|---|---|
| **Planner** | Returns parseable JSON **and** picks a metric id that exists in the semantic model |
| **Narrator** | Returns parseable JSON **and** copies pre-formatted values verbatim (`86.60%` stays `86.60%`) |

## Results

| Model | Planner | Narrator | Verdict |
|---|---|---|---|
| **`baidu/ernie-5.5-flash-preview:free`** | **3.6s ✓ JSON + valid id** | **2.4s ✓ kept format** | **chosen — only model fast *and* correct on both** |
| `openrouter/free` | 1.5s ✓ | 1.8s ✓ | fastest, but it is a router — the model behind it can change between runs, so results are not reproducible |
| `dots-studio/dots-3-note-preview:free` | 8.6s ✓ | 12.1s ✓ | correct but slow |
| `recursive/flint-v1.0:free` | 13.1s ✓ | 21.7s ✓ | correct, far too slow |
| `nvidia/nemotron-3.5-lightning:free` | 10.5s ✓ | ✗ reasoning only | spends its whole token budget thinking and returns empty content |
| `inclusionai/ling-3.2-flash:free` | ✗ error | 31.6s ✓ | unusable as a planner |
| `google/gemma-4-31b-it:free` | ✗ error | ✗ error | failed both |
| `google/gemma-4-26b-a4b-it:free` | ✗ error | ✗ error | failed both |

Reproduce with `python scripts/check_llm.py` (verifies the live config) — the full
sweep is in the commit history.

## Configuration

```ini
LLM_API_KEY=sk-or-v1-...
LLM_BASE_URL=https://openrouter.ai/api/v1
PLANNER_MODEL=baidu/ernie-5.5-flash-preview:free
NARRATOR_MODEL=baidu/ernie-5.5-flash-preview:free
PLANNER_MAX_TOKENS=6000
```

`PLANNER_MAX_TOKENS` is deliberately generous. Reasoning models spend output tokens
on an internal monologue *before* the answer; with a normal budget they hit the limit
mid-thought and return empty content, which looks like a dead endpoint and is not.
The provider now detects that case and says so explicitly.

## What it costs in latency

| | Time | LLM calls |
|---|---|---|
| New question | ~11–13s | 1 (narration) |
| Same question again | **8ms** | 0 |

**Narration is essentially all of it** — 11.0s of an 11.2s request. The free model is
the entire bottleneck; the database query is 101ms. Two consequences:

- The caches matter far more with a slow model than a fast one. A repeat is 8ms.
- A paid model removes the problem: `qwen/qwen3-coder-next` + `gemini-3-flash-preview`
  answers in ~2s for about **$2.84 per 1,000 questions**. Two lines in `.env`.

## Why most answers make no planner call

The deterministic resolver handles any question it fully understands, so the planner
is only consulted for genuinely ambiguous phrasing. In the demo set that is almost
never — which is why you see `llm=['narrate']` and not `llm=['plan','narrate']`.

That is the intended design, not an integration failure: **the model is never asked
for a number, only for words.**

## Other providers

Any OpenAI-compatible endpoint works, and planner and narrator can be on different
ones (`PLANNER_BASE_URL` / `NARRATOR_BASE_URL`). Verified during setup:

| Provider | Base URL | Note |
|---|---|---|
| OpenRouter | `https://openrouter.ai/api/v1` | current setup |
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | key prefix `nvapi-`; Nemotron models return their monologue in `reasoning_content` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | OpenAI-compatible endpoint; a plain API key works as a Bearer token |
| Ollama / LM Studio | `http://localhost:11434/v1` | fully local, no key, nothing leaves the machine |
