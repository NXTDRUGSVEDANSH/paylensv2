"""
The advisory catalogue, passed in the prompt rather than retrieved.

semantic/playbooks.yaml is the classic pipeline's file and stays its file — v2
reads it, never writes it, so a payments person still edits exactly one catalogue
and both pipelines follow it.
"""
from __future__ import annotations

import functools
from pathlib import Path

import yaml

from backend import config as base

PATH = Path(base.SEMANTIC_MODEL_PATH).parent / "playbooks.yaml"


@functools.lru_cache(maxsize=1)
def _raw() -> dict:
    if not PATH.exists():
        return {"glossary": [], "playbooks": []}
    return yaml.safe_load(PATH.read_text(encoding="utf-8")) or {}


def summaries() -> list[dict]:
    """One line per entry — what call 1 sees."""
    out = []
    for g in _raw().get("glossary", []):
        out.append({"id": g["id"], "title": g["term"],
                    "summary": " ".join(str(g.get("says", "")).split())[:180]})
    for p in _raw().get("playbooks", []):
        out.append({"id": p["id"], "title": p.get("title", p["id"]),
                    "summary": " ".join(str(p.get("frames", "")).split())[:180]})
    return out


def known_ids() -> set[str]:
    return {s["id"] for s in summaries()}


def expand(ids: list[str]) -> str:
    """
    The levers behind the ids call 1 selected — what call 2 may recommend.

    Every lever carries an owner and a question that checks it. Advice with no
    owner never gets actioned; advice with no measurement never gets verified.
    """
    want = set(ids or [])
    out: list[str] = []
    for g in _raw().get("glossary", []):
        if g["id"] in want:
            out.append(f"DEFINITION — {g['term']}: "
                       f"{' '.join(str(g.get('says','')).split())}")
    for p in _raw().get("playbooks", []):
        if p["id"] not in want:
            continue
        out.append(f"PLAYBOOK — {p.get('title', p['id'])}")
        out.append(f"  framing: {' '.join(str(p.get('frames','')).split())}")
        for l in p.get("levers", []) or []:
            out.append(f"  - {' '.join(str(l['lever']).split())}  [owner: {l['owner']}]")
        for lim in p.get("limits", []) or []:
            out.append(f"  limit: {' '.join(str(lim).split())}")
    return "\n".join(out)


def levers(ids: list[str]) -> list[dict]:
    """
    The levers behind the selected playbooks, structured.

    Each carries the owner and the PayLens question that checks it. Advice with
    no owner never gets actioned; advice with no measurement never gets
    verified — so both travel with the lever rather than being left to prose.
    """
    want = set(ids or [])
    out: list[dict] = []
    for p in _raw().get("playbooks", []):
        if p["id"] not in want:
            continue
        for l in p.get("levers", []) or []:
            out.append({
                "lever": " ".join(str(l["lever"]).split()),
                "owner": " ".join(str(l.get("owner", "")).split()),
                "why": " ".join(str(l.get("why", "")).split()),
                "measure": " ".join(str(l.get("measure", "")).split()),
                "playbook": p["id"]})
    return out


def framing(ids: list[str]) -> str:
    want = set(ids or [])
    bits = [" ".join(str(p.get("frames", "")).split())
            for p in _raw().get("playbooks", []) if p["id"] in want]
    bits += [" ".join(str(g.get("says", "")).split())
             for g in _raw().get("glossary", []) if g["id"] in want]
    return " ".join(b for b in bits if b)


def limits(ids: list[str]) -> list[str]:
    want = set(ids or [])
    return [" ".join(str(x).split())
            for p in _raw().get("playbooks", []) if p["id"] in want
            for x in (p.get("limits", []) or [])]


def verify_ids(ids: list[str]) -> tuple[list[str], list[str]]:
    """(valid, invented). An id the catalogue does not hold is dropped, not served."""
    known = known_ids()
    valid = [i for i in (ids or []) if i in known]
    bad = [i for i in (ids or []) if i not in known]
    return valid, bad
