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
import 'knowledge_sheets.dart';

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
        showDragHandle: true,
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
    void choose(_MenuAction action) =>
        Navigator.of(context).pop((action: action, autoSave: autoSave));
    return SafeArea(
      top: false,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
            child: SheetTitle(i18n.t('knowledge:manage.title')),
          ),
          ListTile(
            key: const ValueKey('knowledge-auto-save'),
            contentPadding: const EdgeInsets.symmetric(horizontal: 20),
            horizontalTitleGap: 14,
            leading: Icon(Icons.chat_bubble_outline, size: 20, color: t.n700),
            title: Text(i18n.t('knowledge:manage.autoSave'), style: label),
            subtitle: Text(
              i18n.t(
                autoSave
                    ? 'knowledge:manage.autoSaveHint'
                    : 'knowledge:manage.autoSaveOffHint',
              ),
              style: TextStyle(
                fontSize: FontSizes.xs,
                height: 1.5,
                color: t.n600,
              ),
            ),
            trailing: autoSave
                ? Icon(Icons.check_rounded, size: 20, color: t.a700)
                : null,
            onTap: () => choose(_MenuAction.autoSave),
          ),
          ListTile(
            key: const ValueKey('knowledge-export'),
            minTileHeight: 50,
            contentPadding: const EdgeInsets.symmetric(horizontal: 20),
            horizontalTitleGap: 14,
            leading: Icon(
              Icons.file_download_outlined,
              size: 20,
              color: t.n700,
            ),
            title: Text(i18n.t('knowledge:manage.export'), style: label),
            onTap: () => choose(_MenuAction.export),
          ),
          ListTile(
            key: const ValueKey('knowledge-clear'),
            minTileHeight: 50,
            contentPadding: const EdgeInsets.symmetric(horizontal: 20),
            horizontalTitleGap: 14,
            leading: Icon(
              Icons.delete_sweep_outlined,
              size: 20,
              color: t.dangerInk,
            ),
            title: Text(
              i18n.t('knowledge:manage.clear'),
              style: label.copyWith(color: t.dangerInk),
            ),
            onTap: () => choose(_MenuAction.clear),
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
