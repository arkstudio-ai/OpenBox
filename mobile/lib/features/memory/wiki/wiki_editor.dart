import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/spinner.dart';
import '../api/knowledge_api.dart';
import '../models/wiki_models.dart';
import '../utils/knowledge_text.dart';
import '../widgets/knowledge_parts.dart';

/// Edit a page in place (web `WikiEditor`): its title and the plain text
/// behind it. Saving updates the underlying memories or file in one step;
/// the page rebuilds itself in the background. Resolves true once saved.
Future<bool> showWikiEditor(BuildContext context, String pageId) async =>
    await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: context.tokens.card,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(Radii.xl2)),
      ),
      builder: (_) => _WikiEditorSheet(pageId: pageId),
    ) ??
    false;

class _WikiEditorSheet extends ConsumerStatefulWidget {
  const _WikiEditorSheet({required this.pageId});

  final String pageId;

  @override
  ConsumerState<_WikiEditorSheet> createState() => _WikiEditorSheetState();
}

class _WikiEditorSheetState extends ConsumerState<_WikiEditorSheet> {
  late Future<WikiEditSnapshot> _snapshot = _load();

  // The base revision is the one first shown; nothing later moves it forward
  // under an unsaved edit.
  WikiEditSnapshot? _base;
  final _title = TextEditingController();
  final _entries = <TextEditingController>[];

  /// One per editor, so a retried save is the same command.
  final _requestId = newRequestId();
  bool _pending = false;
  String? _error;

  Future<WikiEditSnapshot> _load() =>
      ref.read(knowledgeApiProvider).editSnapshot(widget.pageId);

  @override
  void dispose() {
    _title.dispose();
    for (final entry in _entries) {
      entry.dispose();
    }
    super.dispose();
  }

  void _adopt(WikiEditSnapshot snapshot) {
    if (_base != null) return;
    _base = snapshot;
    _title.text = snapshot.title;
    for (final entry in snapshot.entries) {
      _entries.add(TextEditingController(text: entry.text));
    }
  }

  bool get _changed {
    final base = _base!;
    if (_title.text.trim() != base.title) return true;
    for (var i = 0; i < base.entries.length; i++) {
      if (_entries[i].text != base.entries[i].text) return true;
    }
    return false;
  }

  bool get _complete =>
      _title.text.trim().isNotEmpty &&
      _entries.every((entry) => entry.text.trim().isNotEmpty);

  Future<void> _save() async {
    final base = _base!;
    final i18n = ref.read(i18nProvider);
    setState(() {
      _pending = true;
      _error = null;
    });
    try {
      await ref.read(knowledgeApiProvider).edit(base, _title.text.trim(), [
        for (var i = 0; i < base.entries.length; i++)
          WikiEditEntry(
            id: base.entries[i].id,
            revision: base.entries[i].revision,
            text: _entries[i].text,
            maxLength: base.entries[i].maxLength,
          ),
      ], _requestId);
      if (mounted) Navigator.of(context).pop(true);
    } catch (error) {
      if (!mounted) return;
      // A conflict keeps the person's text exactly where it is.
      setState(
        () => _error = apiErrorOf(error)?.status == 409
            ? i18n.t('wiki:consumer.conflict')
            : errorText(i18n, error),
      );
    } finally {
      if (mounted) setState(() => _pending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final media = MediaQuery.of(context);
    return PopScope(
      canPop: !_pending,
      child: Padding(
        padding: EdgeInsets.only(bottom: media.viewInsets.bottom),
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(18, 18, 18, 14),
          child: FutureBuilder<WikiEditSnapshot>(
            future: _snapshot,
            builder: (context, snapshot) {
              final data = snapshot.data;
              if (data != null) _adopt(data);
              return Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    i18n.t('wiki:consumer.edit'),
                    style: TextStyle(
                      fontSize: FontSizes.lg,
                      fontWeight: FontWeight.w600,
                      color: t.ink,
                    ),
                  ),
                  if (_base != null)
                    ..._form(t, i18n)
                  else if (snapshot.hasError)
                    ..._failed(i18n, snapshot.error!)
                  else
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 24),
                      child: Row(
                        children: [
                          const Spinner(),
                          const SizedBox(width: 8),
                          Text(
                            i18n.t('wiki:loading'),
                            style: TextStyle(
                              fontSize: FontSizes.sm,
                              color: t.n600,
                            ),
                          ),
                        ],
                      ),
                    ),
                ],
              );
            },
          ),
        ),
      ),
    );
  }

  List<Widget> _failed(I18nState i18n, Object error) => [
    const SizedBox(height: 14),
    ErrorNotice(text: errorText(i18n, error)),
    const SizedBox(height: 16),
    Row(
      mainAxisAlignment: MainAxisAlignment.end,
      children: [
        KnowledgeButton(
          label: i18n.t('wiki:consumer.cancel'),
          onPressed: () => Navigator.of(context).pop(false),
        ),
        const SizedBox(width: 8),
        KnowledgeButton(
          tone: PillTone.primary,
          label: i18n.t('wiki:retry'),
          onPressed: () => setState(() => _snapshot = _load()),
        ),
      ],
    ),
  ];

  List<Widget> _form(BossipTokens t, I18nState i18n) {
    final base = _base!;
    return [
      const SizedBox(height: 6),
      Text(
        i18n.t('wiki:consumer.editorHint'),
        style: TextStyle(fontSize: FontSizes.sm, height: 1.6, color: t.n600),
      ),
      _Field(
        label: i18n.t('wiki:consumer.titleLabel'),
        child: TextField(
          key: const ValueKey('wiki-editor-title'),
          controller: _title,
          enabled: !_pending,
          inputFormatters: [LengthLimitingTextInputFormatter(160)],
          onChanged: (_) => setState(() {}),
          style: TextStyle(fontSize: FontSizes.base, color: t.ink),
          decoration: _decoration(t),
        ),
      ),
      for (var i = 0; i < base.entries.length; i++)
        _Field(
          label: base.entries.length == 1
              ? i18n.t('wiki:consumer.content')
              : i18n.t('wiki:consumer.contentNumber', vars: {'number': i + 1}),
          child: TextField(
            key: ValueKey('wiki-editor-entry-$i'),
            controller: _entries[i],
            enabled: !_pending,
            minLines: 5,
            maxLines: 12,
            keyboardType: TextInputType.multiline,
            inputFormatters: [
              LengthLimitingTextInputFormatter(base.entries[i].maxLength),
            ],
            onChanged: (_) => setState(() {}),
            style: TextStyle(
              fontSize: FontSizes.base,
              height: 1.6,
              color: t.ink,
            ),
            decoration: _decoration(t),
          ),
        ),
      if (_error != null) ...[
        const SizedBox(height: 12),
        ErrorNotice(key: const ValueKey('wiki-editor-error'), text: _error!),
      ],
      const SizedBox(height: 16),
      Row(
        mainAxisAlignment: MainAxisAlignment.end,
        children: [
          KnowledgeButton(
            label: i18n.t('wiki:consumer.cancel'),
            onPressed: _pending ? null : () => Navigator.of(context).pop(false),
          ),
          const SizedBox(width: 8),
          KnowledgeButton(
            key: const ValueKey('wiki-editor-save'),
            tone: PillTone.primary,
            label: i18n.t(
              _pending ? 'wiki:consumer.saving' : 'wiki:consumer.save',
            ),
            onPressed: !_pending && _changed && _complete ? _save : null,
          ),
        ],
      ),
    ];
  }
}

InputDecoration _decoration(BossipTokens t) {
  OutlineInputBorder border(Color color) => OutlineInputBorder(
    borderRadius: BorderRadius.circular(Radii.md),
    borderSide: BorderSide(color: color),
  );
  return InputDecoration(
    isDense: true,
    filled: true,
    fillColor: t.bg,
    contentPadding: const EdgeInsets.all(12),
    border: border(t.hair),
    enabledBorder: border(t.hair),
    disabledBorder: border(t.hair),
    focusedBorder: border(t.accent),
  );
}

class _Field extends StatelessWidget {
  const _Field({required this.label, required this.child});

  final String label;
  final Widget child;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(top: 14),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          label,
          style: TextStyle(fontSize: FontSizes.sm, color: context.tokens.n700),
        ),
        const SizedBox(height: 6),
        child,
      ],
    ),
  );
}
