import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/download/native_download.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/utils/format.dart';
import '../../../shared/widgets/toast.dart';
import '../api/knowledge_api.dart';
import '../models/wiki_models.dart';
import '../state/knowledge_providers.dart';
import '../utils/knowledge_text.dart';
import 'knowledge_parts.dart';
import 'knowledge_sheets.dart';

/// The upload target (web `FileDropzone`). A phone has nothing to drag, so
/// it is one row that opens the file picker, saying what it accepts.
class UploadRow extends ConsumerWidget {
  const UploadRow({super.key, required this.enabled, required this.onChoose});

  final bool enabled;
  final VoidCallback onChoose;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return KnowledgeGroup(
      children: [
        Semantics(
          button: true,
          enabled: enabled,
          child: InkWell(
            key: const ValueKey('knowledge-upload-card'),
            onTap: enabled ? onChoose : null,
            child: Opacity(
              opacity: enabled ? 1 : 0.6,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(14, 12, 16, 12),
                child: Row(
                  children: [
                    IconTile(
                      icon: Icons.upload_file_outlined,
                      size: 34,
                      background: t.a100,
                      foreground: t.a700,
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            i18n.t('knowledge:uploadFile'),
                            style: TextStyle(
                              fontSize: FontSizes.base,
                              fontWeight: FontWeight.w500,
                              color: t.ink,
                            ),
                          ),
                          const SizedBox(height: 2),
                          Text(
                            i18n.t(
                              enabled
                                  ? 'knowledge:file.dropHint'
                                  : 'knowledge:file.unavailable',
                            ),
                            style: TextStyle(
                              fontSize: FontSizes.xs,
                              height: 1.5,
                              color: t.n600,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }
}

enum _FileAction { read, download, retry, delete }

/// Uploaded files (web `FileList`): what each is called and how far
/// organizing has come. A readable file opens on a tap; reading, retrying,
/// downloading and deleting sit behind its "more" button.
class FileList extends ConsumerStatefulWidget {
  const FileList({
    super.key,
    required this.files,
    required this.query,
    required this.onRead,
    this.footer,
  });

  final List<KnowledgeDocument> files;
  final String query;
  final ValueChanged<String> onRead;

  /// A last row, such as "load more".
  final Widget? footer;

  @override
  ConsumerState<FileList> createState() => _FileListState();
}

class _FileListState extends ConsumerState<FileList> {
  bool _retrying = false;

  void _error(Object error) {
    if (!mounted) return;
    ref
        .read(toastProvider.notifier)
        .error(errorText(ref.read(i18nProvider), error));
  }

  Future<void> _retry(KnowledgeDocument file) async {
    setState(() => _retrying = true);
    try {
      await ref.read(knowledgeApiProvider).retryDocument(file.id);
      if (mounted) ref.invalidate(knowledgeDocumentsProvider);
    } catch (error) {
      _error(error);
    } finally {
      if (mounted) setState(() => _retrying = false);
    }
  }

  Future<void> _download(KnowledgeDocument file) async {
    final api = ref.read(knowledgeApiProvider);
    final downloads = ref.read(nativeDownloadProvider);
    try {
      final original = await api.original(file.id);
      await downloads.saveBytes(
        bytes: original.bytes,
        suggestedName: original.filename ?? file.filename,
        mimeType: 'application/octet-stream',
      );
    } catch (error) {
      _error(error);
    }
  }

  Future<void> _delete(KnowledgeDocument file) async {
    final pending = await showDialog<bool>(
      context: context,
      builder: (_) => _DeleteFileDialog(file: file),
    );
    if (pending == null || !mounted) return;
    final i18n = ref.read(i18nProvider);
    ref
        .read(toastProvider.notifier)
        .success(
          i18n.t(
            pending
                ? 'knowledge:file.deletedPending'
                : 'knowledge:file.deleted',
            vars: {'name': file.filename},
          ),
        );
    // Pages built from the file go too; everything in this scope reads again.
    refreshKnowledge(ref);
  }

  Future<void> _actions(KnowledgeDocument file) async {
    final i18n = ref.read(i18nProvider);
    final action = await showActionSheet<_FileAction>(
      context,
      header: _FileSheetHeader(file: file),
      actions: [
        if (file.pageIds.isNotEmpty)
          SheetAction(
            key: ValueKey('file-read-${file.id}'),
            value: _FileAction.read,
            label: i18n.t('knowledge:file.read'),
            icon: Icons.menu_book_outlined,
          ),
        SheetAction(
          key: ValueKey('file-download-${file.id}'),
          value: _FileAction.download,
          label: i18n.t('knowledge:file.download'),
          icon: Icons.download_outlined,
        ),
        if (file.failed)
          SheetAction(
            key: ValueKey('file-retry-${file.id}'),
            value: _FileAction.retry,
            label: i18n.t('knowledge:file.retry'),
            icon: Icons.refresh,
            enabled: !_retrying,
          ),
        SheetAction(
          key: ValueKey('file-delete-${file.id}'),
          value: _FileAction.delete,
          label: i18n.t('knowledge:file.delete'),
          icon: Icons.delete_outline,
          danger: true,
        ),
      ],
    );
    if (!mounted) return;
    switch (action) {
      case _FileAction.read:
        widget.onRead(file.pageIds.first);
      case _FileAction.download:
        await _download(file);
      case _FileAction.retry:
        await _retry(file);
      case _FileAction.delete:
        await _delete(file);
      case null:
        break;
    }
  }

  @override
  Widget build(BuildContext context) => KnowledgeGroup(
    dividerIndent: 62,
    children: [
      for (final file in widget.files)
        _FileRow(
          key: ValueKey('file-row-${file.id}'),
          file: file,
          query: widget.query,
          // Reading is the point of a file; anything else is one tap away.
          onTap: file.pageIds.isNotEmpty
              ? () => widget.onRead(file.pageIds.first)
              : () => _actions(file),
          onMore: () => _actions(file),
        ),
      ?widget.footer,
    ],
  );
}

/// What a file's status looks like: the words, their colour and a dot for
/// anything still moving or stuck.
({String label, Color color, Color? dot}) _status(
  I18nState i18n,
  BossipTokens t,
  KnowledgeDocument file,
) {
  final label = tOr(
    i18n,
    'knowledge:file.status.${file.status}',
    'knowledge:file.status.pending',
  );
  if (file.status == 'failed') {
    return (label: label, color: t.dangerInk, dot: t.dangerInk);
  }
  if (file.status == 'index_failed') {
    return (label: label, color: t.n600, dot: t.accent);
  }
  if (const {'ready', 'searchable'}.contains(file.status)) {
    return (label: label, color: t.n600, dot: null);
  }
  return (label: label, color: t.n600, dot: t.n500);
}

/// status · size · when, on one line.
TextSpan _metaLine(I18nState i18n, BossipTokens t, KnowledgeDocument file) {
  final status = _status(i18n, t, file);
  final created = file.createdAt;
  final rest = [
    if ((file.bytes ?? 0) > 0) formatBytes(file.bytes!),
    if (created != null) formatSince(created, i18n.language),
  ];
  return TextSpan(
    children: [
      if (status.dot != null) statusDot(status.dot!),
      TextSpan(
        text: status.label,
        style: TextStyle(color: status.color),
      ),
      if (rest.isNotEmpty) TextSpan(text: ' · ${rest.join(' · ')}'),
    ],
  );
}

class _FileRow extends ConsumerWidget {
  const _FileRow({
    super.key,
    required this.file,
    required this.query,
    required this.onTap,
    required this.onMore,
  });

  final KnowledgeDocument file;
  final String query;
  final VoidCallback onTap;
  final VoidCallback onMore;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 10, 4, 10),
        child: Row(
          children: [
            IconTile(
              icon: Icons.description_outlined,
              size: 34,
              background: t.hairSoft,
              foreground: t.n700,
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  HighlightText(
                    file.filename,
                    query: query,
                    maxLines: 1,
                    style: TextStyle(
                      fontSize: FontSizes.base,
                      height: 1.45,
                      color: t.ink,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text.rich(
                    _metaLine(i18n, t, file),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                  ),
                ],
              ),
            ),
            IconButton(
              key: ValueKey('file-more-${file.id}'),
              tooltip: i18n.t('common:action.more'),
              icon: Icon(Icons.more_horiz, size: 20, color: t.n600),
              onPressed: onMore,
            ),
          ],
        ),
      ),
    );
  }
}

/// The file the sheet's actions apply to, and why organizing it stopped.
class _FileSheetHeader extends ConsumerWidget {
  const _FileSheetHeader({required this.file});

  final KnowledgeDocument file;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          file.filename,
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(
            fontSize: FontSizes.base,
            height: 1.45,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
        const SizedBox(height: 2),
        Text.rich(
          _metaLine(i18n, t, file),
          style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
        ),
        if (file.reasonCode != null)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text(
              documentProblem(i18n, file.reasonCode!),
              style: TextStyle(
                fontSize: FontSizes.xs,
                height: 1.5,
                color: t.dangerInk,
              ),
            ),
          ),
      ],
    );
  }
}

/// Confirms deleting one file. Pops with whether its original is still being
/// removed from storage, or null when cancelled.
class _DeleteFileDialog extends ConsumerStatefulWidget {
  const _DeleteFileDialog({required this.file});

  final KnowledgeDocument file;

  @override
  ConsumerState<_DeleteFileDialog> createState() => _DeleteFileDialogState();
}

class _DeleteFileDialogState extends ConsumerState<_DeleteFileDialog> {
  bool _pending = false;
  String? _error;

  Future<void> _confirm() async {
    setState(() {
      _pending = true;
      _error = null;
    });
    try {
      final pending = await ref
          .read(knowledgeApiProvider)
          .removeDocument(widget.file.id);
      if (mounted) Navigator.of(context).pop(pending);
    } catch (error) {
      if (mounted) {
        setState(() => _error = errorText(ref.read(i18nProvider), error));
      }
    } finally {
      if (mounted) setState(() => _pending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return PopScope(
      canPop: !_pending,
      child: AlertDialog(
        title: Text(
          i18n.t('knowledge:file.deleteTitle'),
          style: const TextStyle(fontSize: FontSizes.lg),
        ),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 12,
                  vertical: 9,
                ),
                decoration: BoxDecoration(
                  color: t.hairSoft,
                  borderRadius: BorderRadius.circular(Radii.md),
                ),
                child: Text(
                  widget.file.filename,
                  style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
                ),
              ),
              const SizedBox(height: 12),
              Text(
                i18n.t('knowledge:file.deleteBody'),
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  height: 1.6,
                  color: t.n700,
                ),
              ),
              if (_error != null) ...[
                const SizedBox(height: 12),
                ErrorNotice(text: _error!),
              ],
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: _pending ? null : () => Navigator.of(context).pop(),
            child: Text(i18n.t('knowledge:file.cancel')),
          ),
          TextButton(
            key: const ValueKey('file-delete-confirm'),
            onPressed: _pending ? null : _confirm,
            child: Text(
              i18n.t(
                _pending
                    ? 'knowledge:file.deleting'
                    : 'knowledge:file.deleteConfirm',
              ),
              style: TextStyle(color: t.dangerInk),
            ),
          ),
        ],
      ),
    );
  }
}
