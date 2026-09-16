# w3-frontend report

The idle admin session page now makes at most 4 requests a minute with a healthy socket. Internal endpoints are blocked at nginx and the access log has request timing. All required checks pass. Playwright wasn't run (out of scope), and two of its specs will probably break; see open issue 1.

**Branch:** `wp/w3-frontend`, based on 7c6b5ba, not pushed.
**Head:** `0863663f3d24d122f67a3e5da5dff7bc9ec6b1d1`. There are two commits: d75142b for polling and 0863663 for nginx.

## Summary

**Polling budget**
- **Events:** while the socket is connected the poll is now every 30 s instead of 10 s. The 2 s poll while disconnected, the hidden-tab limit and the immediate read when the tab is shown again are unchanged.
- **Header and list probe:** each is read again 60 s after its last answer while the socket is connected, and 30 s while it isn't (same as before). I replaced TanStack's fixed interval with a small hook in `queries.ts`. It times the next read from the last answer, so a socket that keeps dropping and reconnecting can't hold reads off.
- **Hints:** `trajectory.available` still reads events and the header at once, unchanged.
- **Payload `?meta=1`:** runs only while the payload is actually on screen, using a new `useOnScreen` hook. If a payload scrolls back into view after the check is overdue, it is checked at once. A check on tab visibility change still happens. `usePayload`'s fourth argument is now an options object, because lint allows at most 4 parameters. Checks on servers without `?meta=1` support are unchanged.
- **Budget test:** it renders the real live session page with fake timers and an open socket. Over 5 idle minutes it counts 15 requests (events 10, header 5); the old code would make 40. A second test checks that a hint reads events and the header at once.

**nginx**
- **Access log:** the new `log_format openbox_timing` is the image's `main` format followed by ` rt=$request_time urt=$upstream_response_time`. The server block now sets `access_log /var/log/nginx/access.log openbox_timing;`.
- **Internal endpoints:** `location ^~ /api/internal/ { return 404; }` sits before `/api/`.
- **Test script:** checks that `/api/internal/...` returns 404 and never reaches the fixture, including POST, `//` and `%69` spellings. It compares the new format against the image's own `main`. It also parses log lines: a proxied request keeps the client IP as the last quoted field with numeric `rt=`/`urt=`, and a refused one logs `urt=-`. All existing routing assertions still pass.

## Changed files
All under `/Users/wang/workspace/OpenBox/.claude/worktrees/agent-a85b32683aaad5e2d/frontend-v2/`:
- `src/features/admin-trajectories/constants/polling.ts` and `polling.test.ts`
- `src/features/admin-trajectories/api/queries.ts` and `queries.test.tsx`
- `src/features/admin-trajectories/hooks/useTrajectorySync.ts` (comment only) and `useTrajectorySync.test.tsx`
- `src/features/admin-trajectories/components/inspector/PayloadView.tsx`, `useOnScreen.ts` (new) and `MediaRefView.test.tsx`
- `src/features/admin-trajectories/components/TrajectorySessionPage.budget.test.tsx` (new)
- `nginx.conf`
- `scripts/test-nginx.mjs`

## Tests
| Command | Result |
|---|---|
| `npx vitest run src/features/admin-trajectories` | 34 files, 256 passed, 0 failed, 0 skipped |
| `npx vitest run` | 125 files, 867 passed, 0 failed, 0 skipped (before my changes: 124 files, 859 tests) |
| `npm run build && NGINX_TEST_IMAGE=nginx:1.31.5-alpine npm run test:nginx` | PASS, exit 0, containers and network removed |

`tsc -b`, ESLint and Prettier on the changed files are also clean.

## Deviations
- **Script path:** the nginx test lives at `frontend-v2/scripts/test-nginx.mjs`; there is no `scripts/test-nginx.mjs` at the repo root.
- **List probe:** it isn't on the session page at all, only on the list page, so the old idle count was about 8 a minute, not 11. The list page never opens the socket, so its probe stays at 30 s. Slowing the list page too would be a one-constant change.
- **"Actually displayed":** I took this to mean on screen, not just mounted. A payload deleted while scrolled out of view is noticed when it comes back into view, or on a tab visibility change.
- **SPEC §11.2:** it still describes the old 10 s / 30 s / 30 s intervals. I didn't edit it because it isn't my file.

## Open issues
1. **Playwright:** two specs in `frontend-v2/e2e/trajectories.spec.ts` will likely fail.
   - "deleting the watched session clears what was shown" waits 15 s. The fixture's `deleteSession` (`e2e/helpers/trajectory-server.ts:353`) sends no `deleted: true` hint, so the page only notices on the next 30 s events poll.
   - "refused read (403)" waits 15 s and now depends on the 15 s payload re-read, which is borderline.
   - Fix: have the fixture send the deletion hint like the real worker does, or raise those timeouts to at least 35 s.
2. **Deletion speed in production:** it now depends on the worker sending `deleted: true` hints (owned by w3-harden-service). Without them, a deletion shows up within 30 s instead of 10 s.
3. **No trailing slash:** `/api/internal` without a slash still goes through `/api/` to the backend. The backend has no route at that exact path.
4. **No new settings.**
