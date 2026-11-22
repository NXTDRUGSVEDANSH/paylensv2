"""Login, logout, and session resolution."""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend import config
from backend.core import auth
from backend.core.auth import AuthError, User

router = APIRouter(prefix="/auth", tags=["auth"])
COOKIE = "paylens_session"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def current_user(request: Request,
                 authorization: str | None = Header(default=None)) -> User | None:
    """
    Session token from the cookie, or a Bearer header for API clients.

    Returns None when unauthenticated. Callers decide whether that is fatal —
    which keeps REQUIRE_AUTH=false usable for local development without
    scattering conditionals through the routes.
    """
    token = request.cookies.get(COOKIE)
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    return auth.resolve_session(token)


def require_user(request: Request,
                 authorization: str | None = Header(default=None)) -> User:
    user = current_user(request, authorization)
    if user is None:
        if not config.REQUIRE_AUTH:
            # Browsing without signing in. The role is configurable rather than
            # hardcoded, so "anonymous" is not silently a synonym for "admin".
            return User("anonymous", "Browsing (not signed in)", config.ANON_ROLE, {})
        raise HTTPException(401, "not signed in")
    return user


@router.post("/login")
def login(req: LoginRequest, response: Response) -> dict:
    try:
        token, user, expires = auth.login(req.username, req.password)
    except AuthError as exc:
        # 401 with a deliberately vague message: distinguishing "no such user"
        # from "wrong password" hands an attacker a username oracle.
        raise HTTPException(401, str(exc)) from exc
    response.set_cookie(
        COOKIE, token, httponly=True, samesite="lax",
        secure=not config.CORS_ORIGINS == ["*"],   # https in any real deployment
        max_age=auth.SESSION_TTL_S, path="/")
    return {"user": {"username": user.username, "display_name": user.display_name,
                     "role": user.role, "claims": user.claims},
            "expires_at": expires,
            # Returned for non-browser clients; browsers use the cookie.
            "token": token}


@router.post("/logout")
def logout(request: Request, response: Response,
           authorization: str | None = Header(default=None)) -> dict:
    token = request.cookies.get(COOKIE)
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    ok = auth.logout(token)
    response.delete_cookie(COOKIE, path="/")
    return {"signed_out": ok}


@router.get("/me")
def me(request: Request, authorization: str | None = Header(default=None)) -> dict:
    user = current_user(request, authorization)
    if user is None:
        from semantic.loader import get_model
        rd = get_model().roles.get(config.ANON_ROLE)
        return {"authenticated": False, "require_auth": config.REQUIRE_AUTH,
                "anon_role": config.ANON_ROLE,
                "anon_role_label": rd.label if rd else config.ANON_ROLE}
    from semantic.loader import get_model
    rd = get_model().roles.get(user.role)
    return {
        "authenticated": True,
        "user": {"username": user.username, "display_name": user.display_name,
                 "role": user.role, "claims": user.claims},
        "role_detail": None if not rd else {
            "label": rd.label, "row_scope": rd.row_scope,
            "masked_columns": list(rd.masked_columns),
            "denied_metrics": list(rd.denied_metrics)},
    }


@router.get("/demo-users")
def demo_users() -> dict:
    """
    The seeded accounts, shown on the sign-in screen.

    Only exposed when demo seeding is on. A real deployment sets
    SEED_DEMO_USERS=false and this returns nothing.
    """
    if not config.SEED_DEMO_USERS:
        return {"enabled": False, "users": []}
    return {
        "enabled": True,
        "password": config.DEMO_PASSWORD,
        "users": [{"username": u, "display_name": d, "role": r,
                   "claims": c, "note": n} for u, d, r, c, n in auth.DEMO_USERS],
    }
