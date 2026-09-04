# Logto SSO — production deployment (Aliyun)

How OpenBox wires up Logto in each environment, and the exact values to use when
deploying the production stack behind **https://ai.bossipai.com.cn**.

## How the redirect URI flows through the stack

The frontend never hard-codes the OIDC settings. It reads them from the backend:

```
browser → GET {VITE_API_URL}/api/auth/logto/config
        ← { enabled, endpoint, issuer, app_id, redirect_uri, post_logout_redirect_uri }
```

`redirect_uri` / `post_logout_redirect_uri` are returned straight from the
backend's `LOGTO_REDIRECT_URI` / `LOGTO_POST_LOGOUT_REDIRECT_URI` env vars
(`backend/auth/routes.py::logto_config`). The browser then builds
`{endpoint}/oidc/auth?...redirect_uri=...` and, after the redirect, the backend
does the code→token exchange server-side using the **same** `redirect_uri`
(`backend/auth/routes.py::logto_exchange`).

Two consequences:

1. The **backend env vars are the single source of truth** — set them per env.
2. Every `redirect_uri` you use **must be registered on the Logto application**,
   or Logto rejects the authorize request. (CORS allowlist is *not* needed —
   the token exchange is server-to-server.)

## Environment matrix

| Setting | dev (local) | prod (Aliyun) |
|---|---|---|
| Frontend origin | `http://localhost:3000` | `https://ai.bossipai.com.cn` |
| `LOGTO_ENDPOINT` | `https://account.rankgale.ai` | `https://auth.bossipai.com.cn` |
| Logto application | (dev app) | **bossip-web** (Traditional Web) |
| `LOGTO_APP_ID` | `f8cc4vjoshp0ewkh76uoj` | `m9383f921ea87ocylm40p` |
| `LOGTO_APP_SECRET` | from console | from console (**required**, secret) |
| `LOGTO_REDIRECT_URI` | `http://localhost:3000/callback` | `https://ai.bossipai.com.cn/callback` |
| `LOGTO_POST_LOGOUT_REDIRECT_URI` | `http://localhost:3000` | `https://ai.bossipai.com.cn` |
| `LOGTO_ISSUER` | unset → `{endpoint}/oidc` | unset → `{endpoint}/oidc` |
| `LOGTO_JWKS_URI` | unset → `{endpoint}/oidc/jwks` | unset → `{endpoint}/oidc/jwks` |

`account.rankgale.ai` and `auth.bossipai.com.cn` are two **different** Logto
servers with different signing keys — they are not interchangeable.

## Production backend env (`backend/.env` on the prod host)

```dotenv
LOGTO_ENDPOINT=https://auth.bossipai.com.cn
LOGTO_APP_ID=m9383f921ea87ocylm40p
LOGTO_APP_SECRET=            # copy from Logto Console → Applications → bossip-web → App Secret
LOGTO_REDIRECT_URI=https://ai.bossipai.com.cn/callback
LOGTO_POST_LOGOUT_REDIRECT_URI=https://ai.bossipai.com.cn
# LOGTO_ISSUER / LOGTO_JWKS_URI: leave unset, they default to {endpoint}/oidc[/jwks]
```

- `bossip-web` is a **Traditional Web** (confidential) app, so `LOGTO_APP_SECRET`
  is **required**. Missing it → `oidc.invalid_client` at the token endpoint even
  though the sign-in screen worked.
- Keep the secret out of git. Inject it via the host's secret store / CI secret /
  the untracked `backend/.env` on the server.

## Production frontend env

`frontend-v2` reads only `VITE_API_URL` (`src/shared/config/env.ts`):

- **Same origin** (frontend and API both under `https://ai.bossipai.com.cn`):
  leave `VITE_API_URL` empty → the app calls `/api/...` relatively.
- **Separate API origin**: set `VITE_API_URL=https://<api-host>` at build time.

## Registered redirect URIs on `bossip-web`

Added 2026-09-04 (this deployment), alongside the pre-existing bossip domains:

- Redirect URIs: `https://ai.bossipai.com.cn/callback`
- Post sign-out redirect URIs: `https://ai.bossipai.com.cn`

If the prod origin ever changes, register the new `<origin>/callback` on
bossip-web **before** flipping `LOGTO_REDIRECT_URI`.

## Mobile (Flutter native app)

The phone app is a **separate** Logto application because a native client cannot
keep a secret — it is a public client, and its App ID is a second accepted
ID-token audience. The backend hands it to the app via `native_app_id` in
`/api/auth/logto/config`; the app reads it in `mobile/lib/features/auth/api/logto.dart`
and only shows the SSO button when it is non-empty.

| Setting | Value |
|---|---|
| Logto application | **bossip-mobile** (Native App, public client) |
| `LOGTO_NATIVE_APP_ID` | `h4cxokmv8yy3w5vge24fn` |
| Redirect URI | `com.bossip.bipmobile://callback` |

**The redirect URI is a custom scheme, not a web URL, and must match on all three
sides:**

- `mobile/lib/shared/config/env.dart` → `Env.ssoRedirectUri` default
  `com.bossip.bipmobile://callback` (overridable at build with
  `--dart-define=SSO_REDIRECT_URI=...`).
- `mobile/android/app/src/main/AndroidManifest.xml` → the
  `flutter_web_auth_2.CallbackActivity` intent-filter `android:scheme="com.bossip.bipmobile"`.
- The Logto **bossip-mobile** application's registered Redirect URIs.

`com.bossip.bipmobile://callback` is registered on bossip-mobile (added
2026-09-04). A stale `io.bossip.mobile://callback` entry is also present but
matches nothing in the app — safe to delete.

Leave `LOGTO_NATIVE_APP_ID` unset to turn mobile SSO off (the app falls back to
its account/password form).

## Smoke test after deploy

```bash
# 1. Backend exposes the prod config
curl -s https://ai.bossipai.com.cn/api/auth/logto/config | jq
#    expect: endpoint=https://auth.bossipai.com.cn,
#            app_id=m9383f921ea87ocylm40p,
#            native_app_id=h4cxokmv8yy3w5vge24fn,   # only if LOGTO_NATIVE_APP_ID is set
#            redirect_uri=https://ai.bossipai.com.cn/callback

# 2. Logto OIDC discovery is reachable
curl -s https://auth.bossipai.com.cn/oidc/.well-known/openid-configuration | jq .issuer
#    expect: https://auth.bossipai.com.cn/oidc
```

Then click **Sign in** on `https://ai.bossipai.com.cn/login`: it must land on
`auth.bossipai.com.cn`, and after login return to `.../callback` and complete —
no `invalid redirect_uri` and no `invalid_client`.

## Admin console access (ops note)

The Logto admin console is pinned to `ADMIN_ENDPOINT=http://localhost:3002`, so
it can only be opened over a local tunnel on port **3002** (not the public
domain). See `backend/scripts/logto_tunnel.sh` — open `http://localhost:3002/console`.
