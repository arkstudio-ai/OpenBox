import 'dart:async';
import 'dart:typed_data';

import 'package:audio_session/audio_session.dart';
import 'package:flutter/foundation.dart';

import '../state/voice_call_state.dart';
import 'call_audio_session.dart';
import 'debug_capture.dart';
import 'pcm_capture.dart';
import 'pcm_player.dart';
import 'tones.dart';

/// Something the call's audio reports by itself.
sealed class CallAudioEvent {
  const CallAudioEvent();
}

/// A phone call, Siri or another app took the audio ([begin]) or gave it
/// back.
final class CallAudioInterruption extends CallAudioEvent {
  const CallAudioInterruption({required this.begin});

  final bool begin;
}

/// A headset (or car, AirPlay…) came or went; [external] is the result.
final class CallAudioRouteChange extends CallAudioEvent {
  const CallAudioRouteChange({required this.external});

  final bool external;
}

/// The microphone stopped and could not be brought back.
final class CallAudioMicLost extends CallAudioEvent {
  const CallAudioMicLost();
}

/// Why the call's audio could not start.
class CallAudioFailure implements Exception {
  const CallAudioFailure(this.reason);

  final VoiceEndReason reason;
}

/// The call's audio as the controller drives it: one session, one
/// microphone, one player. Swapped for a fake in tests.
abstract class CallAudio {
  Stream<CallAudioEvent> get events;

  /// Loudness of what the assistant is saying, 0–1.
  ValueListenable<double> get outputLevel;

  /// Session, player, then microphone. [onPacket] gets 100 ms uplink
  /// packets from then on. Throws [CallAudioFailure].
  Future<void> open({required void Function(Uint8List packet) onPacket});

  void playTone(CallTone tone);

  /// The ringback, repeated until [stopRinging] (the call is answered).
  void startRinging();

  void stopRinging();

  /// 24 kHz PCM16 from the server, played in arrival order.
  void play(Uint8List pcm);

  /// `playback.clear`. Tones keep playing.
  void clearPlayback();

  /// An interruption: stop the microphone and playback, keep everything
  /// else ready.
  Future<void> pause();

  /// False when the system still holds the audio.
  Future<bool> resume();

  /// A headset or other external output is in use.
  Future<bool> externalOutput();

  Future<void> setSpeaker(bool on);

  /// Stops the microphone at once, plays [tone] to its end, then releases
  /// the player and the session. Safe to call twice.
  Future<void> close({CallTone? tone});
}

/// The real thing: [CallAudioSession] + [PcmCapture] + [PcmPlayer].
class DeviceCallAudio implements CallAudio {
  DeviceCallAudio({
    CallAudioSession? session,
    PcmCapture? capture,
    PcmPlayer? player,
  }) : _session =
           session ??
           CallAudioSession(
             systemManaged:
                 !kIsWeb && defaultTargetPlatform == TargetPlatform.iOS,
           ),
       _capture = capture ?? (voiceFakeMic ? FilePcmCapture() : PcmCapture()),
       _player = player ?? PcmPlayer();

  /// A microphone silent this long has stalled; restart it.
  static const _stall = Duration(milliseconds: 2500);
  static const _maxRestarts = 5;

  final CallAudioSession _session;
  final PcmCapture _capture;
  final PcmPlayer _player;
  final _events = StreamController<CallAudioEvent>.broadcast();
  StreamSubscription<AudioInterruptionEvent>? _interruptions;
  StreamSubscription<AudioDevicesChangedEvent>? _devices;
  Map<CallTone, Int16List> _tones = const {};
  void Function(Uint8List packet)? _onPacket;
  Timer? _watchdog;
  bool _paused = false;
  bool _restarting = false;
  bool _closed = false;
  int _restarts = 0;

  @override
  Stream<CallAudioEvent> get events => _events.stream;

  @override
  ValueListenable<double> get outputLevel => _player.level;

  @override
  Future<void> open({required void Function(Uint8List packet) onPacket}) async {
    _onPacket = onPacket;
    try {
      _tones = await CallTones.load();
    } catch (_) {
      _tones = const {}; // A call without beeps is still a call.
    }
    // The player first: its setup sets a bare category and activates it,
    // which the voice-chat configuration below then replaces.
    try {
      await _player.open();
    } catch (_) {
      throw const CallAudioFailure(VoiceEndReason.error);
    }
    bool active;
    try {
      active = await _session.open();
    } catch (_) {
      active = false;
    }
    if (!active) throw const CallAudioFailure(VoiceEndReason.micBusy);
    _interruptions = _session.interruptions.listen((event) {
      // Android "duck" (with pause-when-ducked set it arrives as pause).
      if (event.type == AudioInterruptionType.duck) return;
      _events.add(CallAudioInterruption(begin: event.begin));
    });
    _devices = _session.deviceChanges.listen((_) async {
      final external = await _session.externalOutput();
      if (!_closed) _events.add(CallAudioRouteChange(external: external));
    });
    // With no input device at all (a simulator on a Mac without a
    // microphone) the recorder's native tap throws an exception Dart never
    // sees and the app dies; ask first and end the call instead.
    if (!await _capture.hasMicrophone() || !await _session.hasInput()) {
      throw const CallAudioFailure(VoiceEndReason.micMissing);
    }
    try {
      await _capture.start(onPacket: onPacket, onLost: _micLost);
    } catch (_) {
      throw CallAudioFailure(
        await _capture.hasMicrophone()
            ? VoiceEndReason.micBusy
            : VoiceEndReason.micMissing,
      );
    }
    _watchdog = Timer.periodic(const Duration(seconds: 1), (_) => _check());
  }

  /// The stream failed or ended: let the watchdog try to bring it back.
  void _micLost() => _capture.lastAudioAt = DateTime(2000);

  Future<void> _check() async {
    if (_closed || _paused || _restarting) return;
    if (DateTime.now().difference(_capture.lastAudioAt) < _stall) return;
    _restarting = true;
    try {
      if (++_restarts > _maxRestarts) {
        throw StateError('The microphone keeps stopping.');
      }
      await _capture.stop();
      await _session.reactivate();
      await _capture.start(onPacket: _onPacket!, onLost: _micLost);
    } catch (_) {
      if (!_closed) _events.add(const CallAudioMicLost());
    } finally {
      _restarting = false;
    }
  }

  @override
  void playTone(CallTone tone) {
    final samples = _tones[tone];
    if (samples != null && !_closed) _player.tone(samples);
  }

  @override
  void startRinging() {
    final ring = _tones[CallTone.ringback];
    if (ring != null && !_closed) _player.startRinging(ring, ringGap);
  }

  @override
  void stopRinging() => _player.stopRinging();

  @override
  void play(Uint8List pcm) {
    if (!_closed && !_paused) _player.enqueue(pcm);
  }

  @override
  void clearPlayback() => _player.clear();

  @override
  Future<void> pause() async {
    _paused = true;
    _player.clear();
    try {
      await _capture.pause();
    } catch (_) {
      // The system already stopped it.
    }
  }

  @override
  Future<bool> resume() async {
    if (_closed) return false;
    if (!await _session.reactivate()) return false;
    try {
      await _capture.resume();
    } catch (_) {
      try {
        await _capture.stop();
        await _capture.start(onPacket: _onPacket!, onLost: _micLost);
      } catch (_) {
        return false;
      }
    }
    _paused = false;
    return true;
  }

  @override
  Future<bool> externalOutput() => _session.externalOutput();

  @override
  Future<void> setSpeaker(bool on) => _session.setSpeaker(on);

  @override
  Future<void> close({CallTone? tone}) async {
    if (_closed) return;
    _closed = true;
    _watchdog?.cancel();
    await _interruptions?.cancel();
    await _devices?.cancel();
    await _capture.dispose();
    _player.stopRinging();
    _player.clear();
    final samples = tone == null ? null : _tones[tone];
    if (samples != null) {
      _player.tone(samples);
      await _player.drain();
    }
    await _player.close();
    await _session.close();
    await _events.close();
  }
}
