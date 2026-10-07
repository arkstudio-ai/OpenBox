import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:path_provider/path_provider.dart';

import 'pcm_capture.dart';

/// QA only: built with `--dart-define=VOICE_FAKE_MIC=true`, calls take their
/// "microphone" from `voice-fake-mic.pcm` (16 kHz mono PCM16) in the app's
/// Documents folder — played once in real time, then silence. A simulator on
/// a Mac without a microphone can then place a whole call. Release builds
/// never define it, so [FilePcmCapture] is never constructed there.
const voiceFakeMic = bool.fromEnvironment('VOICE_FAKE_MIC');

class FilePcmCapture extends PcmCapture {
  FilePcmCapture();

  static const _packet = 3200; // 100 ms
  Timer? _timer;
  Uint8List _pcm = Uint8List(0);
  int _offset = 0;
  bool _held = false;

  @override
  Future<void> start({
    required void Function(Uint8List packet) onPacket,
    required void Function() onLost,
  }) async {
    if (_pcm.isEmpty) {
      final dir = await getApplicationDocumentsDirectory();
      final file = File('${dir.path}/voice-fake-mic.pcm');
      if (file.existsSync()) _pcm = await file.readAsBytes();
    }
    _held = false;
    lastAudioAt = DateTime.now();
    _timer?.cancel();
    _timer = Timer.periodic(const Duration(milliseconds: 100), (_) {
      lastAudioAt = DateTime.now();
      if (_held) return;
      final packet = Uint8List(_packet);
      if (_offset < _pcm.length) {
        final end = (_offset + _packet).clamp(0, _pcm.length);
        packet.setRange(0, end - _offset, _pcm, _offset);
        _offset = end;
      }
      onPacket(packet);
    });
  }

  @override
  Future<bool> hasMicrophone() async => true;

  @override
  Future<void> pause() async => _held = true;

  @override
  Future<void> resume() async {
    _held = false;
    lastAudioAt = DateTime.now();
  }

  @override
  Future<void> stop() async {
    _timer?.cancel();
    _timer = null;
  }

  @override
  Future<void> dispose() => stop();
}
