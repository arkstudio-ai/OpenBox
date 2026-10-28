# Android 1.0.40 (51)

This release includes the mobile waiting-state fix and accompanies the voice
handoff fixes on main. A pending task card now stops the working indicator even
when the session's busy event has not caught up; activity returns when the next
execution step starts. Both personal-assistant and original-task views use the
same content state.

## Related server changes

- Ordinary authorized task messages and revisions reach the original conversation
  without a second confirmation solely because that conversation is workspace-visible.
  Ownership, attachment access and consequential decisions retain their checks.
- Known no-effect card failures may recover the same current spoken request once;
  uncertain effects are not replayed. Legacy confirmation receipts remain scoped,
  expiring and idempotent.
- Spoken progress can use the committed submission receipt to distinguish receiving
  an instruction from executing it and finishing the requested work.
- A newer accepted instruction supersedes its stale read reply. Timer-driven waiting
  announcements no longer reuse a historical background result or old card answer.

These server changes require a separate backend deployment. Building or installing
this APK does not deploy the server or alter its configuration. No database schema
migration or new mobile protocol is required for this change.

## Focused validation

The implementation was checked with targeted tests, not the full suite:

- Task delegation and legacy-card regression batch: 114 passed.
- Voice handoff and delivery consistency batch: 101 passed.
- Waiting announcements, delivery and call-mode regression batch: 96 passed.
- Web waiting-state regression: 20 passed; TypeScript check passed.
- Flutter content-view and assistant widget regressions passed; static analysis of
  the six changed mobile files reported no issues.

These batches overlap and must not be summed as unique test cases. Real local
browser calls used synthetic microphone audio: manual card confirmation was seen
by voice, a subsequent request reached the original task exactly once with no new
confirmation card, and the completed welcome text was spoken. A separate video
flow produced a playable five-second file. This is not Android hardware acceptance.

## Release build

```bash
cd mobile
flutter build apk --release \
  --dart-define=API_BASE=https://ai.bossipai.com.cn \
  --dart-define=WEB_BASE=https://ai.bossipai.com.cn
```

Use the normal release build so the release plugin registrant is regenerated after
Flutter tests. Do not hand-edit generated Java or upgrade dependencies for packaging.
Use the existing local release keystore, excluded from Git, and verify that the
certificate matches Android 1.0.39 before distributing. Expected certificate SHA-256:
`161650a5683223010b68accbfa0894f21498746d5559a7e2e1baebbfff016196`.

Package identity is `com.bossip.bipmobile`, versionName `1.0.40`, versionCode `51`.
Final artifact hashes, signature, manifest, production-origin and alignment checks
are stored with the local release artifacts; no credentials or test audio are included.
