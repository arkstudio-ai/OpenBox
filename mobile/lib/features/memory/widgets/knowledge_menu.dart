import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/download/native_download.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/labeled_checkbox.dart';
import '../../../shared/widgets/toast.dart';
import '../api/knowledge_api.dart';
import '../state/knowledge_providers.dart';
import 'knowledge_parts.dart';

enum _MenuAction { autoSave, export, clear }

/// The person's controls over memory (web `KnowledgeMenu`): whether chats
/// are remembered at all, taking everything with them, and clearing it.
Future<void> showKnowledgeMenu(
  BuildContext context,
  WidgetRef ref, {
  required String projectId,
  required String? scopeName,
}) async {
  final choice =
      await showModalBottomSheet<({_MenuAction action, bool autoSave})>(
        context: context,
        backgroundColor: context.tokens.card,
        builder: (_) => const _MenuSheet(),
      );
  if (choice == null || !context.mounted) return;
  final i18n = ref.read(i18nProvider);
  final toast = ref.read(toastProvider.notifier);
  final api = ref.read(knowledgeApiProvider);
  switch (choice.action) {
    case _MenuAction.autoSave:
      try {
        // The state the person saw when they chose, not a later read.
        final next = await api.setAutoSave(!choice.autoSave);
        toast.success(
          i18n.t(
            next.autoSave
                ? 'knowledge:manage.autoSaveOn'
                : 'knowledge:manage.autoSaveOff',
          ),
        );
        if (context.mounted) ref.invalidate(memorySettingsProvider);
      } catch (error) {
        toast.error(errorText(i18n, error));
      }
    case _MenuAction.export:
      try {
        final bytes = await api.exportAll(i18n.language);
        await ref
            .read(nativeDownloadProvider)
            .saveBytes(
              bytes: bytes,
              suggestedName: i18n.t('knowledge:manage.exportFile'),
              mimeType: 'text/markdown',
            );
      } catch (error) {
        toast.error(errorText(i18n, error));
      }
    case _MenuAction.clear:
      await showDialog<void>(
        context: context,
        builder: (_) =>
            _ClearAllDialog(projectId: projectId, scopeName: scopeName),
      );
  }
}

class _MenuSheet extends ConsumerWidget {
  const _MenuSheet();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final autoSave =
        ref.watch(memorySettingsProvider).current?.autoSave ?? true;
    final label = TextStyle(fontSize: FontSizes.base, color: t.ink);
    return SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 16, 20, 6),
            child: Align(
              alignment: Alignment.centerLeft,
              child: Text(
                i18n.t('knowledge:manage.title'),
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  fontWeight: FontWeight.w600,
                  color: t.n600,
                ),
              ),
            ),
          ),
          ListTile(
            key: const ValueKey('knowledge-auto-save'),
            leading: Icon(
              Icons.check,
              size: 18,
              color: autoSave ? t.a700 : Colors.transparent,
            ),
            title: Text(i18n.t('knowledge:manage.autoSave'), style: label),
            subtitle: Text(
              i18n.t(
                autoSave
                    ? 'knowledge:manage.autoSaveHint'
                    : 'knowledge:manage.autoSaveOffHint',
              ),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
            onTap: () => Navigator.of(
              context,
            ).pop((action: _MenuAction.autoSave, autoSave: autoSave)),
          ),
          ListTile(
            key: const ValueKey('knowledge-export'),
            leading: const SizedBox(width: 18),
            title: Text(i18n.t('knowledge:manage.export'), style: label),
            onTap: () => Navigator.of(
              context,
            ).pop((action: _MenuAction.export, autoSave: autoSave)),
          ),
          ListTile(
            key: const ValueKey('knowledge-clear'),
            leading: const SizedBox(width: 18),
            title: Text(
              i18n.t('knowledge:manage.clear'),
              style: label.copyWith(color: t.dangerInk),
            ),
            onTap: () => Navigator.of(
              context,
            ).pop((action: _MenuAction.clear, autoSave: autoSave)),
          ),
          const SizedBox(height: 8),
        ],
      ),
    );
  }
}

/// Forgets every memory in view only after an explicit confirmation.
class _ClearAllDialog extends ConsumerStatefulWidget {
  const _ClearAllDialog({required this.projectId, required this.scopeName});

  final String projectId;
  final String? scopeName;

  @override
  ConsumerState<_ClearAllDialog> createState() => _ClearAllDialogState();
}

class _ClearAllDialogState extends ConsumerState<_ClearAllDialog> {
  bool _understood = false;
  bool _pending = false;
  String? _error;

  Future<void> _clear() async {
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    final invalidate = ProviderScope.containerOf(
      context,
      listen: false,
    ).invalidate;
    setState(() {
      _pending = true;
      _error = null;
    });
    try {
      final forgotten = await ref
          .read(knowledgeApiProvider)
          .forgetAll(widget.projectId);
      toast.success(
        i18n.t('knowledge:manage.cleared', vars: {'count': forgotten}),
      );
      invalidateKnowledge(invalidate);
      if (mounted) Navigator.of(context).pop();
    } catch (error) {
      if (mounted) setState(() => _error = errorText(i18n, error));
    } finally {
      if (mounted) setState(() => _pending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final scope = widget.scopeName;
    return PopScope(
      canPop: !_pending,
      child: AlertDialog(
        title: Text(
          i18n.t('knowledge:manage.clearTitle'),
          style: const TextStyle(fontSize: FontSizes.lg),
        ),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                scope != null
                    ? i18n.t(
                        'knowledge:manage.clearScope',
                        vars: {'scope': scope},
                      )
                    : i18n.t('knowledge:manage.clearEverywhere'),
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  height: 1.6,
                  color: t.n700,
                ),
              ),
              LabeledCheckbox(
                key: const ValueKey('knowledge-clear-understood'),
                value: _understood,
                label: i18n.t('knowledge:manage.clearConfirm'),
                onChanged: (value) {
                  if (!_pending) setState(() => _understood = value);
                },
              ),
              if (_error != null) ErrorNotice(text: _error!),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: _pending ? null : () => Navigator.of(context).pop(),
            child: Text(i18n.t('knowledge:forget.cancel')),
          ),
          TextButton(
            key: const ValueKey('knowledge-clear-confirm'),
            onPressed: !_understood || _pending ? null : _clear,
            child: Text(
              i18n.t(
                _pending
                    ? 'knowledge:manage.clearing'
                    : 'knowledge:manage.clearAction',
              ),
              style: TextStyle(
                color: !_understood || _pending ? t.n500 : t.dangerInk,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
