import 'dart:typed_data';

import 'package:bossip_mobile/features/voice/audio/pcm_codec.dart';
import 'package:bossip_mobile/features/voice/audio/pcm_player.dart';
import 'package:bossip_mobile/features/voice/audio/tones.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

/// Records what the player hands the device; the test plays the device's
/// part by calling [report].
class _Output implements PcmOutput {
  final fed = <Int16List>[];
  int? sampleRate;
  int? threshold;
  void Function(int remaining)? _onFeed;
  bool released = false;

  void report(int remaining) => _onFeed!(remaining);

  @override
  Future<void> setup({required int sampleRate}) async =>
      this.sampleRate = sampleRate;

  @override
  Future<void> setFeedThreshold(int frames) async => threshold = frames;

  @override
  void setFeedCallback(void Function(int remainingFrames)? onFeed) =>
      _onFeed = onFeed;

  @override
  Future<void> feed(Int16List samples) async => fed.add(samples);

  @override
  Future<void> release() async => released = true;
}

Uint8List _pcm(int count, int sample) {
  final data = ByteData(count * 2);
  for (var i = 0; i < count; i++) {
    data.setInt16(i * 2, sample, Endian.little);
  }
  return data.buffer.asUint8List();
}

Int16List _samples(int count, int sample) =>
    Int16List.fromList(List.filled(count, sample));

ByteData _wav({
  required int rate,
  required int channels,
  required List<int> samples,
  bool listChunk = false,
}) {
  final list = listChunk ? 12 : 0;
  final data = ByteData(44 + list + samples.length * 2);
  void four(int at, String s) {
    for (var i = 0; i < 4; i++) {
      data.setUint8(at + i, s.codeUnitAt(i));
    }
  }

  four(0, 'RIFF');
  data.setUint32(4, data.lengthInBytes - 8, Endian.little);
  four(8, 'WAVE');
  four(12, 'fmt ');
  data
    ..setUint32(16, 16, Endian.little)
    ..setUint16(20, 1, Endian.little)
    ..setUint16(22, channels, Endian.little)
    ..setUint32(24, rate, Endian.little)
    ..setUint32(28, rate * channels * 2, Endian.little)
    ..setUint16(32, channels * 2, Endian.little)
    ..setUint16(34, 16, Endian.little);
  var at = 36;
  if (listChunk) {
    four(at, 'LIST');
    data.setUint32(at + 4, 4, Endian.little);
    four(at + 8, 'INFO');
    at += 12;
  }
  four(at, 'data');
  data.setUint32(at + 4, samples.length * 2, Endian.little);
  for (var i = 0; i < samples.length; i++) {
    data.setInt16(at + 8 + i * 2, samples[i], Endian.little);
  }
  return data;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('uplink packets', () {
    test('any block size becomes exact 100 ms packets', () {
      final chunker = PcmChunker();
      final packets = <Uint8List>[];
      // 682-byte blocks: what iOS hands over at 48 kHz → 16 kHz.
      final source = List<int>.generate(682 * 10, (i) => i % 251);
      for (var at = 0; at < source.length; at += 682) {
        chunker.add(source.sublist(at, at + 682), packets.add);
      }
      expect(packets, hasLength(2));
      expect(packets.every((p) => p.length == uplinkPacketBytes), isTrue);
      expect(packets[1].first, source[3200]);
      // A partial packet is dropped on reset, not glued to new audio.
      chunker
        ..reset()
        ..add(List.filled(3200, 7), packets.add);
      expect(packets.last.every((b) => b == 7), isTrue);
    });

    test('levels: silence is zero, loud speech saturates', () {
      expect(pcm16Level(Uint8List(3200)), 0);
      expect(pcm16Level(_pcm(1600, 30000)), 1);
      final quiet = pcm16Level(_pcm(1600, 600));
      expect(quiet, closeTo(600 / 32768 * 9, 1e-6));
      expect(int16Level(_samples(10, 600)), closeTo(quiet, 1e-6));
      expect(int16FromBytes(Uint8List.fromList([1, 0, 0xff, 0xff, 9])), [
        1,
        -1,
      ]);
    });
  });

  group('tones', () {
    test('only mono 16-bit PCM at the call rate decodes', () {
      expect(
        decodeWavPcm16(_wav(rate: 24000, channels: 1, samples: [1, -2, 3])),
        [1, -2, 3],
      );
      expect(
        decodeWavPcm16(
          _wav(rate: 24000, channels: 1, samples: [5], listChunk: true),
        ),
        [5],
      );
      expect(
        decodeWavPcm16(_wav(rate: 24000, channels: 2, samples: [1, 2])),
        isNull,
      );
      expect(
        decodeWavPcm16(_wav(rate: 16000, channels: 1, samples: [1])),
        isNull,
      );
      expect(decodeWavPcm16(ByteData(8)), isNull);
    });

    test('the bundled tones are the spec lengths at 24 kHz', () async {
      final tones = await CallTones.load(rootBundle);
      expect(tones.keys, CallTone.values);
      // A 1 s ring (the player adds the 4 s of silence), 180 ms, 200 ms.
      expect(tones[CallTone.ringback], hasLength(24000));
      expect(tones[CallTone.ended], hasLength(4320));
      expect(tones[CallTone.error], hasLength(4800));
      final peak = tones.values
          .expand((samples) => samples)
          .map((s) => s.abs())
          .reduce((a, b) => a > b ? a : b);
      expect(peak / 32768, closeTo(0.2, 0.01));
    });
  });

  group('player', () {
    test('keeps the device buffer short and feeds on demand', () async {
      final output = _Output();
      final player = PcmPlayer(output);
      await player.open();
      expect(output.sampleRate, 24000);
      expect(output.threshold, PcmPlayer.feedThreshold);

      player.enqueue(_pcm(6000, 1000));
      expect(output.fed.map((f) => f.length), [PcmPlayer.chunkFrames]);
      // More audio waits here until the device asks.
      player.enqueue(_pcm(100, 1000));
      expect(output.fed, hasLength(1));
      output.report(PcmPlayer.feedThreshold);
      output.report(1200);
      expect(output.fed.map((f) => f.length), [2400, 2400, 1300]);
      expect(player.level.value, greaterThan(0));
      output.report(500);
      expect(output.fed, hasLength(3));
      output.report(0);
      expect(player.level.value, 0);
    });

    test('playback.clear drops the server audio but not a tone', () async {
      final output = _Output();
      final player = PcmPlayer(output);
      await player.open();
      player.enqueue(_pcm(9000, 1000));
      expect(output.fed.single.every((s) => s == 1000), isTrue);
      player.tone(_samples(3000, 100));
      output.report(PcmPlayer.feedThreshold);
      // Call audio sets the block size; the tone is mixed into it.
      expect(output.fed[1], hasLength(2400));
      expect(output.fed[1].every((s) => s == 1100), isTrue);
      player.clear();
      output.report(1000);
      expect(output.fed[2], hasLength(600));
      expect(output.fed[2].every((s) => s == 100), isTrue);
      output.report(0);
      expect(output.fed, hasLength(3));
    });

    test(
      'the ringback repeats with its silence until the call is answered',
      () async {
        final output = _Output();
        final player = PcmPlayer(output);
        await player.open();
        // A 100 ms ring and 100 ms of silence, for a short test.
        player.startRinging(
          _samples(2400, 300),
          const Duration(milliseconds: 100),
        );
        expect(output.fed.single.every((s) => s == 300), isTrue);
        output.report(PcmPlayer.feedThreshold);
        expect(output.fed[1].every((s) => s == 0), isTrue);
        output.report(PcmPlayer.feedThreshold);
        expect(output.fed[2].every((s) => s == 300), isTrue); // rings again
        player.stopRinging();
        output.report(0);
        expect(output.fed, hasLength(3));
        // The greeting then plays alone.
        player.enqueue(_pcm(2400, 1000));
        expect(output.fed[3].every((s) => s == 1000), isTrue);
      },
    );

    test('a sample split across messages is joined', () async {
      final output = _Output();
      final player = PcmPlayer(output);
      await player.open();
      final bytes = _pcm(2, 30000);
      player.enqueue(Uint8List.sublistView(bytes, 0, 3));
      expect(output.fed.single, [30000]);
      output.report(0);
      player.enqueue(Uint8List.sublistView(bytes, 3));
      expect(output.fed.last, [30000]);
    });

    test('a tone over loud speech clips instead of wrapping', () async {
      final output = _Output();
      final player = PcmPlayer(output);
      await player.open();
      player
        ..enqueue(_pcm(10, 1000))
        ..tone(_samples(10, 32000))
        ..enqueue(_pcm(10, 30000));
      output.report(PcmPlayer.feedThreshold);
      expect(output.fed[1], List.filled(10, 32767));
    });

    test('drain waits for the device, close releases it', () async {
      final output = _Output();
      final player = PcmPlayer(output);
      await player.open();
      player.tone(_samples(100, 50));
      var drained = false;
      final waiting = player.drain().then((_) => drained = true);
      await Future<void>.delayed(Duration.zero);
      expect(drained, isFalse);
      output.report(0);
      await waiting;
      expect(drained, isTrue);
      await player.close();
      expect(output.released, isTrue);
      // A closed player ignores late audio.
      player.enqueue(_pcm(10, 1));
      expect(output.fed, hasLength(1));
    });
  });
}
