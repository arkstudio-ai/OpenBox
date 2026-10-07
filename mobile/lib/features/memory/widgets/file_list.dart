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

const _tones = {
  'ready': StatusTone.ok,
  'searchable': StatusTone.ok,
  'indexing': StatusTone.muted,
  'index_failed': StatusTone.warn,
  'failed': StatusTone.danger,
};

/// The upload target (web `FileDropzone`). A phone has nothing to drag, so
/// it is a generous button that opens the file picker.
class UploadCard extends ConsumerWidget {
  const UploadCard({
    super.key,
    required this.enabled,
    required this.onChoose,
    this.compact = false,
  });

  final bool enabled;
  final VoidCallback onChoose;
  final bool compact;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Opacity(
      opacity: enabled ? 1 : 0.6,
      child: Material(
        key: const ValueKey('knowledge-upload-card'),
        color: t.card.withValues(alpha: 0.6),
        shape: RoundedRectangleBorder(
          side: BorderSide(color: t.n400),
          borderRadius: BorderRadius.circular(Radii.xl),
        ),
        child: InkWell(
          customBorder: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(Radii.xl),
          ),
          onTap: enabled ? onChoose : null,
          child: Padding(
            padding: EdgeInsets.symmetric(
              horizontal: 20,
              vertical: compact ? 18 : 28,
            ),
            child: Column(
              children: [
                Icon(
                  Icons.cloud_upload_outlined,
                  size: compact ? 22 : 28,
                  color: t.a700,
                ),
                const SizedBox(height: 6),
                Text(
                  i18n.t('knowledge:uploadFile'),
                  style: TextStyle(
                    fontSize: FontSizes.base,
                    fontWeight: FontWeight.w500,
                    color: t.ink,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  i18n.t(
                    enabled
                        ? 'knowledge:file.dropHint'
                        : 'knowledge:file.unavailable',
                  ),
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    fontSize: FontSizes.xs,
                    height: 1.6,
                    color: t.n600,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Uploaded files (web `FileList`): what each is called, how far organizing
/// has come, and reading, retrying, downloading or deleting it.
class FileList extends ConsumerStatefulWidget {
  const FileList({
    super.key,
    required this.files,
    required this.query,
    required this.onRead,
  });

  final List<KnowledgeDocument> files;
  final String query;
  final ValueChanged<String> onRead;

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

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return KnowledgeCard(
      child: Column(
        children: [
          for (var i = 0; i < widget.files.length; i++) ...[
            if (i > 0) Divider(height: 1, thickness: 1, color: t.hair),
            _FileRow(
              key: ValueKey('file-row-${widget.files[i].id}'),
              file: widget.files[i],
              query: widget.query,
              retrying: _retrying,
              onRead: widget.onRead,
              onRetry: _retry,
              onDownload: _download,
              onDelete: _delete,
            ),
          ],
        ],
      ),
    );
  }
}

class _FileRow extends ConsumerWidget {
  const _FileRow({
    super.key,
    required this.file,
    required this.query,
    required this.retrying,
    required this.onRead,
    required this.onRetry,
    required this.onDownload,
    required this.onDelete,
  });

  final KnowledgeDocument file;
  final String query;
  final bool retrying;
  final ValueChanged<String> onRead;
  final ValueChanged<KnowledgeDocument> onRetry;
  final ValueChanged<KnowledgeDocument> onDownload;
  final ValueChanged<KnowledgeDocument> onDelete;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final meta = TextStyle(fontSize: FontSizes.xs, color: t.n600);
    final created = file.createdAt;
    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 12, 8, 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              IconTile(
                icon: Icons.description_outlined,
                background: t.hairSoft,
                foreground: t.n700,
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    HighlightText(
                      file.filename,
                      query: query,
                      maxLines: 2,
                      style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                    ),
                    const SizedBox(height: 4),
                    Wrap(
                      spacing: 8,
                      runSpacing: 4,
                      crossAxisAlignment: WrapCrossAlignment.center,
                      children: [
                        KnowledgeStatusPill(
                          label: tOr(
                            i18n,
                            'knowledge:file.status.${file.status}',
                            'knowledge:file.status.pending',
                          ),
                          tone: _tones[file.status] ?? StatusTone.warn,
                        ),
                        if ((file.bytes ?? 0) > 0)
                          Text(formatBytes(file.bytes!), style: meta),
                        if (created != null)
                          Text(
                            formatSince(created, i18n.language),
                            style: meta,
                          ),
                      ],
                    ),
                    if (file.reasonCode != null)
                      Padding(
                        padding: const EdgeInsets.only(top: 4),
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
                ),
              ),
            ],
          ),
          // On a phone the name takes the row beside its icon and the
          // actions sit underneath, rather than squeezing it.
          Padding(
            padding: const EdgeInsets.only(left: 48, top: 6),
            child: Wrap(
              spacing: 4,
              runSpacing: 4,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                if (file.pageIds.isNotEmpty)
                  KnowledgeButton(
                    key: ValueKey('file-read-${file.id}'),
                    compact: true,
                    label: i18n.t('knowledge:file.read'),
                    onPressed: () => onRead(file.pageIds.first),
                  ),
                if (file.failed)
                  KnowledgeButton(
                    key: ValueKey('file-retry-${file.id}'),
                    compact: true,
                    icon: Icons.refresh,
                    label: i18n.t('knowledge:file.retry'),
                    onPressed: retrying ? null : () => onRetry(file),
                  ),
                IconButton(
                  key: ValueKey('file-download-${file.id}'),
                  tooltip: i18n.t('knowledge:file.download'),
                  visualDensity: VisualDensity.compact,
                  icon: Icon(Icons.download_outlined, size: 18, color: t.n600),
                  onPressed: () => onDownload(file),
                ),
                IconButton(
                  key: ValueKey('file-delete-${file.id}'),
                  tooltip: i18n.t('knowledge:file.delete'),
                  visualDensity: VisualDensity.compact,
                  icon: Icon(Icons.delete_outline, size: 18, color: t.n600),
                  onPressed: () => onDelete(file),
                ),
              ],
            ),
          ),
        ],
      ),
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
