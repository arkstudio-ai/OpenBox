import 'dart:io' show Platform;

import 'package:audio_session/audio_session.dart';

/// iOS outputs that take the call off the phone's own speaker/earpiece.
const externalIosOutputs = {
  AVAudioSessionPort.headphones,
  AVAudioSessionPort.bluetoothA2dp,
  AVAudioSessionPort.bluetoothHfp,
  AVAudioSessionPort.bluetoothLe,
  AVAudioSessionPort.usbAudio,
  AVAudioSessionPort.carAudio,
  AVAudioSessionPort.airPlay,
  AVAudioSessionPort.lineOut,
};

/// Android output devices that do the same.
const externalAndroidOutputs = {
  AndroidAudioDeviceType.wiredHeadset,
  AndroidAudioDeviceType.wiredHeadphones,
  AndroidAudioDeviceType.bluetoothSco,
  AndroidAudioDeviceType.bluetoothA2dp,
  AndroidAudioDeviceType.usbHeadset,
  AndroidAudioDeviceType.hearingAid,
  AndroidAudioDeviceType.lineAnalog,
  AndroidAudioDeviceType.lineDigital,
};

/// The call's audio session (docs/VOICE_CALL_MOBILE.md §4).
///
/// iOS: `playAndRecord` + `voiceChat` (the system's voice processing) with
/// Bluetooth headsets allowed. `defaultToSpeaker` is deliberately left out:
/// with it the default route *is* the speaker and the earpiece could only be
/// reached by changing the category mid-call. Instead the default is the
/// earpiece and [setSpeaker] overrides the route to the speaker.
///
/// Android: voice-communication attributes, transient focus (music resumes
/// after the call), the communication audio mode.
class CallAudioSession {
  CallAudioSession({this.systemManaged = false});

  /// CallKit activates/deactivates iOS call audio at system priority.
  final bool systemManaged;
  static const _configuration = AudioSessionConfiguration(
    avAudioSessionCategory: AVAudioSessionCategory.playAndRecord,
    avAudioSessionCategoryOptions: AVAudioSessionCategoryOptions.allowBluetooth,
    avAudioSessionMode: AVAudioSessionMode.voiceChat,
    avAudioSessionRouteSharingPolicy:
        AVAudioSessionRouteSharingPolicy.defaultPolicy,
    avAudioSessionSetActiveOptions:
        AVAudioSessionSetActiveOptions.notifyOthersOnDeactivation,
    androidAudioAttributes: AndroidAudioAttributes(
      contentType: AndroidAudioContentType.speech,
      usage: AndroidAudioUsage.voiceCommunication,
    ),
    androidAudioFocusGainType: AndroidAudioFocusGainType.gainTransient,
    androidWillPauseWhenDucked: true,
  );

  AudioSession? _session;
  (AVAudioSessionCategory, AVAudioSessionCategoryOptions, AVAudioSessionMode)?
  _saved;

  Future<AudioSession> get _instance async =>
      _session ??= await AudioSession.instance;

  Stream<AudioInterruptionEvent> get interruptions async* {
    yield* (await _instance).interruptionEventStream;
  }

  Stream<AudioDevicesChangedEvent> get deviceChanges async* {
    yield* (await _instance).devicesChangedEventStream;
  }

  /// Configures and activates the session. False when the system refused
  /// (a phone call holds the audio).
  Future<bool> open() async {
    final session = await _instance;
    if (Platform.isIOS && !systemManaged) {
      // Put back whatever the app used before (video playback relies on it).
      final av = AVAudioSession();
      try {
        _saved = (await av.category, await av.categoryOptions, await av.mode);
      } catch (_) {
        _saved = null;
      }
    }
    await session.configure(_configuration);
    if (Platform.isAndroid) {
      await AndroidAudioManager().setMode(
        AndroidAudioHardwareMode.inCommunication,
      );
    }
    return systemManaged ? true : session.setActive(true);
  }

  /// After an interruption iOS leaves the session inactive.
  Future<bool> reactivate() async {
    if (systemManaged) return true;
    try {
      return await (await _instance).setActive(true);
    } catch (_) {
      return false;
    }
  }

  /// A wired or Bluetooth headset (or car, AirPlay…) is the output now.
  /// Platform types, not audio_session's cross-platform `AudioDeviceType`,
  /// which is still marked experimental.
  /// Whether the active route has an input at all. iOS lists a built-in
  /// microphone even when the device behind it is missing (a simulator on a
  /// Mac without one); the route is what the recorder will actually use.
  Future<bool> hasInput() async {
    try {
      if (Platform.isIOS) {
        return (await AVAudioSession().currentRoute).inputs.isNotEmpty;
      }
    } catch (_) {
      // Unknown: let the recorder try.
    }
    return true;
  }

  Future<bool> externalOutput() async {
    try {
      if (Platform.isIOS) {
        final route = await AVAudioSession().currentRoute;
        return route.outputs.any(
          (port) => externalIosOutputs.contains(port.portType),
        );
      }
      if (Platform.isAndroid) {
        final devices = await AndroidAudioManager().getDevices(
          AndroidGetAudioDevicesFlags.outputs,
        );
        return devices.any(
          (device) => externalAndroidOutputs.contains(device.type),
        );
      }
    } catch (_) {
      // Unknown: treat as the phone's own speaker.
    }
    return false;
  }

  Future<void> setSpeaker(bool on) async {
    if (Platform.isIOS) {
      await AVAudioSession().overrideOutputAudioPort(
        on
            ? AVAudioSessionPortOverride.speaker
            : AVAudioSessionPortOverride.none,
      );
    } else if (Platform.isAndroid) {
      await AndroidAudioManager().setSpeakerphoneOn(on);
    }
  }

  Future<void> close() async {
    if (systemManaged) return;
    final session = _session;
    if (session == null) return;
    try {
      await session.setActive(false);
    } catch (_) {
      // Something still holds I/O; the system deactivates it later.
    }
    try {
      if (Platform.isIOS) {
        final saved = _saved;
        if (saved != null) {
          await AVAudioSession().setCategory(saved.$1, saved.$2, saved.$3);
        }
      } else if (Platform.isAndroid) {
        final android = AndroidAudioManager();
        await android.setSpeakerphoneOn(false);
        await android.setMode(AndroidAudioHardwareMode.normal);
      }
    } catch (_) {
      // Best effort: the next call configures the session again anyway.
    }
  }
}
