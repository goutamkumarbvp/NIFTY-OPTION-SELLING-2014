from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from terminal.api.deps import current_user, ok

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


def _token(request: Request) -> str:
    return request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("token", "")


@router.get("/me")
async def me(request: Request):
    auth = request.app.state.auth
    return ok({"user": auth.identify(_token(request)), **auth.describe()})


@router.post("/login")
async def login(body: LoginBody, request: Request):
    auth = request.app.state.auth
    if auth.locked(body.username):
        request.app.state.terminal.audit.record("LOGIN_LOCKED", {"username": body.username}, body.username)
        raise HTTPException(status_code=429, detail="ACCOUNT_LOCKED")
    res = auth.login(body.username, body.password)
    if not res:
        request.app.state.terminal.audit.record("LOGIN_FAILED", {"username": body.username}, body.username)
        raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS")
    request.app.state.terminal.audit.record("LOGIN", {"username": body.username}, body.username)
    return ok(res)


@router.post("/logout")
async def logout(request: Request, user: dict = Depends(current_user)):
    request.app.state.auth.logout(_token(request))
    return ok()
