import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/json.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/widgets/toast.dart';
import '../workspace/state/active_workspace_store.dart';
import 'api/light_api.dart';

final _myTemplates = FutureProvider.autoDispose((ref) {
  ref.watch(activeWorkspaceProvider);
  return ref.watch(lightApiProvider).ownTemplates();
});

class MediaTemplatesPage extends ConsumerStatefulWidget {
  const MediaTemplatesPage({super.key, this.highlight});

  final String? highlight;

  @override
  ConsumerState<MediaTemplatesPage> createState() => _MediaTemplatesPageState();
}

class _MediaTemplatesPageState extends ConsumerState<MediaTemplatesPage> {
  bool _busy = false;

  String _t(String key) =>
      ref.read(i18nProvider).t('light:mediaTemplates.$key');

  Future<void> _run(Future<void> Function() action) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await action();
      if (mounted) ref.invalidate(_myTemplates);
    } catch (error) {
      if (mounted) {
        ref
            .read(toastProvider.notifier)
            .error(errorText(ref.read(i18nProvider), error));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _try(String edition, [String? session]) => _run(() async {
    final id =
        session ?? await ref.read(lightApiProvider).startTemplateTrial(edition);
    if (mounted) await context.push(Paths.lightProduction(id));
  });

  Future<void> _submit(String edition, String entry, String session) =>
      _run(() async {
        final api = ref.read(lightApiProvider);
        final categories = await api.templateCategories();
        if (!mounted) return;
        final parent = categories
            .where((category) => category['id'] == entry)
            .firstOrNull;
        final options = asList(parent?['scenes']).map(asMap).toList();
        String? scene;
        var consent = false;
        final category = await showDialog<String>(
          context: context,
          builder: (dialog) => StatefulBuilder(
            builder: (dialog, setDialogState) => AlertDialog(
              title: Text(_t('submit')),
              content: SingleChildScrollView(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    DropdownButtonFormField<String>(
                      initialValue: scene,
                      decoration: InputDecoration(labelText: _t('category')),
                      items: [
                        for (final child in options)
                          DropdownMenuItem(
                            value: asString(child['id']),
                            child: Text(asString(child['label']) ?? ''),
                          ),
                      ],
                      onChanged: (value) => setDialogState(() => scene = value),
                    ),
                    CheckboxListTile(
                      value: consent,
                      onChanged: (value) =>
                          setDialogState(() => consent = value == true),
                      title: Text(_t('consent')),
                      controlAffinity: ListTileControlAffinity.leading,
                      contentPadding: EdgeInsets.zero,
                    ),
                  ],
                ),
              ),
              actions: [
                TextButton(
                  onPressed: () => Navigator.pop(dialog),
                  child: Text(_t('cancel')),
                ),
                FilledButton(
                  onPressed: consent && scene != null
                      ? () => Navigator.pop(dialog, scene)
                      : null,
                  child: Text(_t('submit')),
                ),
              ],
            ),
          ),
        );
        if (category != null) {
          await api.submitTemplate(edition, session, category);
        }
      });

  @override
  Widget build(BuildContext context) {
    final templates = ref.watch(_myTemplates);
    final i18n = ref.watch(i18nProvider);
    return Scaffold(
      appBar: AppBar(title: Text(_t('mine'))),
      body: templates.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (error, _) => Center(
          child: TextButton(
            onPressed: () => ref.invalidate(_myTemplates),
            child: Text(errorText(i18n, error)),
          ),
        ),
        data: (rows) => RefreshIndicator(
          onRefresh: () async => ref.refresh(_myTemplates.future),
          child: ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.all(16),
            children: [
              if (rows.isEmpty) Text(_t('empty')),
              for (final row in rows) _card(row),
            ],
          ),
        ),
      ),
    );
  }

  Widget _card(Map<String, dynamic> row) {
    final id = asString(row['id']) ?? '';
    final status = asString(row['status']) ?? 'draft';
    final canTest =
        status == 'draft' &&
        row['source'] == 'creator' &&
        row['can_trial'] != false;
    final trials = asList(row['trials']).map(asMap).toList();
    final confirmed = trials
        .where((trial) => trial['confirmed'] == true)
        .firstOrNull;
    final latest = confirmed ?? trials.firstOrNull;
    final session = asString(latest?['session_id']);
    final source = asString(row['source_session_id']);
    return Card(
      color: id == widget.highlight
          ? context.tokens.hairSoft
          : context.tokens.card,
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '${row['title']} · v${row['version']}',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            Text(_t('states.$status')),
            if (asString(row['reason']) case final String reason
                when reason.isNotEmpty)
              Text(reason),
            Wrap(
              spacing: 8,
              runSpacing: 2,
              children: [
                if (source != null)
                  TextButton(
                    onPressed: _busy
                        ? null
                        : () => context.push(
                            Uri(
                              path: Paths.chat(source),
                              queryParameters: {
                                if (row['source'] == 'creator')
                                  'reviseTemplate': id
                                else
                                  'templateAsset': ?asString(
                                    row['source_asset_id'],
                                  ),
                              },
                            ).toString(),
                          ),
                    child: Text(_t('source')),
                  ),
                if (canTest && session != null)
                  TextButton(
                    onPressed: _busy ? null : () => _try(id, session),
                    child: Text(_t('resume')),
                  ),
                if (canTest && confirmed == null)
                  TextButton(
                    onPressed: _busy ? null : () => _try(id),
                    child: Text(_t('try')),
                  ),
                if (canTest && confirmed != null && session != null)
                  FilledButton(
                    onPressed: _busy
                        ? null
                        : () => _submit(id, row['entry_id'] as String, session),
                    child: Text(_t('submit')),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
