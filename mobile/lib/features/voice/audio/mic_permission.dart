import 'dart:async';
import 'dart:io' show Platform;

import 'package:permission_handler/permission_handler.dart';
import 'package:record/record.dart';
import 'package:shared_preferences/shared_preferences.dart';

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

/// iOS asks `record` (AVCaptureDevice): audio_session's permission calls are
/// compiled out unless the build defines AUDIO_SESSION_MICROPHONE, which its
/// Swift package reads from the build's environment — every build would have
/// to remember it, and without it the answer was always "denied". `record`
/// only knows granted or not, so whether the dialog was shown is kept here.
/// Android asks permission_handler, which knows a "don't ask again" refusal
/// from a first one.
class DeviceMicPermission implements MicPermission {
  const DeviceMicPermission();

  static const _askedKey = 'voice.micAsked';

  @override
  Future<MicAccess> status() async {
    if (Platform.isIOS) {
      if (await _ios(request: false)) return MicAccess.granted;
      final prefs = await SharedPreferences.getInstance();
      return prefs.getBool(_askedKey) == true
          ? MicAccess.denied
          : MicAccess.undetermined;
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
    if (Platform.isIOS) {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setBool(_askedKey, true);
      return _ios(request: true);
    }
    return (await Permission.microphone.request()).isGranted;
  }

  @override
  Future<void> openSettings() async {
    await openAppSettings();
  }

  static Future<bool> _ios({required bool request}) async {
    final recorder = AudioRecorder();
    try {
      return await recorder.hasPermission(request: request);
    } finally {
      unawaited(recorder.dispose());
    }
  }
}
