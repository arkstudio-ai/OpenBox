import 'dart:io' show Platform;

import 'package:audio_session/audio_session.dart';
import 'package:permission_handler/permission_handler.dart';

/// Microphone access as the call flow needs it: may we use it, may we still
/// ask, or only Settings can change it.
enum MicAccess { granted, undetermined, denied }

abstract class MicPermission {
  Future<MicAccess> status();

  /// Shows the system dialog when it still can; true when granted.
  Future<bool> request();

  /// The app's page in system Settings, where a refusal can be undone.
  Future<void> openSettings();
}

/// iOS reads AVAudioSession's own three states (no permission_handler
/// compile-time switch involved, so an Xcode-started build behaves the same
/// as `flutter build`); Android asks permission_handler, which knows a
/// "don't ask again" refusal from a first one.
class DeviceMicPermission implements MicPermission {
  const DeviceMicPermission();

  @override
  Future<MicAccess> status() async {
    if (Platform.isIOS) {
      return switch (await AVAudioSession().recordPermission) {
        AVAudioSessionRecordPermission.granted => MicAccess.granted,
        AVAudioSessionRecordPermission.undetermined => MicAccess.undetermined,
        AVAudioSessionRecordPermission.denied => MicAccess.denied,
      };
    }
    final status = await Permission.microphone.status;
    if (status.isGranted || status.isLimited) return MicAccess.granted;
    if (status.isPermanentlyDenied || status.isRestricted) {
      return MicAccess.denied;
    }
    return MicAccess.undetermined;
  }

  @override
  Future<bool> request() async {
    if (Platform.isIOS) return AVAudioSession().requestRecordPermission();
    return (await Permission.microphone.request()).isGranted;
  }

  @override
  Future<void> openSettings() async {
    await openAppSettings();
  }
}
