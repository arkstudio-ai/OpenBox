import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/widgets/media_viewer.dart';
import 'production_api.dart';
import 'production_voice.dart';
import 'production_widgets.dart';

final sourceSpeechProvider = FutureProvider.autoDispose
    .family<Map<String, dynamic>, (String, String)>((ref, key) {
      return ref.watch(lightProductionApiProvider).speech(key.$1);
    });

class SourceSpeechSettings extends ConsumerWidget {
  const SourceSpeechSettings({
    super.key,
    required this.task,
    required this.enabled,
    required this.redo,
    required this.onChange,
  });
  final LightProduction task;
  final bool enabled, redo;
  final ValueChanged<bool> onChange;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (task.assets.isEmpty) return const SizedBox.shrink();
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final request = sourceSpeechProvider((
      task.sessionId,
      task.assets.join(','),
    ));
    final result = ref.watch(request);
    final sources = asList(result.valueOrNull?['sources']).map(asMap).toList();
    final incomplete = result.valueOrNull?['incomplete'] == true;
    final status = result.isLoading
        ? 'speechChecking'
        : sources.isNotEmpty
        ? 'speechPresent'
        : result.hasError || incomplete
        ? 'speechUnknown'
        : 'speechAbsent';
    final hint = redo
        ? (sources.isNotEmpty ? 'redoHint' : 'addHint')
        : 'keepHint';
    return Padding(
      padding: const EdgeInsets.only(top: 12),
      child: VoiceCard(
        child: Column(
          children: [
            ListTile(
              key: const Key('production-speech-detection'),
              leading: const Icon(Icons.mic_none_rounded),
              title: Text(voiceText(i18n, status)),
              subtitle: sources.isEmpty
                  ? null
                  : Text(
                      voiceText(
                        i18n,
                        'speechCount',
                        vars: {'count': sources.length},
                      ),
                    ),
              trailing: sources.isNotEmpty
                  ? VoicePlayButton(
                      load: () async => sources.first['url'] as String,
                    )
                  : (result.hasError || incomplete)
                  ? IconButton(
                      tooltip: voiceText(i18n, 'retry'),
                      onPressed: () => ref.invalidate(request),
                      icon: const Icon(Icons.refresh),
                    )
                  : null,
            ),
            Divider(height: 1, indent: 16, endIndent: 16, color: t.hair),
            SwitchListTile.adaptive(
              key: const Key('production-redo-narration'),
              title: Text(
                voiceText(
                  i18n,
                  sources.isEmpty && status == 'speechAbsent'
                      ? 'addNarration'
                      : 'redo',
                ),
              ),
              subtitle: Text(
                voiceText(
                  i18n,
                  incomplete && sources.isNotEmpty ? 'speechPartial' : hint,
                ),
              ),
              value: redo,
              onChanged:
                  enabled &&
                      (!result.isLoading && !result.hasError && !incomplete ||
                          redo)
                  ? onChange
                  : null,
            ),
          ],
        ),
      ),
    );
  }
}

class NarrationReviewPage extends ConsumerStatefulWidget {
  const NarrationReviewPage({super.key, required this.task});
  final LightProduction task;

  @override
  ConsumerState<NarrationReviewPage> createState() =>
      _NarrationReviewPageState();
}

class _NarrationReviewPageState extends ConsumerState<NarrationReviewPage> {
  late LightProduction _task = widget.task;
  final _emphasis = TextEditingController();
  Map<String, dynamic> _detection = {};
  List<Map<String, dynamic>> _shots = [];
  List<String> _lines = [];
  Map<String, dynamic>? _preview;
  String? _progress, _error, _confirmRequest;
  int _page = 0;

  LightProductionApi get _api => ref.read(lightProductionApiProvider);
  I18nState get _i18n => ref.read(i18nProvider);
  String _t(String key, {Map<String, Object?>? vars}) =>
      voiceText(_i18n, key, vars: vars);

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _emphasis.dispose();
    super.dispose();
  }

  Future<void> _work(String label, Future<void> Function() work) async {
    if (_progress != null) return;
    setState(() {
      _progress = label;
      _error = null;
    });
    try {
      await work();
    } catch (error) {
      if (mounted) {
        final response = apiErrorOf(error);
        setState(
          () => _error = switch (response?.code) {
            'PRODUCTION_NARRATION_LENGTH' => _t(
              'lineLength',
              vars: {'shot': response?.meta['shot']},
            ),
            'PRODUCTION_NARRATION_MISMATCH' => _t(
              'lineMismatch',
              vars: {'shot': response?.meta['shot']},
            ),
            _ => productionVoiceError(_i18n, error),
          },
        );
      }
    } finally {
      if (mounted) setState(() => _progress = null);
    }
  }

  void _load() => _work('speechChecking', () async {
    final data = await _api.speech(_task.sessionId);
    if (!mounted) return;
    _detection = data;
    final draft = asMap(data['draft']);
    if (draft.isNotEmpty) {
      _setDraft(draft);
      final previous = asMap(data['preview']);
      if (previous.isNotEmpty) {
        _lines = asList(previous['lines']).whereType<String>().toList();
        _preview = previous;
      }
    }
  });

  void _setDraft(Map<String, dynamic> data) {
    _shots = asList(data['shots']).map(asMap).toList();
    _lines = _shots.map((shot) => asString(shot['text']) ?? '').toList();
    _emphasis.text = asString(data['emphasis']) ?? '';
    _preview = null;
    _confirmRequest = null;
    _page = 1;
  }

  void _draft({required bool refine}) => _work('planning', () async {
    final draft = await _api.narrationDraft(
      _task,
      _emphasis.text,
      refine ? _lines : null,
    );
    final refreshed = await _api.get(_task.sessionId);
    if (!mounted) return;
    _task = refreshed;
    _setDraft(draft);
  });

  void _chooseAndPreview() => _work('previewPreparing', () async {
    final selection = await chooseProductionVoice(
      context,
      _task.voiceId,
      allowNone: false,
    );
    if (!mounted || selection == null) return;
    if (selection != _task.voiceId) {
      _task = await _api.edit(_task, {'voice_id': selection});
      _preview = null;
      _confirmRequest = null;
    }
    final preview = await _api.narrationPreview(_task, _lines);
    final refreshed = await _api.get(_task.sessionId);
    if (!mounted) return;
    _task = refreshed;
    _preview = preview;
    _page = 2;
  });

  void _confirm() => _work('confirming', () async {
    final task = await _api.action(
      _task,
      'render',
      _confirmRequest ??= productionRequestId(),
      narrationPreviewId: _preview!['id'] as String,
    );
    if (mounted) Navigator.pop(context, task);
  });

  Future<void> _editLine(int index) async {
    var text = _lines[index];
    final line = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(_shots[index]['label'] as String),
        content: TextFormField(
          key: const Key('production-narration-line-input'),
          initialValue: text,
          onChanged: (value) => text = value,
          autofocus: true,
          maxLength: 240,
          minLines: 2,
          maxLines: 5,
          decoration: InputDecoration(hintText: _t('lineHint')),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: Text(_i18n.t('common:action.cancel')),
          ),
          TextButton(
            onPressed: () => Navigator.pop(context, text.trim()),
            child: Text(_t('saveLine')),
          ),
        ],
      ),
    );
    if (!mounted || line == null || line == _lines[index]) return;
    await _work('savingLine', () async {
      final revised = [..._lines]..[index] = line;
      await _api.saveNarrationLines(_task, revised);
      final task = await _api.get(_task.sessionId);
      if (!mounted) return;
      _task = task;
      _lines = revised;
      _preview = null;
      _confirmRequest = null;
    });
  }

  List<Widget> _sourceCards() {
    final sources = asList(_detection['sources']).map(asMap).toList();
    return [
      if (sources.isEmpty && _detection.isNotEmpty)
        Text(
          _t(
            _detection['incomplete'] == true ? 'speechUnknown' : 'speechAbsent',
          ),
        ),
      for (final (index, source) in sources.indexed)
        Padding(
          padding: const EdgeInsets.only(bottom: 10),
          child: VoiceCard(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(14, 8, 14, 14),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          _t('sourceNumber', vars: {'index': index + 1}),
                          style: const TextStyle(fontWeight: FontWeight.w700),
                        ),
                      ),
                      VoicePlayButton(
                        load: () async => source['url'] as String,
                      ),
                    ],
                  ),
                  Text(
                    source['text'] as String,
                    style: const TextStyle(fontSize: 15, height: 1.45),
                  ),
                ],
              ),
            ),
          ),
        ),
    ];
  }

  @override
  Widget build(BuildContext context) {
    ref.watch(i18nProvider);
    final t = context.tokens;
    final catalogue = _page == 2
        ? ref.watch(productionVoicesProvider).valueOrNull
        : null;
    final hasSpeech = asList(_detection['sources']).isNotEmpty;
    final busy = _progress != null;
    return PopScope(
      canPop: !busy,
      child: Scaffold(
        appBar: AppBar(
          title: Text(_task.kind.label(_i18n)),
          leading: BackButton(
            onPressed: busy
                ? null
                : () {
                    if (_page == 0) {
                      Navigator.pop(context);
                    } else {
                      setState(() => _page--);
                    }
                  },
          ),
        ),
        body: ListView(
          key: ValueKey('narration-page-$_page'),
          padding: const EdgeInsets.fromLTRB(16, 10, 16, 20),
          keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
          children: [
            ProductionHeading(
              _t(switch (_page) {
                0 => hasSpeech ? 'originalScript' : 'narrationScript',
                1 => 'shotReview',
                _ => 'previewTitle',
              }),
              description: _t(switch (_page) {
                0 => hasSpeech ? 'originalScriptHint' : 'newScriptHint',
                1 => 'timelineHint',
                _ => 'previewHint',
              }),
            ),
            const SizedBox(height: 18),
            if (busy) ...[
              LinearProgressIndicator(semanticsLabel: _t(_progress!)),
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 8),
                child: Text(_t(_progress!)),
              ),
            ],
            if (_page == 0) ...[
              ..._sourceCards(),
              if (_detection.isNotEmpty) ...[
                const SizedBox(height: 10),
                TextField(
                  key: const Key('production-narration-emphasis'),
                  controller: _emphasis,
                  enabled: !busy,
                  maxLength: 500,
                  minLines: 2,
                  maxLines: 5,
                  decoration: InputDecoration(
                    labelText: _t('emphasis'),
                    hintText: _t('emphasisHint'),
                  ),
                ),
              ] else if (!busy)
                TextButton(onPressed: _load, child: Text(_t('retry'))),
            ] else if (_page == 1) ...[
              if (hasSpeech)
                Align(
                  alignment: Alignment.centerRight,
                  child: TextButton.icon(
                    onPressed: busy ? null : () => setState(() => _page = 0),
                    icon: const Icon(Icons.description_outlined),
                    label: Text(_t('originalScript')),
                  ),
                ),
              for (final (index, shot) in _shots.indexed)
                Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: VoiceCard(
                    child: InkWell(
                      key: ValueKey('production-narration-line-$index'),
                      onTap: busy ? null : () => _editLine(index),
                      child: Padding(
                        padding: const EdgeInsets.all(10),
                        child: Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            ClipRRect(
                              borderRadius: BorderRadius.circular(8),
                              child: Image.network(
                                shot['image'] as String,
                                width: 88,
                                height: 78,
                                fit: BoxFit.cover,
                                errorBuilder: (_, _, _) => SizedBox(
                                  width: 88,
                                  height: 78,
                                  child: ColoredBox(
                                    color: t.hairSoft,
                                    child: const Icon(Icons.movie_outlined),
                                  ),
                                ),
                              ),
                            ),
                            const SizedBox(width: 12),
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(
                                    '${voiceTime(shot['start'] as num)} – ${voiceTime(shot['end'] as num)}',
                                    style: TextStyle(
                                      color: t.n800,
                                      fontSize: 12,
                                      fontWeight: FontWeight.w700,
                                    ),
                                  ),
                                  Text(
                                    shot['label'] as String,
                                    style: TextStyle(
                                      color: t.n800,
                                      fontSize: 12,
                                    ),
                                  ),
                                  const SizedBox(height: 5),
                                  Text(
                                    _lines[index].isEmpty
                                        ? _t('noLine')
                                        : _lines[index],
                                    style: const TextStyle(height: 1.35),
                                  ),
                                ],
                              ),
                            ),
                            const Icon(Icons.edit_outlined, size: 18),
                          ],
                        ),
                      ),
                    ),
                  ),
                ),
              OutlinedButton.icon(
                onPressed: busy ? null : () => _draft(refine: true),
                icon: const Icon(Icons.refresh),
                label: Text(_t('reoptimize')),
              ),
            ] else if (_preview case final preview?) ...[
              ClipRRect(
                borderRadius: BorderRadius.circular(14),
                child: ColoredBox(
                  color: Colors.black,
                  child: SizedBox(
                    height: 270,
                    child: MediaPreviewVideo(
                      key: ValueKey(preview['id']),
                      source: preview['url'] as String,
                      failedText: _t('playError'),
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 12),
              VoiceCard(
                child: ProductionSettingRow(
                  label: _t('settings'),
                  icon: Icons.graphic_eq,
                  value: voiceLabel(
                    _i18n,
                    _task.voiceId,
                    name: savedVoiceName(catalogue, _task.voiceId),
                  ),
                  onTap: busy ? null : _chooseAndPreview,
                ),
              ),
              const SizedBox(height: 12),
              Text(
                _t(hasSpeech ? 'replaceHint' : 'confirmHint'),
                style: TextStyle(color: t.n800),
              ),
            ],
            if (_error != null)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 10),
                child: Text(_error!, style: TextStyle(color: t.dangerInk)),
              ),
          ],
        ),
        bottomNavigationBar: VoiceFooter(
          label: _t(switch (_page) {
            0 => 'optimize',
            1 => 'choose',
            _ => hasSpeech ? 'replace' : 'confirm',
          }),
          onPressed: busy
              ? null
              : switch (_page) {
                  0 =>
                    _detection.isEmpty || _detection['incomplete'] == true
                        ? null
                        : () => _draft(refine: false),
                  1 =>
                    _lines.any((line) => line.isNotEmpty)
                        ? _chooseAndPreview
                        : null,
                  _ => _preview == null ? null : _confirm,
                },
          secondary: _page == 2
              ? TextButton(
                  onPressed: busy ? null : () => setState(() => _page = 1),
                  child: Text(_t('editAgain')),
                )
              : null,
        ),
      ),
    );
  }
}
