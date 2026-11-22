"""
One JSON-returning LLM call, made robust.

Two layers, because the model chosen for v2 is a CODE model: excellent SQL,
careless JSON.

  1. ask for JSON mode, when the provider supports it
  2. parse tolerantly, repairing and truncating as needed (jsonx)

A caller that needs SQL and got unparseable JSON can still salvage the query —
and it remains safe to try, because salvaged SQL passes check 10 and all nine
guard checks before anything executes.
"""
from __future__ import annotations

import logging

from backend.llm import provider
from backend.llm.provider import CallBudget
from paylens2.core import jsonx
from backend.core import events

log = logging.getLogger("paylens2.llmjson")

JSON_MODE = {"type": "json_object"}


def call(prompt: str, *, system: str, model: str, purpose: str,
         budget: CallBudget | None = None, json_mode: bool = True) -> tuple[dict, str]:
    """
    Returns (parsed, raw_text). `parsed` is {} when nothing could be recovered;
    `raw_text` is kept so the caller can salvage.
    """
    try:
        raw = provider.call_text(prompt, system=system, model=model,
                                 purpose=purpose, budget=budget,
                                 response_format=JSON_MODE if json_mode else None)
    except Exception as exc:  # noqa: BLE001
        # A provider that rejects response_format fails the whole request, so a
        # single retry without it is the difference between "this model does not
        # support JSON mode" and "this model does not work".
        if json_mode and _looks_like_unsupported(exc):
            log.info("provider rejected JSON mode, retrying without: %s", exc)
            events.recovery("model", "the provider refused JSON mode — "
                                     "asked again without it", detail=str(exc)[:200])
            raw = provider.call_text(prompt, system=system, model=model,
                                     purpose=purpose, budget=budget)
        else:
            raise
    try:
        return jsonx.loads(raw), raw
    except ValueError:
        log.warning("response was not recoverable as JSON (%d chars)", len(raw or ""))
        events.recovery("author", "the response was not valid JSON at all — "
                                  "falling back to salvaging the query from it")
        return {}, raw or ""


def _looks_like_unsupported(exc: Exception) -> bool:
    t = str(exc).lower()
    return ("response_format" in t or "json_object" in t
            or "not supported" in t or "unsupported" in t
            or "400" in t and "format" in t)
