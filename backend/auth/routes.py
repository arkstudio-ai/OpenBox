"""Authentication API routes — register, login, refresh, logout, ticket."""
import hashlib as _hashlib
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response, Depends
from pydantic import BaseModel

from auth.jwt import create_access_token, create_refresh_token, decode_refresh_token, init_auth
from auth.password import hash_password, verify_password, validate_password_strength
from auth.ticket import create_ticket
from auth.middleware import get_current_user
from auth.preview_token import revoke_preview_tokens
from core.identifier import generate_id
from core.log import create_logger
from db.repository.user_repo import PgUserRepo
from db.repository.preference_repo import PgPreferenceRepo

log = create_logger("auth.routes")

router = APIRouter(prefix="/api/auth", tags=["Auth"])

_LEGACY_REFRESH_COOKIE = "refresh_token"
_HOST_REFRESH_COOKIE = "__Host-openbox_refresh_token"

_user_repo = PgUserRepo()
_pref_repo = PgPreferenceRepo()
_cache = None  # Set by init


def init_auth_routes(cache):
    global _cache
    _cache = cache


def _refresh_cookie_policy() -> tuple[str, bool, str]:
    """Return (name, secure, path) for the active deployment boundary."""
    from core.config import get_config

    config = get_config()
    secure = bool(
        getattr(config, "preview_public_origin", "")
        or getattr(config, "auth_cookie_secure", False)
    )
    if secure:
        return _HOST_REFRESH_COOKIE, True, "/"
    return _LEGACY_REFRESH_COOKIE, False, "/api/auth"


def _set_refresh_cookie(response: Response, token: str) -> None:
    name, secure, path = _refresh_cookie_policy()
    response.set_cookie(
        key=name,
        value=token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path=path,
    )


def _get_refresh_cookie(request: Request) -> str | None:
    name, _secure, _path = _refresh_cookie_policy()
    # In dedicated-preview mode the legacy name is deliberately ignored: a
    # sibling preview hostname can create a parent-Domain cookie with that
    # unprefixed name, but browsers will not accept a Domain __Host- cookie.
    return request.cookies.get(name)


def _clear_refresh_cookies(response: Response) -> None:
    name, secure, path = _refresh_cookie_policy()
    response.delete_cookie(
        name,
        path=path,
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    # Clear the old host-only cookie during a deployment-mode transition. It
    # remains ignored in __Host mode even if a sibling keeps injecting a
    # parent-Domain cookie that this host cannot delete.
    if name != _LEGACY_REFRESH_COOKIE:
        response.delete_cookie(
            _LEGACY_REFRESH_COOKIE,
            path="/api/auth",
            httponly=True,
            samesite="lax",
        )
    else:
        response.delete_cookie(
            _HOST_REFRESH_COOKIE,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )


# ── Request/Response models ──

class RegisterRequest(BaseModel):
    username: str
    password: str
    email: str | None = None

class LoginRequest(BaseModel):
    username: str
    password: str

class ExtensionAuthRequest(BaseModel):
    refresh_token: str

class LogtoExchangeRequest(BaseModel):
    code: str
    code_verifier: str
    redirect_uri: str | None = None

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict

class PreferencesUpdate(BaseModel):
    theme: str | None = None
    default_model: str | None = None
    default_agent: str | None = None
    default_variant: str | None = None
    sidebar_open: bool | None = None
    right_panel_open: bool | None = None
    bottom_panel_height: int | None = None
    extra: dict | None = None


# ── Rate limiting helper ──

async def _check_rate_limit(key: str, limit: int, window: int):
    if _cache is None:
        return
    count = await _cache.incr(f"rl:{key}", ttl=window)
    if count > limit:
        raise HTTPException(status_code=429, detail="Too many requests")


# ── Routes ──

def _blacklist_key(token: str) -> str:
    """Cache key identifying one refresh token.

    A hash of the whole token, never a prefix. Every JWT this service issues
    opens with the same base64 header — `token[:32]` was that header verbatim,
    identical for every token of every user. Blacklisting one therefore
    blacklisted all of them: a single logout silently signed the whole
    deployment out and kept it that way until the entry expired.
    """
    return f"jwt_bl:{_hashlib.sha256(token.encode()).hexdigest()}"


@router.post("/register", response_model=TokenResponse)
async def register(body: RegisterRequest, request: Request, response: Response):
    # Validate password
    err = validate_password_strength(body.password)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # Check username taken
    existing = await _user_repo.get_by_username(body.username)
    if existing:
        raise HTTPException(status_code=409, detail="Username already taken")

    # Create user
    user_id = generate_id()
    hashed = hash_password(body.password)
    await _user_repo.create(id=user_id, username=body.username, password_hash=hashed, email=body.email)

    # Create default project
    from db.repository.session_repo import PgSessionRepo  # avoid circular
    from db.models.project import Project
    from db.base import get_db_session
    project_id = generate_id()
    now = datetime.now(timezone.utc)
    async with get_db_session() as session:
        session.add(Project(id=project_id, user_id=user_id, name="Default", slug="default",
                           created_at=now, updated_at=now))

    # Issue tokens
    access = create_access_token(user_id)
    refresh = create_refresh_token(user_id)
    _set_refresh_cookie(response, refresh)

    user = await _user_repo.get(user_id)
    return TokenResponse(access_token=access, user=_safe_user(user))


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    await _check_rate_limit(f"login:{ip}", limit=5, window=60)

    user = await _user_repo.get_by_username(body.username)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="Account is disabled")

    # Check lockout
    if user.get("locked_until"):
        locked = datetime.fromisoformat(user["locked_until"])
        if locked > datetime.now(timezone.utc):
            raise HTTPException(status_code=423, detail="Account locked. Try again later.")
        else:
            await _user_repo.reset_failed_login(user["id"])

    if not verify_password(body.password, user.get("password_hash", "")):
        count = await _user_repo.increment_failed_login(user["id"])
        if count >= 5:
            lock_until = datetime.now(timezone.utc) + timedelta(minutes=15)
            await _user_repo.lock_until(user["id"], lock_until.isoformat())
            raise HTTPException(status_code=423, detail="Account locked for 15 minutes")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Reset failed count on success
    if user.get("failed_login_count", 0) > 0:
        await _user_repo.reset_failed_login(user["id"])

    access = create_access_token(user["id"], user.get("role", "user"))
    refresh = create_refresh_token(user["id"])
    _set_refresh_cookie(response, refresh)

    return TokenResponse(access_token=access, user=_safe_user(user))


@router.get("/logto/config")
async def logto_config():
    """Public values the browser needs to start the PKCE flow."""
    from auth.logto import public_config
    from core.config import get_config

    config = get_config()
    data = public_config()
    data["redirect_uri"] = config.logto_redirect_uri
    data["post_logout_redirect_uri"] = config.logto_post_logout_redirect_uri
    return data


@router.post("/logto/exchange", response_model=TokenResponse)
async def logto_exchange(body: LogtoExchangeRequest, request: Request, response: Response):
    """Trade a Logto authorization code for an OpenBox session.

    The browser ran the PKCE dance up to the redirect; we complete the code
    exchange server-side (avoids Logto's CORS allowlist), verify the resulting
    ID token against JWKS, then upsert the user and issue our own JWT.
    """
    from auth.logto import (
        PROVIDER, LogtoError, derive_username, exchange_code, is_enabled, verify_id_token,
    )
    from core.config import get_config

    if not is_enabled():
        raise HTTPException(status_code=503, detail="Logto is not configured on this server")

    ip = request.client.host if request.client else "unknown"
    await _check_rate_limit(f"logto:{ip}", limit=20, window=60)

    redirect_uri = body.redirect_uri or get_config().logto_redirect_uri
    try:
        tokens = await exchange_code(body.code, body.code_verifier, redirect_uri)
        claims = verify_id_token(tokens["id_token"])
    except LogtoError as e:
        log.warning(f"Logto sign-in rejected from {ip}: {e}")
        raise HTTPException(status_code=401, detail=str(e))

    subject = str(claims["sub"])
    email = claims.get("email")
    avatar = claims.get("picture")

    # Keyed on `sub` only. A local username/password account with the same email
    # stays a separate user — silently merging identities across auth methods is
    # an account-takeover foot-gun, so we don't.
    user = await _user_repo.get_by_oauth(PROVIDER, subject)
    if user is None:
        username = derive_username(claims)
        if await _user_repo.get_by_username(username):
            username = f"{username}-{subject[:6]}"

        user_id = generate_id()
        await _user_repo.create(
            id=user_id, username=username, password_hash=None, email=email,
            oauth_provider=PROVIDER, oauth_id=subject, avatar_url=avatar,
        )

        from db.models.project import Project
        from db.base import get_db_session
        now = datetime.now(timezone.utc)
        async with get_db_session() as session:
            session.add(Project(id=generate_id(), user_id=user_id, name="Default",
                                slug="default", created_at=now, updated_at=now))

        user = await _user_repo.get(user_id)
        log.info(f"Provisioned Logto user {username} ({user_id})")

    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="Account is disabled")

    access = create_access_token(user["id"], user.get("role", "user"))
    refresh_token = create_refresh_token(user["id"])
    _set_refresh_cookie(response, refresh_token)
    return TokenResponse(access_token=access, user=_safe_user(user))


@router.post("/refresh")
async def refresh(request: Request, response: Response):
    token = _get_refresh_cookie(request)
    if not token:
        raise HTTPException(status_code=401, detail="No refresh token")

    # Check if refresh token is blacklisted
    if _cache and await _cache.exists(_blacklist_key(token)):
        raise HTTPException(status_code=401, detail="Token has been revoked")

    payload = decode_refresh_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user_id = payload.get("sub")
    user = await _user_repo.get(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    access = create_access_token(user_id, user.get("role", "user"))
    new_refresh = create_refresh_token(user_id)
    _set_refresh_cookie(response, new_refresh)

    return {"access_token": access, "token_type": "bearer"}


@router.post("/logout")
async def logout(request: Request, response: Response, current_user: dict = Depends(get_current_user)):
    # Blacklist the refresh token if present
    refresh_token = _get_refresh_cookie(request)
    if refresh_token and _cache:
        from auth.jwt import decode_refresh_token
        payload = decode_refresh_token(refresh_token)
        if payload:
            # Blacklist for remaining validity
            import time
            exp = payload.get("exp", 0)
            ttl = max(int(exp - time.time()), 1)
            await _cache.set(_blacklist_key(refresh_token), "1", ttl=ttl)

    await revoke_preview_tokens(current_user["user_id"])
    _clear_refresh_cookies(response)
    return {"ok": True}


@router.post("/ticket")
async def get_ticket(current_user: dict = Depends(get_current_user)):
    ticket = await create_ticket(current_user["user_id"], current_user.get("role", "user"))
    return {"ticket": ticket}


@router.post("/extension-auth")
async def extension_auth(body: ExtensionAuthRequest):
    """Authenticate Chrome extension via refresh_token value (sent in body).

    Returns a one-time ticket for WebSocket connection + user info.
    """
    if _cache and await _cache.exists(_blacklist_key(body.refresh_token)):
        raise HTTPException(status_code=401, detail="Token has been revoked")

    payload = decode_refresh_token(body.refresh_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user_id = payload.get("sub")
    user = await _user_repo.get(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    ticket = await create_ticket(user_id, user.get("role", "user"))
    return {"ticket": ticket, "user": _safe_user(user)}


@router.get("/me/prompt-history")
async def get_prompt_history(current_user: dict = Depends(get_current_user)):
    # For now return empty list (will be populated when prompt history tracking is added)
    return []


@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    user = await _user_repo.get(current_user["user_id"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _safe_user(user)


@router.get("/me/preferences")
async def get_preferences(current_user: dict = Depends(get_current_user)):
    prefs = await _pref_repo.get(current_user["user_id"])
    return prefs or {}


@router.put("/me/preferences")
async def update_preferences(body: PreferencesUpdate, current_user: dict = Depends(get_current_user)):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    result = await _pref_repo.upsert(current_user["user_id"], **fields)
    return result


def _safe_user(user: dict) -> dict:
    """Remove sensitive fields from user dict."""
    return {k: v for k, v in user.items() if k not in ("password_hash",)}
