import 'dart:collection';
import 'dart:math' as math;
import 'dart:typed_data';

/// Pure PCM16 helpers shared by capture and playback. Everything on the wire
/// is signed 16-bit little endian, mono.

/// 100 ms of 16 kHz mono PCM16: the only frame size the server takes.
const int uplinkPacketBytes = 3200;

/// Re-cuts a byte stream into packets of exactly [packetBytes]. Microphones
/// hand over whatever block size the device likes.
class PcmChunker {
  PcmChunker([this.packetBytes = uplinkPacketBytes])
    : _buffer = Uint8List(packetBytes);

  final int packetBytes;
  final Uint8List _buffer;
  int _filled = 0;

  /// Calls [emit] once per complete packet; each packet is a fresh list.
  void add(List<int> bytes, void Function(Uint8List packet) emit) {
    var offset = 0;
    while (offset < bytes.length) {
      final take = math.min(packetBytes - _filled, bytes.length - offset);
      _buffer.setRange(_filled, _filled + take, bytes, offset);
      _filled += take;
      offset += take;
      if (_filled == packetBytes) {
        emit(Uint8List.fromList(_buffer));
        _filled = 0;
      }
    }
  }

  /// Drops a partial packet (after a pause, its audio is stale).
  void reset() => _filled = 0;
}

/// Loudness for the orb, 0–1: RMS of the samples times nine, the same
/// scaling as the web demo's `data.level * 9`.
double pcm16Level(Uint8List bytes) {
  final count = bytes.length ~/ 2;
  if (count == 0) return 0;
  final data = ByteData.sublistView(bytes, 0, count * 2);
  var sum = 0.0;
  for (var i = 0; i < count; i++) {
    final sample = data.getInt16(i * 2, Endian.little) / 32768;
    sum += sample * sample;
  }
  return math.min(1, math.sqrt(sum / count) * 9);
}

/// [pcm16Level] for samples already decoded.
double int16Level(Int16List samples) {
  if (samples.isEmpty) return 0;
  var sum = 0.0;
  for (final value in samples) {
    final sample = value / 32768;
    sum += sample * sample;
  }
  return math.min(1, math.sqrt(sum / samples.length) * 9);
}

/// Little-endian bytes to samples. A trailing odd byte is ignored; callers
/// that stream keep it for the next block.
Int16List int16FromBytes(Uint8List bytes) {
  final count = bytes.length ~/ 2;
  final out = Int16List(count);
  final data = ByteData.sublistView(bytes, 0, count * 2);
  for (var i = 0; i < count; i++) {
    out[i] = data.getInt16(i * 2, Endian.little);
  }
  return out;
}

String _fourCc(ByteData data, int offset) => String.fromCharCodes([
  for (var i = 0; i < 4; i++) data.getUint8(offset + i),
]);

/// The samples of a PCM16 mono WAV file at [sampleRate]; null for anything
/// else. Walks the chunks, so a `LIST` chunk before `data` is fine.
Int16List? decodeWavPcm16(ByteData data, {int sampleRate = 24000}) {
  if (data.lengthInBytes < 12 ||
      _fourCc(data, 0) != 'RIFF' ||
      _fourCc(data, 8) != 'WAVE') {
    return null;
  }
  int? format;
  int? channels;
  int? rate;
  int? bits;
  var offset = 12;
  while (offset + 8 <= data.lengthInBytes) {
    final id = _fourCc(data, offset);
    final size = data.getUint32(offset + 4, Endian.little);
    final body = offset + 8;
    if (id == 'fmt ' && body + 16 <= data.lengthInBytes) {
      format = data.getUint16(body, Endian.little);
      channels = data.getUint16(body + 2, Endian.little);
      rate = data.getUint32(body + 4, Endian.little);
      bits = data.getUint16(body + 14, Endian.little);
    } else if (id == 'data') {
      if (format != 1 || channels != 1 || bits != 16 || rate != sampleRate) {
        return null;
      }
      final end = math.min(body + size, data.lengthInBytes);
      final count = (end - body) ~/ 2;
      final out = Int16List(count);
      for (var i = 0; i < count; i++) {
        out[i] = data.getInt16(body + i * 2, Endian.little);
      }
      return out;
    }
    offset = body + size + (size.isOdd ? 1 : 0);
  }
  return null;
}

/// A FIFO of samples that can be drained into a mix in any block size.
class PcmQueue {
  final _chunks = ListQueue<Int16List>();
  int _head = 0;
  int _length = 0;

  /// Samples waiting.
  int get length => _length;
  bool get isEmpty => _length == 0;
  bool get isNotEmpty => _length != 0;

  void add(Int16List samples) {
    if (samples.isEmpty) return;
    _chunks.add(samples);
    _length += samples.length;
  }

  void clear() {
    _chunks.clear();
    _head = 0;
    _length = 0;
  }

  /// Takes up to [count] samples off the front and adds them into [mix]
  /// from index 0. Returns how many it took.
  int addTo(Int32List mix, int count) {
    var written = 0;
    while (written < count && _chunks.isNotEmpty) {
      final chunk = _chunks.first;
      final take = math.min(count - written, chunk.length - _head);
      for (var i = 0; i < take; i++) {
        mix[written + i] += chunk[_head + i];
      }
      written += take;
      _head += take;
      if (_head == chunk.length) {
        _chunks.removeFirst();
        _head = 0;
      }
    }
    _length -= written;
    return written;
  }
}
