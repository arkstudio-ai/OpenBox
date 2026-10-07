import 'dart:async';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:flutter_pcm_sound/flutter_pcm_sound.dart';

import 'pcm_codec.dart';

/// The native PCM sink, so the queueing below can be tested without one.
abstract class PcmOutput {
  Future<void> setup({required int sampleRate});
  Future<void> setFeedThreshold(int frames);

  /// [onFeed] gets the frames still buffered natively, when that falls to the
  /// threshold and again when it reaches zero (once each per [feed]).
  void setFeedCallback(void Function(int remainingFrames)? onFeed);

  /// [samples] must own its whole buffer: the plugin sends the backing
  /// buffer, not a view of it.
  Future<void> feed(Int16List samples);
  Future<void> release();
}

/// `flutter_pcm_sound`: a RemoteIO unit on iOS, an AudioTrack on Android.
class PluginPcmOutput implements PcmOutput {
  const PluginPcmOutput();

  @override
  Future<void> setup({required int sampleRate}) async {
    // The default level prints every feed.
    await FlutterPcmSound.setLogLevel(LogLevel.error);
    await FlutterPcmSound.setup(
      sampleRate: sampleRate,
      channelCount: 1,
      // Sets the category without options; CallAudioSession configures the
      // full voice-chat session right after this.
      iosAudioCategory: IosAudioCategory.playAndRecord,
      // Without it a locked phone drops every frame fed to it.
      iosAllowBackgroundAudio: true,
    );
  }

  @override
  Future<void> setFeedThreshold(int frames) =>
      FlutterPcmSound.setFeedThreshold(frames);

  @override
  void setFeedCallback(void Function(int remainingFrames)? onFeed) =>
      FlutterPcmSound.setFeedCallback(
        onFeed == null ? null : (int remaining) => onFeed(remaining),
      );

  @override
  Future<void> feed(Int16List samples) =>
      FlutterPcmSound.feed(PcmArrayInt16(bytes: samples.buffer.asByteData()));

  @override
  Future<void> release() => FlutterPcmSound.release();
}

/// Plays the call: the server's 24 kHz audio, and the call tones on a layer
/// of their own that `playback.clear` never touches (spec §8). Both are
/// mixed here and fed to one native output, so tones follow the call's
/// route and never fight it for the audio session or focus.
///
/// The plugin has no "flush", so this keeps the native buffer short —
/// [feedThreshold] plus one [chunkFrames] block at most — and holds the rest
/// here, where [clear] can drop it at once. Barge-in therefore stops within
/// about 170 ms of `playback.clear`.
class PcmPlayer {
  PcmPlayer([this._output = const PluginPcmOutput()]);

  static const sampleRate = 24000;

  /// 100 ms per feed.
  static const chunkFrames = 2400;

  /// Ask for the next block once ~67 ms are left natively.
  static const feedThreshold = 1600;

  final PcmOutput _output;
  final _call = PcmQueue();
  final _tones = PcmQueue();

  /// Loudness of what is being fed, for the orb while the assistant talks.
  final level = ValueNotifier<double>(0);

  bool _open = false;
  bool _waiting = false;

  /// One ring and its silence, repeated on the tone layer until [stopRinging].
  Int16List? _ring;
  int? _oddByte;
  Timer? _stuck;
  final _drained = <Completer<void>>[];

  bool get _empty => _call.isEmpty && _tones.isEmpty;

  Future<void> open() async {
    await _output.setup(sampleRate: sampleRate);
    await _output.setFeedThreshold(feedThreshold);
    _output.setFeedCallback(_onFeed);
    _open = true;
    _pump();
  }

  /// Server audio, in arrival order. Frames may split a sample across two
  /// messages; the odd byte waits for its partner.
  void enqueue(Uint8List bytes) {
    if (bytes.isEmpty) return;
    var data = bytes;
    if (_oddByte != null) {
      data = Uint8List(bytes.length + 1)
        ..[0] = _oddByte!
        ..setRange(1, bytes.length + 1, bytes);
      _oddByte = null;
    }
    if (data.length.isOdd) _oddByte = data.last;
    _call.add(int16FromBytes(data));
    _pump();
  }

  /// A call tone; queued tones play one after another.
  void tone(Int16List samples) {
    _tones.add(samples);
    _pump();
  }

  /// Rings until [stopRinging]: [ring], then [gap] of silence, again.
  void startRinging(Int16List ring, Duration gap) {
    final cycle = Int16List(
      ring.length + sampleRate * gap.inMilliseconds ~/ 1000,
    )..setRange(0, ring.length, ring);
    _ring = cycle;
    _tones.add(cycle);
    _pump();
  }

  /// The call was answered (or given up): the ringing stops at once.
  void stopRinging() {
    if (_ring == null) return;
    _ring = null;
    _tones.clear();
    if (_call.isEmpty) level.value = 0;
  }

  /// `playback.clear`: drop the server audio not yet handed to the device.
  void clear() {
    _call.clear();
    _oddByte = null;
    if (_tones.isEmpty) level.value = 0;
  }

  /// Completes once everything queued has played, or after [timeout].
  Future<void> drain({Duration timeout = const Duration(milliseconds: 800)}) {
    if (!_open || (_empty && !_waiting)) return Future.value();
    final done = Completer<void>();
    _drained.add(done);
    return done.future.timeout(timeout, onTimeout: () {});
  }

  Future<void> close() async {
    _open = false;
    _ring = null;
    _stuck?.cancel();
    _call.clear();
    _tones.clear();
    _settleDrained();
    level.value = 0;
    _output.setFeedCallback(null);
    try {
      await _output.release();
    } catch (_) {
      // Nothing to release when setup never finished.
    }
  }

  void _pump() {
    final ring = _ring;
    if (ring != null && _tones.isEmpty) _tones.add(ring);
    if (!_open || _waiting || _empty) return;
    // Follow the call audio's own block sizes so a tone mixed into it never
    // opens a gap; tones alone go out in full blocks.
    final frames = math.min(
      chunkFrames,
      _call.isNotEmpty ? _call.length : _tones.length,
    );
    final mix = Int32List(frames);
    _call.addTo(mix, frames);
    _tones.addTo(mix, frames);
    final out = Int16List(frames);
    for (var i = 0; i < frames; i++) {
      out[i] = mix[i].clamp(-32768, 32767);
    }
    level.value = int16Level(out);
    _waiting = true;
    // A callback the plugin never sends must not stall the call.
    _stuck?.cancel();
    _stuck = Timer(const Duration(seconds: 1), () => _onFeed(-1));
    unawaited(_output.feed(out).catchError((Object _) => _onFeed(-1)));
  }

  void _onFeed(int remaining) {
    _stuck?.cancel();
    _waiting = false;
    if (remaining == 0 && _empty) {
      level.value = 0;
      _settleDrained();
    }
    _pump();
  }

  void _settleDrained() {
    for (final done in _drained) {
      if (!done.isCompleted) done.complete();
    }
    _drained.clear();
  }
}
