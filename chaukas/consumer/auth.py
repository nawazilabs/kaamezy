"""Who is calling the consumer API.

CHAUKAS_CONSUMER_AUTH=supabase  the browser sends its Supabase access token as `Authorization: Bearer ...`; we ask
                                Supabase (GET /auth/v1/user) whether it is valid and take the user id from the answer.
                                This is the default when SUPABASE_URL is set.
CHAUKAS_CONSUMER_AUTH=off       everyone is the single user "demo-user" (local development and tests only).

The user id decides whose quotes, KYC, policies and loans a request may touch, so nothing the client sends can
point at another user's data. The token is checked on every request and nothing about it is cached.

Owner: Member 1
"""
from __future__ import annotations

import logging
import os

import httpx
from fastapi import Header, HTTPException

log = logging.getLogger("chaukas.consumer")


def mode() -> str:
    explicit = (os.environ.get("CHAUKAS_CONSUMER_AUTH") or "").strip().lower()
    if explicit in ("supabase", "off"):
        return explicit
    return "supabase" if os.environ.get("SUPABASE_URL") else "off"


def _unauth(msg: str) -> HTTPException:
    return HTTPException(401, {"code": "UNAUTHENTICATED", "message": msg})


def current_user(authorization: str | None = Header(default=None)) -> str:
    if mode() == "off":
        return "demo-user"
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauth("Sign in first.")
    token = authorization[7:].strip()
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    if not url or not key:
        log.error("CHAUKAS_CONSUMER_AUTH=supabase but SUPABASE_URL / SUPABASE_PUBLISHABLE_KEY are not set")
        raise HTTPException(503, {"code": "AUTH_NOT_CONFIGURED", "message": "Sign-in check is not configured on the server."})
    try:
        r = httpx.get(f"{url}/auth/v1/user", headers={"apikey": key, "Authorization": f"Bearer {token}"}, timeout=6)
    except httpx.HTTPError:
        raise HTTPException(503, {"code": "AUTH_UNAVAILABLE", "message": "Could not reach the sign-in service."})
    uid = (r.json() or {}).get("id") if r.status_code == 200 else None
    if not uid:
        raise _unauth("Your sign-in is no longer valid. Please sign in again.")
    return uid
