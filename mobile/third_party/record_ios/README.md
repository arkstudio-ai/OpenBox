# record_ios (vendored)

Upstream: https://github.com/llfbandit/record — pub `record_ios` 2.1.1, the
version `record` 7.1.1 resolves to. MIT (see LICENSE).

The voice call streams the microphone through it
(`lib/features/voice/audio/pcm_capture.dart`).

Vendored for one reason: `RecorderStreamDelegate.start` installs its tap with
the input node's format without checking it. When there is no usable input —
the iOS Simulator on a Mac without a microphone, or an input that is gone for
a moment during a route change or a phone call — that format is 0 Hz /
0 channels and `installTap` raises an Objective-C exception, which Swift
cannot catch: the app crashed the moment a call started. The patch (marked
"OpenBox patch") throws the plugin's ordinary start error instead, which the
call shows as a microphone problem.

Only that guard is added; everything else is the published package without
its example app. Drop the override in `pubspec.yaml` once upstream checks the
format itself.
