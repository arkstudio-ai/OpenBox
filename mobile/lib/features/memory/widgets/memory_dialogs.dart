import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/labeled_checkbox.dart';
import '../../../shared/widgets/toast.dart';
import '../api/knowledge_api.dart';
import '../models/memory_models.dart';
import '../state/knowledge_providers.dart';
import '../utils/knowledge_text.dart';
import 'knowledge_parts.dart';

const _limit = 2000;

/// Why a memory write failed, in the person's terms (web `useMemoryActions`).
String memoryWriteError(I18nState i18n, Object error) {
  final api = apiErrorOf(error);
  if (api?.status == 409) return i18n.t('knowledge:editor.conflict');
  if (api?.code == 'MEMORY_SENSITIVE_CONTENT') {
    return i18n.t('knowledge:editor.sensitive');
  }
  return errorText(i18n, error);
}

/// A refusal ends the command; a lost answer does not. Keeping the request id
/// after a network failure or a server error makes the retry the same command
/// rather than a second one.
bool _answered(Object error) {
  final status = apiErrorOf(error)?.status ?? 0;
  return status > 0 && status < 500;
}

/// Add a memory, or reword one (web `MemoryEditor`). Plain text only: what is
/// written is what the assistant keeps. Resolves with the saved record, or
/// null when closed without saving.
Future<MemoryRecord?> showMemoryEditor(
  BuildContext context, {
  MemoryRecord? memory,
  required String projectId,
  required List<KnowledgeProject> projects,
}) => showModalBottomSheet<MemoryRecord>(
  context: context,
  isScrollControlled: true,
  useSafeArea: true,
  backgroundColor: context.tokens.card,
  shape: const RoundedRectangleBorder(
    borderRadius: BorderRadius.vertical(top: Radius.circular(Radii.xl2)),
  ),
  builder: (_) => _MemoryEditorSheet(
    memory: memory,
    projectId: projectId,
    projects: projects,
  ),
);

class _MemoryEditorSheet extends ConsumerStatefulWidget {
  const _MemoryEditorSheet({
    required this.memory,
    required this.projectId,
    required this.projects,
  });

  final MemoryRecord? memory;
  final String projectId;
  final List<KnowledgeProject> projects;

  @override
  ConsumerState<_MemoryEditorSheet> createState() => _MemoryEditorSheetState();
}

class _MemoryEditorSheetState extends ConsumerState<_MemoryEditorSheet> {
  late final _text = TextEditingController(text: widget.memory?.summary ?? '');

  /// Where a new memory is saved: the scope in view unless changed here.
  late String _target = widget.projectId;
  bool _pending = false;
  String? _error;
  String? _requestId;

  String get _mode => widget.memory == null ? 'create' : 'edit';

  bool get _valid {
    final text = _text.text.trim();
    return text.isNotEmpty && text != widget.memory?.summary;
  }

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_valid || _pending) return;
    final api = ref.read(knowledgeApiProvider);
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    // The sheet can be swiped away mid-save; the save still lands.
    final invalidate = ProviderScope.containerOf(
      context,
      listen: false,
    ).invalidate;
    final summary = _text.text.trim();
    final requestId = _requestId ??= newRequestId();
    setState(() {
      _pending = true;
      _error = null;
    });
    try {
      final memory = widget.memory;
      final saved = memory == null
          ? await api.create(summary, _target, requestId)
          : await api.correct(memory, summary, requestId);
      _requestId = null;
      toast.success(
        i18n.t(
          memory == null
              ? 'knowledge:editor.created'
              : 'knowledge:editor.saved',
        ),
      );
      invalidateKnowledge(invalidate);
      if (mounted) Navigator.of(context).pop(saved);
    } catch (error) {
      if (_answered(error)) _requestId = null;
      // Someone changed it meanwhile: show the current list behind the
      // sheet, and keep the person's text where it is.
      if (apiErrorOf(error)?.status == 409) invalidateKnowledge(invalidate);
      if (mounted) setState(() => _error = memoryWriteError(i18n, error));
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
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                i18n.t('knowledge:editor.${_mode}Title'),
                style: TextStyle(
                  fontSize: FontSizes.lg,
                  fontWeight: FontWeight.w600,
                  color: t.ink,
                ),
              ),
              const SizedBox(height: 6),
              Text(
                i18n.t('knowledge:editor.${_mode}Hint'),
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  height: 1.6,
                  color: t.n600,
                ),
              ),
              const SizedBox(height: 14),
              Semantics(
                label: i18n.t('knowledge:editor.label'),
                textField: true,
                child: TextField(
                  key: const ValueKey('memory-editor-text'),
                  controller: _text,
                  enabled: !_pending,
                  autofocus: true,
                  minLines: 4,
                  maxLines: 8,
                  keyboardType: TextInputType.multiline,
                  inputFormatters: [LengthLimitingTextInputFormatter(_limit)],
                  onChanged: (_) => setState(() {}),
                  style: TextStyle(
                    fontSize: FontSizes.base,
                    height: 1.6,
                    color: t.ink,
                  ),
                  decoration: _fieldDecoration(
                    t,
                    i18n.t('knowledge:editor.placeholder'),
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Row(
                children: [
                  if (widget.memory == null && widget.projects.isNotEmpty)
                    _ScopePicker(
                      projects: widget.projects,
                      value: _target,
                      enabled: !_pending,
                      onChanged: (value) => setState(() => _target = value),
                    ),
                  const Spacer(),
                  Text(
                    i18n.t(
                      'knowledge:editor.characters',
                      vars: {'count': _text.text.length, 'limit': _limit},
                    ),
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
                  ),
                ],
              ),
              if (_error != null) ...[
                const SizedBox(height: 12),
                ErrorNotice(text: _error!),
              ],
              const SizedBox(height: 16),
              Row(
                mainAxisAlignment: MainAxisAlignment.end,
                children: [
                  KnowledgeButton(
                    label: i18n.t('knowledge:editor.cancel'),
                    onPressed: _pending
                        ? null
                        : () => Navigator.of(context).pop(),
                  ),
                  const SizedBox(width: 8),
                  KnowledgeButton(
                    key: const ValueKey('memory-editor-save'),
                    tone: PillTone.primary,
                    label: i18n.t(
                      _pending
                          ? 'knowledge:editor.saving'
                          : 'knowledge:editor.save',
                    ),
                    onPressed: _valid && !_pending ? _submit : null,
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

InputDecoration _fieldDecoration(BossipTokens t, String hint) {
  OutlineInputBorder border(Color color) => OutlineInputBorder(
    borderRadius: BorderRadius.circular(Radii.md),
    borderSide: BorderSide(color: color),
  );
  return InputDecoration(
    isDense: true,
    filled: true,
    fillColor: t.bg,
    hintText: hint,
    hintStyle: TextStyle(fontSize: FontSizes.base, color: t.n500),
    contentPadding: const EdgeInsets.all(12),
    border: border(t.hair),
    enabledBorder: border(t.hair),
    disabledBorder: border(t.hair),
    focusedBorder: border(t.accent),
  );
}

/// "Save to": personal, or one of the projects.
class _ScopePicker extends ConsumerWidget {
  const _ScopePicker({
    required this.projects,
    required this.value,
    required this.enabled,
    required this.onChanged,
  });

  final List<KnowledgeProject> projects;
  final String value;
  final bool enabled;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final style = TextStyle(fontSize: FontSizes.sm, color: t.ink);
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(
          i18n.t('knowledge:editor.saveTo'),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
        ),
        const SizedBox(width: 8),
        ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 170),
          child: DropdownButton<String>(
            key: const ValueKey('memory-editor-scope'),
            // A scope the list does not name (still loading, or gone) shows
            // no choice rather than a wrong one.
            value: value.isEmpty || projects.any((p) => p.id == value)
                ? value
                : null,
            isDense: true,
            isExpanded: true,
            underline: const SizedBox.shrink(),
            borderRadius: BorderRadius.circular(Radii.lg),
            onChanged: enabled ? (next) => onChanged(next ?? '') : null,
            items: [
              DropdownMenuItem(
                value: '',
                child: Text(
                  i18n.t('knowledge:personal'),
                  overflow: TextOverflow.ellipsis,
                  style: style,
                ),
              ),
              for (final project in projects)
                DropdownMenuItem(
                  value: project.id,
                  child: Text(
                    project.name,
                    overflow: TextOverflow.ellipsis,
                    style: style,
                  ),
                ),
            ],
          ),
        ),
      ],
    );
  }
}

/// Confirm forgetting one memory (web `ForgetDialog`). By default only the
/// memory itself goes; ticking the box also clears the original wording it
/// was learned from — never the chat, the session or the project. Resolves
/// true once forgotten.
Future<bool> showForgetDialog(
  BuildContext context,
  MemoryRecord memory,
) async =>
    await showDialog<bool>(
      context: context,
      builder: (_) => _ForgetDialog(memory: memory),
    ) ??
    false;

class _ForgetDialog extends ConsumerStatefulWidget {
  const _ForgetDialog({required this.memory});

  final MemoryRecord memory;

  @override
  ConsumerState<_ForgetDialog> createState() => _ForgetDialogState();
}

class _ForgetDialogState extends ConsumerState<_ForgetDialog> {
  bool _clearSources = false;
  bool _pending = false;
  String? _error;
  String? _requestId;

  Future<void> _confirm(List<String> sourceIds) async {
    final api = ref.read(knowledgeApiProvider);
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    final invalidate = ProviderScope.containerOf(
      context,
      listen: false,
    ).invalidate;
    final requestId = _requestId ??= newRequestId();
    setState(() {
      _pending = true;
      _error = null;
    });
    try {
      await api.forget(
        widget.memory,
        requestId,
        sourceIds: _clearSources ? sourceIds : null,
      );
      _requestId = null;
      toast.success(i18n.t('knowledge:forget.done'));
      invalidateKnowledge(invalidate);
      if (mounted) Navigator.of(context).pop(true);
    } catch (error) {
      if (_answered(error)) _requestId = null;
      if (apiErrorOf(error)?.status == 409) invalidateKnowledge(invalidate);
      if (mounted) setState(() => _error = memoryWriteError(i18n, error));
    } finally {
      if (mounted) setState(() => _pending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final sources =
        ref.watch(memorySourcesProvider(widget.memory.id)).current ??
        const <MemorySource>[];
    final sourceIds = [for (final source in sources) source.id];
    return PopScope(
      canPop: !_pending,
      child: AlertDialog(
        title: Text(
          i18n.t('knowledge:forget.title'),
          style: const TextStyle(fontSize: FontSizes.lg),
        ),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Container(
                constraints: const BoxConstraints(maxHeight: 160),
                padding: const EdgeInsets.symmetric(
                  horizontal: 12,
                  vertical: 9,
                ),
                decoration: BoxDecoration(
                  color: t.hairSoft,
                  borderRadius: BorderRadius.circular(Radii.md),
                ),
                child: SingleChildScrollView(
                  child: Text(
                    widget.memory.summary,
                    style: TextStyle(
                      fontSize: FontSizes.sm,
                      height: 1.6,
                      color: t.ink,
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 12),
              Text(
                i18n.t('knowledge:forget.body'),
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  height: 1.6,
                  color: t.n700,
                ),
              ),
              if (sourceIds.isNotEmpty) ...[
                LabeledCheckbox(
                  key: const ValueKey('forget-clear-sources'),
                  value: _clearSources,
                  label: i18n.t(
                    'knowledge:forget.clearSources',
                    vars: {'count': sourceIds.length},
                  ),
                  onChanged: (value) {
                    if (!_pending) setState(() => _clearSources = value);
                  },
                ),
                if (_clearSources)
                  Padding(
                    padding: const EdgeInsets.only(left: 28),
                    child: Text(
                      i18n.t('knowledge:forget.clearSourcesHint'),
                      style: TextStyle(
                        fontSize: FontSizes.xs,
                        height: 1.5,
                        color: t.n600,
                      ),
                    ),
                  ),
              ],
              if (_error != null) ...[
                const SizedBox(height: 12),
                ErrorNotice(text: _error!),
              ],
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: _pending ? null : () => Navigator.of(context).pop(false),
            child: Text(i18n.t('knowledge:forget.cancel')),
          ),
          TextButton(
            key: const ValueKey('forget-confirm'),
            onPressed: _pending || (_clearSources && sourceIds.isEmpty)
                ? null
                : () => _confirm(sourceIds),
            child: Text(
              i18n.t(
                _pending
                    ? 'knowledge:forget.forgetting'
                    : 'knowledge:forget.confirm',
              ),
              style: TextStyle(color: t.dangerInk),
            ),
          ),
        ],
      ),
    );
  }
}
