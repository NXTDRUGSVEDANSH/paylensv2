"""
v2 routes, mounted under /v2.

ON ROLE AND CLAIMS
------------------
They come from the authenticated session and are NOT accepted in the request
body — exactly as the classic /ask does, and for the reason recorded there: a
self-asserted role means the RBAC layer is enforcing an identity the caller chose.
A second endpoint that took `role` from JSON would reopen that hole while the
first one kept it closed, which is worse than never having fixed it.
"""
from __future__ import annotations

import asyncio
import json
import queue
from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.api.routes.auth import require_user
from backend.core.auth import User
from paylens2 import config
from paylens2.core import model as v2model
from paylens2.core.pipeline import ask as run_v2

router = APIRouter(prefix="/v2", tags=["v2"])


class V2AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    today: date | None = None
    use_kb: bool = True
    # Prior turns, oldest first: [{"question": "...", "sql": "..."}]. The caller
    # holds the thread; v2 is stateless so that a conversation can be replayed
    # exactly for debugging.
    conversation: list[dict] | None = None


@router.post("/ask")
def v2_ask(req: V2AskRequest, request: Request, response: Response,
           user: User = Depends(require_user)) -> dict:
    model = v2model.get_model()
    if user.role not in model.roles:
        raise HTTPException(403, f"account role '{user.role}' is not in the v2 "
                                 f"semantic model")
    out = run_v2(req.question, role=user.role, claims=user.claims,
                 today=req.today, conversation=req.conversation,
                 use_kb=req.use_kb)
    return asdict(out)


@router.post("/ask/stream")
async def v2_ask_stream(req: V2AskRequest, request: Request,
                        user: User = Depends(require_user)) -> StreamingResponse:
    """
    The same answer as /v2/ask, with the pipeline's own events as they happen.

    WHY THIS EXISTS
    ---------------
    The UI used to animate a fixed list of stages on a timer. It was a lie in
    both directions: it advanced whether or not anything had happened, and it
    gave equal screen time to steps that take 7 seconds and steps that take
    nothing. On a measured run the two model calls were 96% of the wall clock
    and the guard checks were 18ms.

    So the stages shown are now the ones the pipeline reports, and the heal loop
    and the recoveries — a repaired JSON response, a provider retry, a rejected
    composition — are reported too, because those are the most interesting thing
    the system does and they were previously visible only on the server's stdout.

    /v2/ask is unchanged and remains the contract for API clients. This endpoint
    is additive: same inputs, same final object, delivered as the last event.
    """
    model = v2model.get_model()
    if user.role not in model.roles:
        raise HTTPException(403, f"account role '{user.role}' is not in the v2 "
                                 f"semantic model")

    q: queue.Queue = queue.Queue()

    def work() -> None:
        # The pipeline is synchronous, so it runs in a worker thread and posts
        # events to this queue. The channel is bound inside ask(), which means
        # it is bound in THIS thread — which is the reason it lives in a
        # ContextVar rather than a module global.
        try:
            out = run_v2(req.question, role=user.role, claims=user.claims,
                         today=req.today, conversation=req.conversation,
                         use_kb=req.use_kb, on_event=lambda ev: q.put(("event", ev)))
            q.put(("answer", asdict(out)))
        except Exception as exc:  # noqa: BLE001
            q.put(("error", {"detail": f"{type(exc).__name__}: {exc}"}))

    async def stream():
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, work)
        # A comment frame up front: it opens the stream immediately, so the
        # browser stops waiting on headers before the first model call returns.
        yield ": open\n\n"
        idle = 0.0
        while True:
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                if fut.done() and q.empty():
                    break
                await asyncio.sleep(0.05)
                idle += 0.05
                if idle >= 15:          # keep proxies from closing a quiet stream
                    idle = 0.0
                    yield ": ping\n\n"
                continue
            idle = 0.0
            yield f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"
            if kind in ("answer", "error"):
                break
        await fut
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",      # nginx would otherwise buffer the whole thing
    })


@router.get("/health")
def v2_health() -> dict:
    from backend.llm import provider
    out = {
        "engine": "v2",
        "views_built": v2model.views_present(),
        "author_model": config.AUTHOR_MODEL,
        "synth_model": config.SYNTH_MODEL,
        "llm_available": provider.available(),
    }
    try:
        m = v2model.get_model()
        out["semantic_model"] = m.version
        out["metrics"] = len(m.metrics)
        out["dimensions"] = len(m.dimensions)
        out["allowed_tables"] = sorted(m.allowed_tables)
    except Exception as exc:  # noqa: BLE001
        out["semantic_model_error"] = str(exc)
    if not out["views_built"]:
        out["hint"] = "run: python -m paylens2.schema.build"
    if not out["llm_available"]:
        out["hint"] = "v2 requires a language model; set LLM_API_KEY"
    return out


@router.get("/schema")
def v2_schema() -> dict:
    """The exact card sent to call 1. Here so a wrong answer can be diagnosed."""
    return {"card": v2model.schema_card()}


@router.get("/catalogue")
def v2_catalogue() -> dict:
    from paylens2.core import catalogue
    return {"entries": catalogue.summaries()}
