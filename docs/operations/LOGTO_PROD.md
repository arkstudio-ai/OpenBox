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
`/api/auth/logto/config`; the app reads it in
`mobile/lib/shared/api/logto_session.dart` and only shows the SSO button when
it is non-empty.

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
  `.AuthCallbackActivity` intent-filter `android:scheme="com.bossip.bipmobile"`
  and `android:host="callback"`.
- The Logto **bossip-mobile** application's registered Redirect URIs.

`com.bossip.bipmobile://callback` is registered on bossip-mobile (added
2026-09-04). A stale `io.bossip.mobile://callback` entry is also present but
matches nothing in the app — safe to delete.

The same `com.bossip.bipmobile://callback` URI is registered under **Post
sign-out redirect URIs**. The app uses it as
`Env.ssoPostLogoutRedirectUri` (overridable with
`--dart-define=SSO_POST_LOGOUT_REDIRECT_URI=...`). Keep both Logto lists and
both app build values aligned if the scheme changes.

Leave `LOGTO_NATIVE_APP_ID` unset to turn mobile SSO off (the app falls back to
its account/password form).

### Android callback task restoration (1.0.16 / 2026-09-09)

Chrome Custom Tabs can deliver the custom-scheme redirect with `NEW_TASK`.
With isolated/empty task affinities, the stock plugin callback can receive the
URI in a different task, complete authentication in Dart, then finish an empty
authentication-manager activity while the original Chrome tab still covers
Flutter. This reproduces “already authorized, but must manually return”.

`AuthCallbackActivity` now validates the callback action, scheme, host, port and
path; removes/delivers the pending plugin callback once; and explicitly starts
`MainActivity` in its original `ActivityManager.AppTask` with
`CLEAR_TOP | SINGLE_TOP`. If the OS removed that task it opens a fresh app task.
The OAuth URI is never forwarded to Flutter routing or written to logs. Logto
still performs state/redirect/PKCE/token verification. A callback after process
death does not recreate authentication: the user returns to retry safely.

Keep both task affinities empty. Do not fix this by making
`AuthenticationManagementActivity` `singleTask`: that creates a separate auth
task and regresses Home/recents return. Newer browser Auth Tabs continue to use
the plugin's ActivityResult path; the bridge handles custom-scheme fallback.

The official [Flutter SDK](https://docs.logto.io/quick-starts/flutter) uses a
system authentication browser on Android. A native SDK does not mean a native
username/password form: Logto [does not expose a headless sign-in/sign-up API](https://docs.logto.io/end-user-flows/sign-up-and-sign-in).
Preserve the hosted OIDC flow and automatic app return instead of capturing
Logto passwords in an embedded custom login implementation.

## Sign-out contract

Signing out has two independent layers and both are required:

1. The OpenBox refresh token must be revoked and its cookie expired; each
   client also clears its in-memory access token, user/workspace scope and live
   socket.
2. The centralized Logto browser session must enter the OIDC end-session flow.

The two clients deliberately use different orchestration appropriate to their
runtime:

- **Web:** the menu performs one full-page navigation to
  `GET /api/auth/logto/logout`. That backend response revokes the OpenBox
  refresh token, expires its cookie, and immediately returns a 302 to
  `{LOGTO_ISSUER}/session/end` with `client_id` and the registered
  `post_logout_redirect_uri`. Do **not** clear the SPA auth store or navigate to
  `/login` first: `SsoEntry` can otherwise start a fresh authorization request
  while the end-session request is still in flight, silently signing the same
  user back in. This is the same single-navigation boundary used by the working
  `workspace/bossip` implementation.
- **Mobile:** the controller calls `POST /api/auth/logout`, clears local
  OpenBox/workspace/socket state, then calls the official Dart SDK's
  `LogtoClient.signOut(postLogoutRedirectUri)`. The SDK clears its stored
  tokens and, on platforms with a persistent browser session, completes the
  same end-session redirect.

Do not replace step 2 with a route back to the landing page. That only signs
out of OpenBox; Logto's browser cookie survives and the next sign-in can return
to the old account without an account prompt. The implementation follows
[Logto's sign-out flow](https://docs.logto.io/end-user-flows/sign-out) and
[Flutter quick start](https://docs.logto.io/quick-starts/flutter).

Mobile additionally deletes the SDK's three local token entries in a `finally`
path. Therefore a discovery/revocation/browser failure cannot restore the
local session; centralized logout remains best-effort when the identity server
itself is unreachable.

Web authorize requests include `prompt=login consent`: `login` protects the
persistent browser flow from silently restoring the account after an
interrupted logout, while `consent` is retained for `offline_access`.

Mobile authorize requests use `prompt=consent`. The current Dart SDK opens an
ephemeral browser session and mobile sign-out explicitly completes Logto's
end-session flow, so the additional `login` prompt is unnecessary. On Android
against the production Logto deployment, `login consent` sent a newly verified
interaction through `/oidc/session/end/confirm` and left Chrome on a blank
`Submitting Callback` page. Keeping the same PKCE request and changing only the
prompt to `consent` produced the registered custom-scheme callback and completed
the OpenBox token exchange.

Production registration was read back from the self-hosted Logto database on
2026-09-06:

- `bossip-web`: `https://ai.bossipai.com.cn/callback` is a Redirect URI and
  `https://ai.bossipai.com.cn` is a Post sign-out redirect URI.
- `bossip-mobile`: `com.bossip.bipmobile://callback` is present in both lists.

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

# 3. Web logout is a single server-side redirect (do not use -L here)
curl -sS -D - -o /dev/null https://ai.bossipai.com.cn/api/auth/logto/logout
#    expect: HTTP 302, refresh_token Max-Age=0, and Location beginning with
#            https://auth.bossipai.com.cn/oidc/session/end
```

Then click **Sign in** on `https://ai.bossipai.com.cn/login`: it must land on
`auth.bossipai.com.cn`, and after login return to `.../callback` and complete —
no `invalid redirect_uri` and no `invalid_client`.

Finally click **Sign out**. The browser must visit
`auth.bossipai.com.cn/oidc/session/end`, return to the app origin, and a new
sign-in must show Logto's login page instead of silently reusing the previous
account. On Android the SDK briefly opens the same browser flow and returns
through the custom scheme;
on iOS the SDK uses an ephemeral authentication session and still clears its
secure token store.

Production acceptance completed on 2026-09-06 with image tag
`20260906-logto-logout3-3586742`: the HTTP contract above passed, all four
containers were healthy, and the identical single-navigation logout
implementation signed `andrewwang` out to the landing page in a real Chrome
session. After the final prompt-only rebuild, starting sign-in again stopped
on Logto's credential page; it did not return silently to the previous OpenBox
session.

## Admin console access (ops note)

The Logto admin console is pinned to `ADMIN_ENDPOINT=http://localhost:3002`, so
it can only be opened over a local tunnel on port **3002** (not the public
domain). See `backend/scripts/logto_tunnel.sh` — open `http://localhost:3002/console`.
