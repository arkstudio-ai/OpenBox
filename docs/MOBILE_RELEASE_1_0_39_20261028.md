# Android 1.0.39 (50)

This release packages the existing mobile client against the voice-delegation
server release merged in PR #102 (`21ed11e9`). The mobile review started from
`32680ef5`. No additional mobile business-logic changes were necessary; the
version/build number increases from 1.0.38+49 to 1.0.39+50.

## Compatibility review

- The voice socket already accepts `playback.clear`, and the controller clears
  queued playback without ending the microphone/call. The server decides which
  card/task change is related to the current reply. The mobile client keeps its
  existing playback-completion fallback; it does not advertise playback ACKs.
- The personal-assistant and original-task pages submit versioned, scoped card
  replies. Closed-card tombstones prevent stale HTTP/WS data resurrecting a
  handled question. Assistant events refresh canonical task projections.
- Deferred sibling operations, contextual handoff and saved model-choice
  recovery are server behavior using existing client contracts.
- Task outcomes already distinguish errors from success. Publication failure
  and unknown-effect handling continue to come from server receipts; submitted
  content does not certify platform approval or public visibility.

## Focused verification

102 tests passed across these 10 files. No full test suite was run:

```text
test/features/voice/voice_call_controller_test.dart
test/features/voice/voice_call_reducer_test.dart
test/features/voice/pcm_audio_test.dart
test/features/voice/voice_call_banner_test.dart
test/features/chat/assistant_cards_test.dart
test/features/chat/assistant_test.dart
test/features/chat/question_lifecycle_test.dart
test/features/chat/question_answered_test.dart
test/features/chat/assistant_watch_summary_test.dart
test/features/chat/assistant_linking_test.dart
```

No additional Android physical-device call, paid video generation or public
publication test was performed in this packaging task. The preceding server
release has separate browser/voice acceptance evidence; those checks are not
Android hardware acceptance.

## Build and package

```bash
cd mobile
flutter build apk --release \
  --dart-define=API_BASE=https://ai.bossipai.com.cn \
  --dart-define=WEB_BASE=https://ai.bossipai.com.cn
```

Run the normal release build after Flutter tests. In this environment, using
`--no-pub` immediately after tests left the generated Android plugin registrant
referencing `integration_test` while Gradle excluded that dev-only dependency
from release. The normal build regenerated the release registrant and completed
successfully. No generated Java was hand-edited, dependencies were not upgraded,
and no test microphone was included in the release artifact.

- Package: `com.bossip.bipmobile`; versionCode: `50`; versionName: `1.0.39`.
- Production API/web origin is present in all three compiled ABIs.
- ABIs: arm64-v8a, armeabi-v7a, x86_64. targetSdk: 36.
- Non-debuggable; release signature verifies and matches 1.0.38.
- Certificate SHA-256:
  `161650a5683223010b68accbfa0894f21498746d5559a7e2e1baebbfff016196`.
- 16 KiB ZIP alignment and all nine 64-bit native ELF library alignments pass.
- APK size: 90,092,040 bytes.
- APK SHA-256:
  `7c46a88c188d7b066b951a9c47846458872e22a042c5de671782adc6db0178e4`.

The 7z contains only the signed APK, Chinese release notes and SHA256SUMS.txt.
Use an in-place upgrade; do not uninstall the previous signed version first.
The online model, billing, memory switches and channel settings are unchanged
by this mobile package.

Local release evidence lives in
`.local-dev/mobile-voice-delegation-release/`: targeted test and build logs,
signature/badging/alignment reports, archive verification, artifact metadata
and Feishu delivery screenshots. Those runtime artifacts and signing secrets
are excluded from Git.
