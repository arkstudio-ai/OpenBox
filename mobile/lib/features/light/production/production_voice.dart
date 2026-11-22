import 'dart:async';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';
import 'package:record/record.dart';
import 'package:video_player/video_player.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import 'production_api.dart';
import 'production_stages.dart';
import 'production_widgets.dart';

String voiceText(I18nState i18n, String key, {Map<String, Object?>? vars}) =>
    i18n.t('light:production.voice.$key', vars: vars);

String voiceLabel(I18nState i18n, String id, {String? name}) =>
    name ??
    voiceText(
      i18n,
      id.startsWith('system:')
          ? id.split(':').last
          : id.startsWith('voice:')
          ? 'mine'
          : id == 'none'
          ? 'none'
          : 'choose',
    );

String productionVoiceError(I18nState i18n, Object error) =>
    productionErrorText(i18n, error);

final productionVoicesProvider =
    FutureProvider.autoDispose<Map<String, dynamic>>(
      (ref) => ref.watch(lightProductionApiProvider).voices(),
    );

String? savedVoiceName(Map<String, dynamic>? catalogue, String id) {
  for (final item in asList(catalogue?['mine'])) {
    final row = asMap(item);
    if (row['id'] == id) return asString(row['name']);
  }
  return null;
}

Future<String?> chooseProductionVoice(
  BuildContext context,
  String current, {
  required bool allowNone,
}) => Navigator.of(context).push<String>(
  MaterialPageRoute(
    builder: (_) => _VoicePicker(current: current, allowNone: allowNone),
  ),
);

class _VoicePicker extends ConsumerStatefulWidget {
  const _VoicePicker({required this.current, required this.allowNone});
  final String current;
  final bool allowNone;

  @override
  ConsumerState<_VoicePicker> createState() => _VoicePickerState();
}

class _VoicePickerState extends ConsumerState<_VoicePicker> {
  late String _selected = widget.current;
  bool _ready = false;

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final list = ref.watch(productionVoicesProvider);
    final api = ref.read(lightProductionApiProvider);
    final systems = asList(list.valueOrNull?['system']).map(asMap).toList();
    final mine = asList(list.valueOrNull?['mine']).map(asMap);
    _ready =
        systems.any((row) => row['id'] == _selected) ||
        mine.any((row) => row['id'] == _selected && row['status'] == 'ready') ||
        (widget.allowNone && _selected == 'none');
    return Scaffold(
      appBar: AppBar(title: Text(voiceText(i18n, 'settings'))),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 20),
        children: [
          ProductionHeading(
            voiceText(i18n, 'choose'),
            description: voiceText(i18n, 'chooseHint'),
          ),
          const SizedBox(height: 18),
          if (list.isLoading && !list.hasValue)
            const Center(child: CircularProgressIndicator()),
          if (list.hasError)
            TextButton.icon(
              onPressed: () => ref.invalidate(productionVoicesProvider),
              icon: const Icon(Icons.refresh),
              label: Text(voiceText(i18n, 'retry')),
            ),
          for (final row in systems)
            _choice(
              i18n,
              row['id'] as String,
              voiceText(i18n, row['style'] as String),
              voiceText(i18n, '${row['style']}Hint'),
              api,
            ),
          if (widget.allowNone)
            _choice(i18n, 'none', voiceText(i18n, 'none'), null, api),
          const SizedBox(height: 8),
          VoiceCard(
            child: ListTile(
              key: const Key('production-my-voices'),
              leading: const Icon(Icons.mic_none_rounded),
              title: Text(voiceText(i18n, 'mine')),
              subtitle: savedVoiceName(list.valueOrNull, _selected) == null
                  ? null
                  : Text(savedVoiceName(list.valueOrNull, _selected)!),
              trailing: const Icon(Icons.chevron_right),
              onTap: () async {
                final voice = await Navigator.of(context).push<String>(
                  MaterialPageRoute(
                    builder: (_) => _MyVoices(current: _selected),
                  ),
                );
                if (mounted && voice != null) setState(() => _selected = voice);
              },
            ),
          ),
        ],
      ),
      bottomNavigationBar: VoiceFooter(
        label: voiceText(i18n, 'use'),
        onPressed: _ready ? () => Navigator.pop(context, _selected) : null,
      ),
    );
  }

  Widget _choice(
    I18nState i18n,
    String id,
    String name,
    String? detail,
    LightProductionApi api,
  ) => Padding(
    padding: const EdgeInsets.only(bottom: 8),
    child: VoiceCard(
      selected: _selected == id,
      child: ListTile(
        key: ValueKey('production-voice-$id'),
        title: Text(name),
        subtitle: detail == null ? null : Text(detail),
        leading: Icon(
          _selected == id ? Icons.check_circle : Icons.radio_button_unchecked,
        ),
        trailing: id == 'none'
            ? null
            : VoicePlayButton(load: () => api.auditionVoice(id)),
        onTap: () => setState(() => _selected = id),
      ),
    ),
  );
}

class _MyVoices extends ConsumerStatefulWidget {
  const _MyVoices({required this.current});
  final String current;

  @override
  ConsumerState<_MyVoices> createState() => _MyVoicesState();
}

class _MyVoicesState extends ConsumerState<_MyVoices> {
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    _poll = Timer.periodic(const Duration(seconds: 4), (_) {
      if (!mounted) return;
      final state = ref.read(productionVoicesProvider);
      if (!state.isLoading &&
          asList(
            state.valueOrNull?['mine'],
          ).any((row) => asMap(row)['status'] == 'processing')) {
        ref.invalidate(productionVoicesProvider);
      }
    });
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final list = ref.watch(productionVoicesProvider);
    final api = ref.read(lightProductionApiProvider);
    final rows = asList(list.valueOrNull?['mine']).map(asMap);
    return Scaffold(
      appBar: AppBar(title: Text(voiceText(i18n, 'mine'))),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (list.isLoading && !list.hasValue)
            const Center(child: CircularProgressIndicator()),
          if (list.hasValue && rows.isEmpty)
            ProductionHeading(
              voiceText(i18n, 'mine'),
              description: voiceText(i18n, 'emptyMine'),
            ),
          if (list.hasError)
            TextButton(
              onPressed: () => ref.invalidate(productionVoicesProvider),
              child: Text(voiceText(i18n, 'retry')),
            ),
          for (final row in rows)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: VoiceCard(
                selected: row['id'] == widget.current,
                child: ListTile(
                  key: ValueKey('production-clone-${row['name']}'),
                  leading: const Icon(Icons.graphic_eq),
                  title: Text(row['name'] as String),
                  subtitle: Text(voiceText(i18n, row['status'] as String)),
                  trailing: row['status'] != 'ready'
                      ? null
                      : VoicePlayButton(
                          load: () => api.auditionVoice(row['id'] as String),
                        ),
                  onTap: row['status'] == 'ready'
                      ? () => Navigator.pop(context, row['id'] as String)
                      : null,
                ),
              ),
            ),
        ],
      ),
      bottomNavigationBar: VoiceFooter(
        label: voiceText(i18n, 'add'),
        onPressed: () async {
          await Navigator.of(
            context,
          ).push<void>(MaterialPageRoute(builder: (_) => const _AddVoice()));
          if (mounted) ref.invalidate(productionVoicesProvider);
        },
      ),
    );
  }
}

class _AddVoice extends ConsumerWidget {
  const _AddVoice();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    return Scaffold(
      appBar: AppBar(title: Text(voiceText(i18n, 'add'))),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          ProductionHeading(
            voiceText(i18n, 'add'),
            description: voiceText(i18n, 'sampleHint'),
          ),
          const SizedBox(height: 20),
          VoiceCard(
            selected: true,
            child: ListTile(
              key: const Key('production-record-voice'),
              leading: const Icon(Icons.mic_none),
              title: Text(voiceText(i18n, 'record')),
              subtitle: Text(voiceText(i18n, 'recordHint')),
              trailing: const Icon(Icons.chevron_right),
              onTap: () async {
                final file = await Navigator.of(context).push<String>(
                  MaterialPageRoute(builder: (_) => const _RecordVoice()),
                );
                if (file == null) return;
                try {
                  if (context.mounted) await _name(context, file);
                } finally {
                  await File(file).delete().catchError((_) => File(file));
                }
              },
            ),
          ),
          const SizedBox(height: 10),
          VoiceCard(
            child: ListTile(
              key: const Key('production-upload-voice'),
              leading: const Icon(Icons.audio_file_outlined),
              title: Text(voiceText(i18n, 'upload')),
              subtitle: Text(voiceText(i18n, 'uploadHint')),
              trailing: const Icon(Icons.chevron_right),
              onTap: () async {
                try {
                  final result = await FilePickerPlatform.instance.pickFiles(
                    type: FileType.custom,
                    allowedExtensions: const ['wav', 'mp3', 'm4a'],
                  );
                  final file = result.firstOrNull?.path;
                  if (file != null && context.mounted) {
                    await _name(context, file);
                  }
                } catch (_) {
                  if (context.mounted) {
                    ScaffoldMessenger.of(context).showSnackBar(
                      SnackBar(content: Text(voiceText(i18n, 'sampleError'))),
                    );
                  }
                }
              },
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _name(BuildContext context, String file) async {
    final created = await Navigator.of(
      context,
    ).push<bool>(MaterialPageRoute(builder: (_) => _SaveVoice(file: file)));
    if (created == true && context.mounted) Navigator.pop(context);
  }
}

class _RecordVoice extends ConsumerStatefulWidget {
  const _RecordVoice();
  @override
  ConsumerState<_RecordVoice> createState() => _RecordVoiceState();
}

class _RecordVoiceState extends ConsumerState<_RecordVoice> {
  final _recorder = AudioRecorder();
  final _elapsed = Stopwatch();
  Timer? _timer;
  StreamSubscription<Amplitude>? _amplitudes;
  final _levels = <double>[];
  bool _recording = false, _busy = false;
  String? _error, _path;

  @override
  void dispose() {
    _timer?.cancel();
    unawaited(_amplitudes?.cancel());
    unawaited(
      _recorder.dispose().whenComplete(() async {
        final abandoned = _path;
        if (abandoned != null) {
          await File(abandoned).delete().catchError((_) => File(abandoned));
        }
      }),
    );
    super.dispose();
  }

  Future<void> _toggle() async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      if (_recording) {
        _timer?.cancel();
        final file = await _recorder.stop();
        if (mounted && file != null && _elapsed.elapsed.inSeconds >= 5) {
          _path = null;
          Navigator.pop(context, file);
          return;
        }
        if (mounted) {
          setState(() {
            _recording = false;
            _error = 'sampleShort';
            _elapsed.stop();
          });
        }
        return;
      }
      if (!await _recorder.hasPermission()) {
        if (mounted) setState(() => _error = 'microphone');
        return;
      }
      final directory = await getTemporaryDirectory();
      final path = '${directory.path}/voice-${productionRequestId()}.wav';
      final old = _path;
      if (old != null) await File(old).delete().catchError((_) => File(old));
      _path = path;
      await _recorder
          .start(
            const RecordConfig(
              encoder: AudioEncoder.wav,
              sampleRate: 24000,
              numChannels: 1,
            ),
            path: path,
          )
          .timeout(const Duration(seconds: 12));
      if (!mounted) return;
      await _amplitudes?.cancel();
      _levels.clear();
      _amplitudes = _recorder
          .onAmplitudeChanged(const Duration(milliseconds: 160))
          .listen((value) {
            if (mounted) {
              setState(() {
                _levels.add(((value.current + 55) / 55).clamp(0, 1).toDouble());
                if (_levels.length > 28) _levels.removeAt(0);
              });
            }
          });
      _elapsed
        ..reset()
        ..start();
      _timer = Timer.periodic(const Duration(seconds: 1), (_) {
        if (!mounted) return;
        if (_elapsed.elapsed.inSeconds >= 59) {
          unawaited(_toggle());
        } else {
          setState(() {});
        }
      });
      setState(() => _recording = true);
    } catch (_) {
      if (mounted) setState(() => _error = 'sampleError');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    return Scaffold(
      appBar: AppBar(title: Text(voiceText(i18n, 'record'))),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          ProductionHeading(
            voiceText(i18n, 'record'),
            description: voiceText(i18n, 'sampleHint'),
          ),
          const SizedBox(height: 34),
          if (_recording)
            Center(
              child: Text(
                voiceText(i18n, 'recording'),
                key: const Key('production-recording'),
              ),
            ),
          Center(
            child: Text(
              voiceTime(_elapsed.elapsed.inSeconds),
              style: TextStyle(
                color: t.ink,
                fontSize: 48,
                fontWeight: FontWeight.w800,
              ),
            ),
          ),
          SizedBox(
            height: 64,
            child: Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                for (var i = 0; i < 28; i++)
                  Container(
                    width: 3,
                    height: 4 + (i < _levels.length ? _levels[i] * 40 : 0),
                    margin: const EdgeInsets.symmetric(horizontal: 2),
                    decoration: BoxDecoration(
                      color: t.n700,
                      borderRadius: BorderRadius.circular(2),
                    ),
                  ),
              ],
            ),
          ),
          const SizedBox(height: 16),
          VoiceCard(
            child: ListTile(
              title: Text(voiceText(i18n, 'readNaturally')),
              subtitle: Text(voiceText(i18n, 'readSample')),
            ),
          ),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.all(12),
              child: Text(
                voiceText(i18n, _error!),
                style: TextStyle(color: t.dangerInk),
              ),
            ),
        ],
      ),
      bottomNavigationBar: VoiceFooter(
        label: voiceText(
          i18n,
          _recording ? 'finishRecording' : 'beginRecording',
        ),
        onPressed: _busy ? null : _toggle,
      ),
    );
  }
}

class _SaveVoice extends ConsumerStatefulWidget {
  const _SaveVoice({required this.file});
  final String file;

  @override
  ConsumerState<_SaveVoice> createState() => _SaveVoiceState();
}

class _SaveVoiceState extends ConsumerState<_SaveVoice> {
  final _name = TextEditingController();
  bool _consented = false, _busy = false;
  String? _error;
  String? _request;
  Duration? _duration;

  bool get _validSample =>
      _duration != null &&
      _duration! >= const Duration(seconds: 5) &&
      _duration! <= const Duration(seconds: 60);

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (_busy || !_consented || !_validSample || _name.text.trim().isEmpty) {
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref
          .read(lightProductionApiProvider)
          .addVoice(
            _name.text.trim(),
            widget.file,
            _request ??= productionRequestId(),
          );
      if (!mounted) return;
      ref.invalidate(productionVoicesProvider);
      Navigator.pop(context, true);
    } catch (error) {
      if (mounted) {
        setState(
          () => _error = productionVoiceError(ref.read(i18nProvider), error),
        );
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    return Scaffold(
      appBar: AppBar(title: Text(voiceText(i18n, 'sample'))),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          ProductionHeading(
            voiceText(i18n, 'sample'),
            description: voiceText(i18n, 'sampleHint'),
          ),
          const SizedBox(height: 16),
          VoiceCard(
            child: ListTile(
              leading: const Icon(Icons.graphic_eq),
              title: Text(
                File(widget.file).uri.pathSegments.last,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
              subtitle: Text(
                _duration == null
                    ? voiceText(i18n, 'listenSample')
                    : voiceTime(_duration!.inMilliseconds / 1000),
              ),
              trailing: VoicePlayButton(
                local: widget.file,
                onDuration: (value) {
                  if (mounted) setState(() => _duration = value);
                },
                key: const Key('production-sample-play'),
              ),
            ),
          ),
          const SizedBox(height: 16),
          TextField(
            key: const Key('production-clone-name'),
            controller: _name,
            enabled: !_busy,
            maxLength: 32,
            onChanged: (_) => setState(() => _request = null),
            decoration: InputDecoration(labelText: voiceText(i18n, 'name')),
          ),
          CheckboxListTile(
            key: const Key('production-voice-consent'),
            contentPadding: EdgeInsets.zero,
            value: _consented,
            onChanged: _busy
                ? null
                : (value) => setState(() => _consented = value == true),
            title: Text(voiceText(i18n, 'consent')),
          ),
          TextButton(
            onPressed: _busy ? null : () => Navigator.pop(context, false),
            child: Text(voiceText(i18n, 'changeSample')),
          ),
          if (_duration != null && !_validSample)
            Text(
              voiceText(i18n, 'sampleDuration'),
              style: TextStyle(color: context.tokens.dangerInk),
            ),
          if (_error != null)
            Text(_error!, style: TextStyle(color: context.tokens.dangerInk)),
        ],
      ),
      bottomNavigationBar: VoiceFooter(
        label: voiceText(i18n, _busy ? 'processing' : 'create'),
        onPressed:
            _busy || !_consented || !_validSample || _name.text.trim().isEmpty
            ? null
            : _save,
      ),
    );
  }
}

String voiceTime(num seconds) {
  final n = seconds.floor();
  return '${(n ~/ 60).toString().padLeft(2, '0')}:${(n % 60).toString().padLeft(2, '0')}';
}

class VoiceCard extends StatelessWidget {
  const VoiceCard({super.key, required this.child, this.selected = false});
  final Widget child;
  final bool selected;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: selected ? t.hairSoft : t.card,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(15),
        side: BorderSide(color: selected ? t.a700 : t.hair),
      ),
      clipBehavior: Clip.antiAlias,
      child: child,
    );
  }
}

class VoiceFooter extends StatelessWidget {
  const VoiceFooter({
    super.key,
    required this.label,
    this.onPressed,
    this.secondary,
  });
  final String label;
  final VoidCallback? onPressed;
  final Widget? secondary;

  @override
  Widget build(BuildContext context) => SafeArea(
    top: false,
    minimum: const EdgeInsets.fromLTRB(16, 8, 16, 8),
    child: Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        ?secondary,
        ProductionPrimary(
          key: const Key('production-voice-primary'),
          label: label,
          onPressed: onPressed,
        ),
      ],
    ),
  );
}

class VoicePlayButton extends ConsumerStatefulWidget {
  const VoicePlayButton({super.key, this.load, this.local, this.onDuration});
  final Future<String> Function()? load;
  final String? local;
  final ValueChanged<Duration>? onDuration;

  @override
  ConsumerState<VoicePlayButton> createState() => _VoicePlayButtonState();
}

class _VoicePlayButtonState extends ConsumerState<VoicePlayButton> {
  static _VoicePlayButtonState? _active;
  VideoPlayerController? _player;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    final local = widget.local;
    if (local != null && widget.onDuration != null) {
      _busy = true;
      unawaited(_inspectLocal(local));
    }
  }

  Future<void> _inspectLocal(String path) async {
    final player = VideoPlayerController.file(File(path));
    _player = player;
    try {
      await player.initialize();
      if (!mounted) return;
      player.addListener(_refresh);
      widget.onDuration?.call(player.value.duration);
    } catch (_) {
      if (mounted) {
        _player = null;
        widget.onDuration?.call(Duration.zero);
        await player.dispose();
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  void dispose() {
    if (_active == this) _active = null;
    unawaited(_player?.dispose());
    super.dispose();
  }

  void _refresh() {
    if (mounted) setState(() {});
  }

  Future<void> _play() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      final previous = _active;
      if (previous != this) await previous?._player?.pause();
      final existing = _player;
      if (existing?.value.isPlaying == true) {
        await existing!.pause();
        return;
      }
      _active = this;
      final local = widget.local;
      final player =
          existing ??
          (local != null
              ? VideoPlayerController.file(File(local))
              : VideoPlayerController.networkUrl(
                  Uri.parse(await widget.load!()),
                ));
      if (!mounted) {
        if (existing == null) await player.dispose();
        return;
      }
      if (existing == null) {
        _player = player;
        await player.initialize();
        player.addListener(_refresh);
      }
      if (!mounted || _active != this) return;
      if (player.value.position >= player.value.duration) {
        await player.seekTo(Duration.zero);
      }
      await player.play();
    } catch (_) {
      await _player?.dispose();
      _player = null;
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(voiceText(ref.read(i18nProvider), 'playError')),
          ),
        );
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final playing = _player?.value.isPlaying == true;
    return IconButton.outlined(
      tooltip: voiceText(i18n, playing ? 'pause' : 'listen'),
      onPressed: _busy ? null : _play,
      icon: _busy
          ? const SizedBox(
              width: 16,
              height: 16,
              child: CircularProgressIndicator(strokeWidth: 2),
            )
          : Icon(playing ? Icons.pause : Icons.play_arrow, size: 19),
    );
  }
}
