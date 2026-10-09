# flutter_pcm_sound (vendored)

Upstream: https://github.com/chipweinberger/flutter_pcm_sound — pub
`flutter_pcm_sound` 3.3.3, the newest release. Public domain (see LICENSE).

The voice call plays the assistant's 24 kHz PCM through it
(`lib/features/voice/audio/pcm_player.dart`): a RemoteIO unit on iOS, an
AudioTrack on Android, fed from Dart on demand.

Vendored for one reason: the published `android/build.gradle` compiles
against android-33. AGP 9 checks a library's compileSdk against the AAR
metadata of its dependencies (`androidx.fragment` 1.7, `androidx.window` 1.2
and others need 34 or later), so `flutter build apk` failed at
`:flutter_pcm_sound:checkDebugAarMetadata`.

`android/build.gradle` is rewritten (compileSdk 36, minSdk 24, Java 17 —
the same values as `third_party/video_thumbnail`). The example app and the docs site are left out.

The Dart setup API and iOS plugin also accept `iosManageAudioSession`
(default `true`). Voice calls set it to `false` because CallKit has already
configured and activated the audio session; resetting its category or calling
`setActive` from the PCM player would interfere with system call priority.
Other consumers keep the original behavior. The Java plugin and podspec are
unchanged.

If upstream ever ships a build script that compiles against 34 or later,
delete this directory and put `flutter_pcm_sound: ^3.3.3` back in
pubspec.yaml.
