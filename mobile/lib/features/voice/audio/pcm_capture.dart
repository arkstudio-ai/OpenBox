import 'dart:async';
import 'dart:typed_data';

import 'package:record/record.dart';

import 'pcm_codec.dart';

/// The microphone as exact 100 ms packets of 16 kHz mono PCM16, through
/// `record`'s stream mode (AVAudioEngine on iOS, AudioRecord on Android).
class PcmCapture {
  PcmCapture([AudioRecorder Function()? recorder])
    : _newRecorder = recorder ?? AudioRecorder.new;

  static const sampleRate = 16000;

  /// Voice processing on: echo cancellation (iOS voice-processing I/O,
  /// Android AcousticEchoCanceler on the voice-communication source), noise
  /// suppression and gain control.
  static const config = RecordConfig(
    encoder: AudioEncoder.pcm16bits,
    sampleRate: sampleRate,
    numChannels: 1,
    echoCancel: true,
    noiseSuppress: true,
    autoGain: true,
    // CallAudioSession owns interruptions and audio focus. Left to itself,
    // record would pause on its own and, on Android, request focus with a
    // second listener — which takes focus from the call's own request.
    audioInterruption: AudioInterruptionMode.none,
    iosConfig: IosRecordConfig(
      // Unused: the session is not record's to manage (see [start]).
      categoryOptions: [],
      // The connected / ended haptics must still be felt mid-recording.
      allowHapticsAndSystemSoundsDuringRecording: true,
    ),
    androidConfig: AndroidRecordConfig(
      audioSource: AndroidAudioSource.voiceCommunication,
    ),
  );

  /// [config] minus voice processing, for devices that refuse it.
  static final withoutProcessing = config.copyWith(
    echoCancel: false,
    noiseSuppress: false,
    autoGain: false,
  );

  final AudioRecorder Function() _newRecorder;
  final _chunker = PcmChunker();
  AudioRecorder? _recorder;
  StreamSubscription<Uint8List>? _subscription;
  bool _paused = false;
  bool _stopping = false;

  /// When the microphone last delivered anything, to spot a stalled engine
  /// (iOS stops AVAudioEngine on some route changes without saying so).
  DateTime lastAudioAt = DateTime.now();

  bool get running => _subscription != null && !_paused;

  /// Starts the microphone. [onPacket] gets 3,200-byte packets; [onLost]
  /// fires if the stream fails or ends without [stop].
  Future<void> start({
    required void Function(Uint8List packet) onPacket,
    required void Function() onLost,
  }) async {
    final recorder = _recorder ??= _newRecorder();
    // The voice-chat session is already configured and active; record must
    // not reset its category or options.
    await recorder.ios?.manageAudioSession(false);
    Stream<Uint8List> stream;
    try {
      stream = await recorder.startStream(config);
    } catch (_) {
      // Voice processing is not available everywhere (the iOS Simulator,
      // some USB microphones). A call without echo cancellation still beats
      // no call; a microphone that cannot start at all throws again here.
      stream = await recorder.startStream(withoutProcessing);
    }
    _chunker.reset();
    _paused = false;
    _stopping = false;
    lastAudioAt = DateTime.now();
    _subscription = stream.listen(
      (bytes) {
        lastAudioAt = DateTime.now();
        if (!_paused) _chunker.add(bytes, onPacket);
      },
      onError: (Object _) {
        if (!_stopping) onLost();
      },
      onDone: () {
        if (!_stopping) onLost();
      },
    );
  }

  /// False only when the device reports no microphone at all.
  Future<bool> hasMicrophone() async {
    try {
      final recorder = _recorder ??= _newRecorder();
      return (await recorder.listInputDevices()).isNotEmpty;
    } catch (_) {
      return true;
    }
  }

  Future<void> pause() async {
    _paused = true;
    _chunker.reset();
    await _recorder?.pause();
  }

  Future<void> resume() async {
    await _recorder?.resume();
    _paused = false;
    lastAudioAt = DateTime.now();
  }

  Future<void> stop() async {
    _stopping = true;
    try {
      await _recorder?.stop();
    } catch (_) {
      // Already stopped by the system.
    }
    await _subscription?.cancel();
    _subscription = null;
  }

  Future<void> dispose() async {
    await stop();
    final recorder = _recorder;
    _recorder = null;
    try {
      await recorder?.dispose();
    } catch (_) {
      // Nothing left to release.
    }
  }
}
