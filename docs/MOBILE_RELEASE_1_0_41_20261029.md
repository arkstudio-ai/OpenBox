# Android 1.0.41 (52)

This release fixes mobile voice interruption recovery and clarifies idle, hearing
and playback states. The matching server changes preserve the configured video
tier names and make a failed handoff check visible instead of silently dropping
the spoken request. Source revision: `98091a2c56091884a53a7c0fee3593e97c0accc7`.

## Changes

- A duckable media interruption no longer pauses the call or claims that an
  incoming phone call was detected. A real audio interruption displays a generic
  audio-ownership message, attempts recovery when audio is available, and offers
  a manual resume control. The former 60-second interruption hangup is removed.
- Resume attempts are bounded and guarded against a newer interruption or hangup.
  An actual CallKit interruption still blocks capture until it ends. Paused calls
  retain their existing connection and silent keepalive behavior.
- Idle listening is labelled separately from detected user speech. The optional
  `phase.hearing` event field carries that distinction. Mobile playback remains
  visible until the native PCM queue drains, rather than ending at the server's
  response-completion event.
- Video choices expose all configured tiers independently. MiniMax H3 retains
  the configured flexible-tier name; it is not collapsed into the standard tier.
  Existing task model selections remain unchanged. Spoken comparisons obtain
  current model capabilities before making claims.
- A failed handoff check retries once when the same request is still current.
  Authorization refers to server-bound human utterances instead of requiring the
  model to reproduce an exact quote. Failure remains non-authorizing and emits a
  nonfatal notice. Original wording, context and constraints are preserved.
- Optional `client.audio` events report pause/resume/failure metadata for future
  diagnosis; they do not upload microphone recordings.

## Focused validation

Only relevant tests were run; no full test suite was used:

- Backend voice, handoff, tools and video workflow batch: 179 passed.
- Backend voice prompt and routing regression batch: 27 passed.
- Web voice reducer and dock: 37 passed; TypeScript build check passed.
- Flutter voice controller, reducer, PCM player, page and banner: 92 passed.
  A final 38-case controller rerun overlaps that batch and is not additional
  coverage. Static analysis reported no issues.
- One Android API 36 native integration scenario passed. A separate process
  requested real Android audio focus: ducking did not pause capture; a 70-second
  transient interruption survived beyond the removed 60-second deadline and
  recovered; a permanent focus transfer recovered using the actual resume button.
  It used real native capture/playback with an isolated fake websocket transport.
- A real local browser call used synthetic microphone audio and the official
  voice service. Initial silence produced no false speech event. A natural request
  for a five-second flexible-tier fruit-shop plan reached the text assistant;
  manual form choices created a task using MiniMax H3 and stopped at plan
  confirmation, with zero video generation jobs. A second call looked up the
  catalog and correctly distinguished flexible and standard capabilities.

The browser and emulator checks cover different parts of the stack. They are not
physical-phone or real cellular incoming-call acceptance, and no new iOS simulator
run was performed. The historical silent-speech report lacked captured audio, so
this release does not claim to prove the cause of every possible VAD false positive.

## Packaging and deployment

Package: `com.bossip.bipmobile`; versionName `1.0.41`; versionCode `52`.
The release is built with the existing production signing key and production
`API_BASE` / `WEB_BASE` defines. It can overwrite the previous installation.

- APK SHA-256: `d3179f26f7ae08e296b67847da5056a26beecacc458f32935980f3391179dc45`.
- Signing certificate SHA-256:
  `161650a5683223010b68accbfa0894f21498746d5559a7e2e1baebbfff016196`.
- 7z SHA-256: `24899dec145f4f50d2ab9e3a56ca1a3f72af1b94ac5386482a3917ce61d23132`.
- APK ZIP integrity, production origins, non-debuggable manifest, signature and
  16 KB ZIP alignment were checked. The archive contains only the APK, release
  notes and checksum file; no test audio, focus-probe application or credentials.

The server and web changes require their own deployment. No new database migration
or environment variable is required. Production voice remains on the official
channel; other model traffic and rollback-era feature settings remain unchanged.
Deployment results are recorded in `DEPLOY.md` and the matching evidence JSON.
