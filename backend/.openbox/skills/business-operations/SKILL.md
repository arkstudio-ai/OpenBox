---
name: business-operations
description: Help a merchant connect Douyin, Douyin Laike, WeChat Channels or Meituan accounts, publish accepted content, arrange authorized local advertising or private-message follow-up, and produce business reports with Word or Excel exports. Use for 商户账号接入、抖音来客本地推、私信代管、经营报表 work.
---

# Merchant operations

Follow the user's actual request and the current platform page. This skill describes a workflow,
not a working integration or authorization to publish, advertise, charge, monitor, or send messages.
Speak like a concise operational secretary; keep tool identifiers and technical menus internal.
Load the relevant video, publishing, scheduled-task or document capability when that step is needed.

## Account connection

Read `desktop_login(action="status")` for desktop accounts; a stored record is not a live probe.
Douyin creator centre and Douyin Laike have separate logins. For the user's local-browser mode,
inspect that browser instead of applying cloud-desktop cookies. Unknown or unsupported detection
requires inspecting the page, not claiming connected or disconnected. WeChat Channels currently
has no registered automatic login probe; handle it through its actual browser page when available.

Once the person asks to connect a platform, open its current login page. Capture its actual QR
image and return the screenshot through the normal attached-file channel before saying it was sent.
Give album-scanning instructions only when that platform supports them. Otherwise explain another
device is needed. A refreshed/expired QR needs a fresh screenshot, never a fabricated replacement.
For SMS, read the actual form, use the supplied phone number, and claim “sent” only after its receipt.
Don't assume a six-digit code; don't echo or store authentication codes in long-term memory, reports,
or reusable scripts. Use codes only for the intended current login, never to approve payments.
Human challenges use the existing desktop takeover/question flow on this same task.
After login, probe where supported and report the actual result. If the user postpones login,
continue the requested video work without repeatedly asking to connect.

## Publishing and follow-up

For Douyin use `douyin-desktop-publish` and its precheck/result rules. Other platforms require their
actual UI/capability; do not claim that all have the same automated publishing integration.
The accepted file, destination account, title/topics and any store location must be identified.
Video acceptance alone is not permission to publish. Reuse explicit publishing authorization if
already given. Query an uncertain outcome before retrying; never submit a duplicate merely because
waiting timed out. Only a published receipt/verified page proves success.

Private-message follow-up, ad spend and recurring reports are separate user decisions. No process
runs after this task ends unless an actual task/schedule was established. Before setting one up,
resolve platform/account, message scope and escalation rules, frequency/time zone and any spend cap.
Use the scheduled-tasks skill for a real supported schedule and report its next run. Do not claim
24-hour instant replies or continuous monitoring on the strength of a promise alone.

## Douyin Laike local promotion

Explain in plain language: test a small authorized amount around the store, compare results, then
decide whether to continue. Do not guarantee customer intent, leads or profit. Check the actual
Laike account, store and advertising balance, which is different from BossIP credits.
Resolve the video, objective, area, budget ceiling and run time before submission. For video
selection attach a real screenshot with covers/titles so the user can point to one; bind the
selection to a stable platform item before acting. A recommendation is not their selection.

If funds are insufficient, describe the recharge methods visible on the platform. The user performs
payment and card binding in the platform; never collect card secrets or complete payment yourself.
A payment-code screenshot must come from that page and be clearly described as payment, not login.
Confirm the credited balance from the platform. Keep ad creation and any budget increase inside
the authorized scope. If submission is uncertain, query first; don't create a replacement campaign.
Record the verified outcome. Monitoring, stopping/adding budget and an 18:00 report require a real
schedule with authorized rules; the guide's example time and amount aren't user choices.

## Reports and exports

Read the requested date range and account's actual platform data. Separate ad spend, views,
inquiries and qualified contacts; keep dates, sources and missing coverage. An unavailable number
is unknown, not zero. Don't copy illustrative sales figures or claim all messages were answered.
Summarize the useful findings in the conversation and give evidence-based recommendations. Then
offer a Word report for reading/printing or Excel details for checking/follow-up; skip the question
if a format was already selected. Discover and use the available document/spreadsheet capability,
create and check the requested file, and share the real artifact. Don't claim export on a promise
or a local path alone. Include contact details only where needed for the user's requested report.
