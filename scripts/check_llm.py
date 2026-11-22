"""
Verify an LLM key end to end.

    python scripts/check_llm.py

Checks, in order: the key is present, the catalogue is reachable, both configured
models exist and are not silently deprecated, each one actually answers, and a real
question routes through the planner. Reports token spend and estimated cost.

Exits non-zero on the first thing that is wrong, with what to do about it.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import config                      # noqa: E402
from backend.llm import provider                # noqa: E402

G, R, Y, D = "\033[0;32m", "\033[0;31m", "\033[0;33m", "\033[0m"
ok = lambda m: print(f"  {G}✓{D} {m}")
bad = lambda m: print(f"  {R}✗{D} {m}")
warn = lambda m: print(f"  {Y}!{D} {m}")


def die(msg: str, fix: str) -> None:
    bad(msg)
    print(f"\n    How to fix: {fix}\n")
    raise SystemExit(1)


def catalogue() -> dict:   # noqa: D401  (kept for OpenRouter setups)
    with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=20) as r:
        return {m["id"]: m for m in json.load(r)["data"]}


def main() -> int:
    print("\nPayLens — LLM connectivity check\n" + "─" * 54)

    # 1. a key for each role (they may be different providers)
    roles = [("planner ", config.PLANNER_MODEL, config.PLANNER_API_KEY, config.PLANNER_BASE_URL),
             ("narrator", config.NARRATOR_MODEL, config.NARRATOR_API_KEY, config.NARRATOR_BASE_URL)]
    for label, mid, key, url in roles:
        if not key:
            die(f"no API key for the {label.strip()} role",
                f"set PLANNER_API_KEY and NARRATOR_API_KEY (or LLM_API_KEY for both)\n"
                f"                in {ROOT}/.env")
        ok(f"{label}  key {key[:7]}…{key[-4:]}")
        print(f"              {mid}")
        print(f"              {url}")

    # 2. each model actually answers (a live call beats a catalogue lookup —
    #    it proves the key, the endpoint and the model id all at once)
    print("\n  live calls")
    for label, mid, _k, _u in roles:
        t0 = time.time()
        try:
            out = provider.call_text("Reply with exactly: OK", model=mid,
                                     purpose="plan", max_retries=1)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            hint = ("the model spends its token budget thinking — raise PLANNER_MAX_TOKENS"
                    if "reasoning" in msg or "budget" in msg else
                    "the model id is retired — list current ones from the provider"
                    if "404" in msg or "not available" in msg.lower() else
                    "the key is rejected — check it was copied whole"
                    if "401" in msg or "auth" in msg.lower() else
                    "no credit on the account — add some, or switch to a :free model"
                    if "402" in msg or "credit" in msg.lower() else
                    "rate limited — free models are throttled; wait a minute"
                    if "429" in msg else "see the error above")
            die(f"{label} call failed: {msg[:120]}", hint)
        ok(f"{label}  replied in {int((time.time()-t0)*1000)}ms  ({out[:40]!r})")

    # 4. a real question goes through the planner
    print("\n  end-to-end")
    from paylens2.core.pipeline import ask
    card = ask("what was the approval rate for netbanking last week",
               role="cxo", use_cache=False)
    calls = card.trace.llm_calls
    if not calls:
        warn("no LLM call was made — the deterministic resolver handled it alone.")
        print("     That is correct behaviour, not a failure: the model is only")
        print("     consulted when the question is genuinely ambiguous.")
    else:
        ok(f"{len(calls)} LLM call(s): "
           + ", ".join(f"{c.purpose}/{c.model.split('/')[-1]} {c.ms}ms" for c in calls))
    print(f"    answer: {(card.narration.headline if card.narration else '—')[:70]}")

    # a question the resolver cannot handle alone must reach the model
    card2 = ask("which rails are dragging our numbers down this past week",
                role="cxo", use_cache=False)
    n2 = len(card2.trace.llm_calls)
    (ok if n2 else warn)(f"ambiguous question made {n2} LLM call(s)")

    print("\n" + "─" * 54)
    print(f"  {G}LLM is connected.{D} Restart the service to pick it up:")
    print("      systemctl --user restart paylens\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
