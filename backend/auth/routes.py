"""Authentication API routes — register, login, refresh, logout, ticket."""
import hashlib as _hashlib
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response, Depends
from pydantic import BaseModel

from auth.jwt import create_access_token, create_refresh_token, decode_refresh_token, init_auth
from auth.password import hash_password, verify_password, validate_password_strength
from auth.ticket import create_ticket
from auth.middleware import get_current_user
from core.identifier import generate_id
from core.log import create_logger
from db.repository.user_repo import PgUserRepo
from db.repository.preference_repo import PgPreferenceRepo

log = create_logger("auth.routes")

router = APIRouter(prefix="/api/auth", tags=["Auth"])

_user_repo = PgUserRepo()
_pref_repo = PgPreferenceRepo()
_cache = None  # Set by init


def init_auth_routes(cache):
    global _cache
    _cache = cache


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

class LogtoIdTokenRequest(BaseModel):
    id_token: str

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


def _coded_error(status: int, code: str, message: str) -> HTTPException:
    """A refusal both clients can turn into their own copy.

    `errors.json` on web and mobile has carried `AUTH_INVALID_CREDENTIALS` and
    `AUTH_USER_EXISTS` all along, but nothing ever emitted them: a bare string
    `detail` leaves the clients falling back to the status, so a wrong password
    came out as "Your session expired. Please sign in again." — which is not
    what happened and not what the person should do about it. Same shape as
    `auth.quota._quota_error`.
    """
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": message},
        headers={"X-Error-Code": code},
    )



@router.post("/register", response_model=TokenResponse)
async def register(body: RegisterRequest, request: Request, response: Response):
    # Validate password
    err = validate_password_strength(body.password)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # Check username taken
    existing = await _user_repo.get_by_username(body.username)
    if existing:
        raise _coded_error(409, "AUTH_USER_EXISTS", "Username already taken")

    # Create user
    user_id = generate_id()
    hashed = hash_password(body.password)
    await _user_repo.create(id=user_id, username=body.username, password_hash=hashed, email=body.email)

    # Create default project
    from db.repository.session_repo import PgSessionRepo  # avoid circular
    from db.models.project import Project
    from db.base import get_db_session
    from project.workspace import DEFAULT_NAME, DEFAULT_SLUG
    project_id = generate_id()
    now = datetime.now(timezone.utc)
    async with get_db_session() as session:
        session.add(Project(id=project_id, user_id=user_id, name=DEFAULT_NAME, slug=DEFAULT_SLUG,
                           created_at=now, updated_at=now))

    # Issue tokens
    access = create_access_token(user_id)
    refresh = create_refresh_token(user_id)
    response.set_cookie(
        key="refresh_token", value=refresh, httponly=True, secure=False,
        samesite="lax", max_age=7 * 24 * 3600, path="/api/auth",
    )

    user = await _user_repo.get(user_id)
    return TokenResponse(access_token=access, user=_safe_user(user))


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    await _check_rate_limit(f"login:{ip}", limit=5, window=60)

    user = await _user_repo.get_by_username(body.username)
    if not user:
        raise _coded_error(401, "AUTH_INVALID_CREDENTIALS", "Invalid credentials")

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
        raise _coded_error(401, "AUTH_INVALID_CREDENTIALS", "Invalid credentials")

    # Reset failed count on success
    if user.get("failed_login_count", 0) > 0:
        await _user_repo.reset_failed_login(user["id"])

    access = create_access_token(user["id"], user.get("role", "user"))
    refresh = create_refresh_token(user["id"])
    response.set_cookie(
        key="refresh_token", value=refresh, httponly=True, secure=False,
        samesite="lax", max_age=7 * 24 * 3600, path="/api/auth",
    )

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
    from auth.logto import LogtoError, exchange_code, is_enabled, verify_id_token
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

    return await _sign_in_logto_identity(claims, response)


@router.post("/logto/id-token", response_model=TokenResponse)
async def logto_id_token(body: LogtoIdTokenRequest, request: Request, response: Response):
    """Trade a verified Logto ID token for an OpenBox session.

    The mobile app runs the PKCE flow itself through Logto's own SDK, which is
    registered as a public native client and therefore completes the code
    exchange on the device — there is no code left for this server to redeem.
    What it hands over is the resulting ID token, and JWKS verification is
    what makes that safe to trust: same signature, issuer, audience and expiry
    checks the exchange path applies, landing on the same account.
    """
    from auth.logto import LogtoError, is_enabled, verify_id_token

    if not is_enabled():
        raise HTTPException(status_code=503, detail="Logto is not configured on this server")

    ip = request.client.host if request.client else "unknown"
    await _check_rate_limit(f"logto:{ip}", limit=20, window=60)

    try:
        claims = verify_id_token(body.id_token)
    except LogtoError as e:
        log.warning(f"Logto sign-in rejected from {ip}: {e}")
        raise HTTPException(status_code=401, detail=str(e))

    return await _sign_in_logto_identity(claims, response)


async def _sign_in_logto_identity(claims: dict, response: Response) -> TokenResponse:
    """Verified Logto claims in, an OpenBox session out.

    Shared by both sign-in shapes — the web's server-side code exchange and the
    mobile SDK's ID token — so a person arriving from either client provisions
    and resolves to exactly one account.
    """
    from auth.logto import PROVIDER, derive_username

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
        from project.workspace import DEFAULT_NAME, DEFAULT_SLUG
        now = datetime.now(timezone.utc)
        async with get_db_session() as session:
            session.add(Project(id=generate_id(), user_id=user_id, name=DEFAULT_NAME,
                                slug=DEFAULT_SLUG, created_at=now, updated_at=now))

        user = await _user_repo.get(user_id)
        log.info(f"Provisioned Logto user {username} ({user_id})")

    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="Account is disabled")

    access = create_access_token(user["id"], user.get("role", "user"))
    refresh_token = create_refresh_token(user["id"])
    response.set_cookie(
        key="refresh_token", value=refresh_token, httponly=True, secure=False,
        samesite="lax", max_age=7 * 24 * 3600, path="/api/auth",
    )
    return TokenResponse(access_token=access, user=_safe_user(user))


@router.post("/refresh")
async def refresh(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
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
    response.set_cookie(
        key="refresh_token", value=new_refresh, httponly=True, secure=False,
        samesite="lax", max_age=7 * 24 * 3600, path="/api/auth",
    )

    return {"access_token": access, "token_type": "bearer"}


@router.post("/logout")
async def logout(request: Request, response: Response, current_user: dict = Depends(get_current_user)):
    # Blacklist the refresh token if present
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token and _cache:
        from auth.jwt import decode_refresh_token
        payload = decode_refresh_token(refresh_token)
        if payload:
            # Blacklist for remaining validity
            import time
            exp = payload.get("exp", 0)
            ttl = max(int(exp - time.time()), 1)
            await _cache.set(_blacklist_key(refresh_token), "1", ttl=ttl)

    response.delete_cookie("refresh_token", path="/api/auth")
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
