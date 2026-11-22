"""
The v2 app, standalone on its own port.

Separate from the classic app on purpose. Mounting the v2 UI inside the existing
server would mean one origin serving two products, two front ends fighting over
`/`, and no way to restart one without the other. A second process on a second
port keeps the classic pipeline exactly as it was — including the fact that it
keeps serving if this one is down.

    python -m paylens2.api.app              # http://localhost:8001
    V2_PORT=9001 python -m paylens2.api.app
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend import config as base
from backend.api.routes import auth as auth_routes
from paylens2 import config
from paylens2.api.routes import router as v2_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("paylens2")

# A durable sink as well as stdout. Without it the log lives only in whatever
# terminal started the process, so a restart loses every record of what the
# models actually did — which is the one thing worth having afterwards.
_LOG_DIR = base.ROOT / "logs"
try:
    from logging.handlers import RotatingFileHandler

    _LOG_DIR.mkdir(exist_ok=True)
    _fh = RotatingFileHandler(_LOG_DIR / "paylens2.log", maxBytes=2_000_000,
                              backupCount=3, encoding="utf-8")
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(_fh)
    log.info("logging to %s", _LOG_DIR / "paylens2.log")
except Exception as exc:  # noqa: BLE001
    # A read-only deployment is a reason to carry on without a file, not to
    # refuse to start.
    log.warning("file logging unavailable: %s", exc)

PORT = int(os.getenv("V2_PORT", "8001"))
FRONTEND = Path(__file__).parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    from backend.core import db
    from paylens2.core.model import get_model, views_present

    try:
        db.connect()
        log.info("duckdb ready: %s", base.DUCKDB_PATH)
    except Exception as exc:  # noqa: BLE001
        log.error("duckdb unavailable: %s", exc)

    m = get_model()
    log.info("v2 semantic model %s (%d metrics, %d dimensions, tables %s)",
             m.version, len(m.metrics), len(m.dimensions), sorted(m.allowed_tables))

    if views_present():
        log.info("v2 views present")
    else:
        log.error("v2 views are NOT built — run: python -m paylens2.schema.build")

    log.info("author=%s  synth=%s", config.AUTHOR_MODEL, config.SYNTH_MODEL)
    if not base.LLM_API_KEY:
        log.error("LLM_API_KEY is not set — v2 cannot author a query without a model")

    if base.SEED_DEMO_USERS:
        from backend.core import auth as auth_mod
        try:
            auth_mod.seed_demo_users()
        except Exception as exc:  # noqa: BLE001
            log.warning("demo user seeding failed: %s", exc)
    yield


app = FastAPI(title="PayLens v2", version="2.0.0",
              description="LLM-authored SQL over guarded views",
              lifespan=lifespan)

_explicit = base.CORS_ORIGINS != ["*"]
app.add_middleware(CORSMiddleware, allow_origins=base.CORS_ORIGINS,
                   allow_credentials=_explicit,
                   allow_methods=["GET", "POST"], allow_headers=["*"])

# Auth first so the role switcher works; the v2 routes take role from the
# session exactly as the classic /ask does.
app.include_router(auth_routes.router)
app.include_router(v2_router)


@app.get("/health")
def health() -> dict:
    from paylens2.api.routes import v2_health
    return v2_health()


# The chart renderer — 797 lines of hand-rolled SVG, no dependencies — now
# lives in this app's own frontend directory. It used to be served from the v1
# UI over a /shared mount so a fix would land in both front ends; with v1 gone
# there is no second front end to keep in step, and a mount pointing at a
# directory that no longer exists is a failure waiting to happen.

# Mounted LAST: a Mount at "/" matches every path and would shadow the API.
if FRONTEND.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="v2ui")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
