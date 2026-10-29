# Android audio-focus acceptance probe

This separate test APK requests real OS focus from another process. It is not part of the BossIP release app. Build its Java class against an Android SDK android.jar, package the manifest and DEX, sign with the local Android debug key, and install only on an emulator.

Run `flutter test integration_test/voice_audio_focus_test.dart -d <emulator>` with microphone permission granted. Watch stdout; for each marker run:

- `FOCUS_QA_DUCK_READY`: `adb -s <emulator> shell am start -n com.bossip.qa.focusprobe/.FocusProbe --ei kind 3 --ei millis 3000`
- `FOCUS_QA_TRANSIENT_READY`: same command with `--ei kind 2 --ei millis 70000`
- `FOCUS_QA_PERMANENT_READY`: same command with `--ei kind 1 --ei millis 3000`

The test asserts that ducking never pauses the call, transient loss survives past 60 seconds and resumes after focus returns, and a permanent loss remains manually recoverable. The websocket is an isolated test double; this test makes no business requests. Use the local real-service browser test separately for ASR and task handoff. Remove the probe APK after testing.
