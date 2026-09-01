# App Preview Origin Isolation

OpenBox proxies applications running on WUYING through a short-lived,
container-and-port-scoped HttpOnly cookie. Previewed applications are untrusted
content: they must not share a fully capable browser origin with the OpenBox
control plane.

## Safe default

`PREVIEW_PUBLIC_ORIGIN` is empty by default. Both web clients then:

- obtain the scoped cookie before mounting the preview;
- use a sandboxed iframe with scripts, forms, and sandbox-inheriting popups;
- deliberately omit `allow-same-origin`;
- do not offer an unsandboxed “open in new tab” action.

This mode keeps trusted single-user/local acceptance usable, especially for
static pages. Applications that require normal origin APIs such as
`localStorage`, module CORS, or service workers need the dedicated-origin mode.

The proxy never forwards the platform Authorization header or preview cookie
to sandbox code. It also removes sandbox-provided `Set-Cookie`,
`Clear-Site-Data`, and `Service-Worker-Allowed` response headers. Authenticated
preview responses are marked `private, no-store` so an edge/shared cache cannot
turn a cookie-protected response into public content.

## Dedicated HTTPS origin

Configure two distinct origins:

```dotenv
PREVIEW_PUBLIC_ORIGIN=https://preview.example.com
CORS_ORIGINS=https://app.example.com
CONTROL_PUBLIC_ORIGINS=https://app.example.com,https://api.example.com
```

`PREVIEW_PUBLIC_ORIGIN` must be a pathless HTTPS origin on a hostname distinct
from every exact UI/API hostname in `CORS_ORIGINS` and
`CONTROL_PUBLIC_ORIGINS`; changing only the port is rejected because cookies
are not port-scoped. Wildcard CORS is rejected. Prefer sibling
subdomains under the same registrable site; browsers that block third-party
cookies may otherwise block the scoped preview cookie inside an iframe.

Point both public hosts at the same backend if desired, but preserve the
original `Host` header. The backend enforces a virtual-host boundary:

| Request host | Allowed surface |
|---|---|
| Control/UI host | normal OpenBox API plus `GET /api/preview/config`; preview cookie/proxy paths are hidden |
| `PREVIEW_PUBLIC_ORIGIN` host | `POST`/`OPTIONS .../preview-token`, `.../preview/{port}/...`, and `GET`/`HEAD /health` only |

Everything else on the preview host returns a generic 404, including auth,
projects, sessions, files, assets, Cron, Skill/MCP, Agent, WebSocket, readiness,
and preview configuration routes. Keep the same allowlist in the edge proxy as
defence in depth; the application check remains authoritative.

The control UI is served with `Content-Security-Policy: frame-ancestors 'none'`
and `X-Frame-Options: DENY` in both repository Nginx and Vite configurations.
This is required even with distinct initial origins: an untrusted preview can
navigate its own frame, so the control document itself must refuse framing.

The browser flow is:

1. Fetch `/api/preview/config` from the control origin.
2. `POST` to the dedicated preview origin with the in-memory access JWT and
   exact control-page `Origin`.
3. The preview host validates Host, Origin, JWT, ownership, container and port.
4. It sets a HostOnly, HttpOnly, Secure, `SameSite=None` cookie scoped to that
   container/port path and returns a clean absolute preview URL.
5. The clients independently verify that the URL is HTTPS, has the expected
   path, carries no query/fragment, and has an origin different from the
   current UI before enabling `allow-same-origin` or external navigation.

The cookie has no `Domain` attribute and is never exposed in a URL. `_pt`
query-string credentials are rejected and stripped before proxying.

When dedicated preview mode is enabled, the control plane also switches its
refresh credential to `__Host-openbox_refresh_token` (`Secure`, HostOnly,
`Path=/`) and ignores the legacy unprefixed cookie. This prevents JavaScript on
a sibling preview hostname from shadowing the control credential with a
parent-`Domain` cookie. Local HTTP mode keeps the legacy cookie for backward
compatibility; HTTPS deployments without preview isolation can opt in with
`AUTH_COOKIE_SECURE=true`.

## Reverse-proxy checks

TLS should terminate at the edge and the preview virtual host should forward
`Host: $host` (or its equivalent). Do not rewrite the preview request to the
control host. At minimum verify:

```text
preview host  GET  /health                                  -> 200
preview host  GET  /api/projects                            -> 404
control host  GET  /api/containers/<id>/preview/<port>/     -> 404
preview host  POST /api/containers/<id>/preview-token       -> 401 without JWT
preview host  POST /api/containers/<id>/preview-token       -> 403 for a non-allowlisted Origin
```

The authorized POST response must echo the exact allowed CORS origin, allow
credentials, return an absolute `PREVIEW_PUBLIC_ORIGIN` URL, and set a cookie
with `HttpOnly; Secure; SameSite=None`, the exact preview path, and no `Domain`.
