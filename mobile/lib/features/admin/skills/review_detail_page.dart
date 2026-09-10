import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/download/native_download.dart';
import '../../../shared/utils/error_text.dart';
import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../state/admin_load_state.dart';
import '../widgets/admin_confirm.dart';
import '../widgets/admin_widgets.dart';

class AdminReviewDetailPage extends ConsumerStatefulWidget {
  const AdminReviewDetailPage({super.key, required this.catalogId});
  final String catalogId;
  @override
  ConsumerState<AdminReviewDetailPage> createState() => _DetailState();
}

class _DetailState extends AdminLoadState<AdminRecord, AdminReviewDetailPage> {
  CancelToken? _download;
  Object? _downloadError;
  @override
  Future<AdminRecord> fetch(CancelToken cancel) =>
      api.reviewDetail(widget.catalogId, cancel);
  @override
  void dispose() {
    _download?.cancel('Review closed');
    super.dispose();
  }

  Future<void> _archive() async {
    if (_download != null) return;
    final cancel = CancelToken();
    final scopedApi = api;
    final downloader = ref.read(nativeDownloadProvider);
    setState(() {
      _download = cancel;
      _downloadError = null;
    });
    try {
      final stream = await scopedApi.reviewArchive(widget.catalogId, cancel);
      if (!mounted || cancel.isCancelled) return;
      await downloader.saveStream(
        stream: stream,
        suggestedName: '${data?.string('name') ?? 'skill'}.zip',
        checkAccess: () {
          scopedApi.checkAccess();
          if (cancel.isCancelled) throw cancel.cancelError!;
          if (!mounted) throw StateError('Review closed');
        },
      );
    } catch (error) {
      if (mounted && !cancel.isCancelled) {
        setState(() => _downloadError = error);
      }
    } finally {
      cancel.cancel('Download finished');
      if (mounted) setState(() => _download = null);
    }
  }

  Future<void> _verdict(String action) async {
    final i = i18n;
    final changed = await confirmAdminAction(
      context,
      title: i.t(
        'admin-skills:dialog.$action.title',
        vars: {'title': data?.string('title') ?? widget.catalogId},
      ),
      body: i.t('admin-skills:dialog.$action.body'),
      confirm: i.t('admin-skills:action.$action'),
      showNote: action != 'approve',
      requireReason: action == 'reject' || action == 'delist',
      run: (note, cancel) async {
        if (action == 'approve' || action == 'reject') {
          await api.reviewSkill(
            widget.catalogId,
            action == 'approve',
            note,
            cancel,
          );
        } else {
          await api.setListing(
            widget.catalogId,
            action == 'list' ? 'listed' : 'delisted',
            note,
            cancel,
          );
        }
      },
    );
    if (changed && mounted) {
      ref.read(adminSkillsChangedProvider)();
      await reload();
    }
  }

  @override
  Widget build(BuildContext context) {
    final i = i18n;
    return Scaffold(
      appBar: AppBar(title: Text(i.t('admin-skills:tab.review'))),
      body: loadable((row) {
        final files = row.records('files');
        final listing = row.string('listing');
        final archiveError = row.string('archive_error');
        return AdminList(
          onRefresh: reload,
          children: [
            AdminCard(
              title: row.string('title'),
              subtitle: widget.catalogId,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Align(
                    alignment: Alignment.centerLeft,
                    child: AdminPill(
                      adminLabel(i, 'admin-skills:status', listing),
                      status: listing,
                    ),
                  ),
                  for (final entry in {
                    'author': row.record('author').string('username', '—'),
                    'version': row.string('version', '—'),
                    'submittedAt': adminDate(
                      row.string('published_at'),
                      i.language,
                    ),
                    'size': row.data['size']?.toString() ?? '—',
                    'requiresMcp': row.strings('requires_mcp').join(', '),
                    'sha256': row.string('sha256'),
                  }.entries)
                    AdminField(
                      i.t('admin-skills:review.meta.${entry.key}'),
                      entry.value,
                    ),
                  if (row.string('listing_note').isNotEmpty)
                    AdminField(
                      i.t('admin-skills:dialog.note'),
                      row.string('listing_note'),
                    ),
                  OutlinedButton.icon(
                    onPressed: _download != null ? null : _archive,
                    icon: _download != null
                        ? const SizedBox.square(
                            dimension: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.download),
                    label: Text(i.t('admin-skills:action.download')),
                  ),
                  if (_downloadError != null)
                    Text(
                      errorText(i, _downloadError!),
                      style: TextStyle(color: context.tokens.danger),
                    ),
                ],
              ),
            ),
            if (archiveError.isNotEmpty)
              AdminCard(
                child: Text(
                  '${i.t('admin-skills:review.archiveRefused')}\n$archiveError',
                  style: TextStyle(color: context.tokens.danger),
                ),
              )
            else ...[
              AdminCard(
                title: i.t('admin-skills:review.skillMd'),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Text(i.t('admin-skills:review.skillMdNotice')),
                    const SizedBox(height: 12),
                    if (row.string('skill_md_error').isNotEmpty)
                      Text(
                        row.string('skill_md_error'),
                        style: TextStyle(color: context.tokens.danger),
                      ),
                    ConstrainedBox(
                      constraints: const BoxConstraints(maxHeight: 360),
                      child: SingleChildScrollView(
                        child: SelectableText(
                          row.string('skill_md').isEmpty
                              ? i.t('admin-skills:review.skillMdEmpty')
                              : row.string('skill_md'),
                        ),
                      ),
                    ),
                    if (row.flag('skill_md_truncated'))
                      Text(i.t('admin-skills:review.truncated')),
                  ],
                ),
              ),
              AdminCard(
                title: i.t(
                  'admin-skills:review.files',
                  count: row.integer('files_total', files.length),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    ConstrainedBox(
                      constraints: const BoxConstraints(maxHeight: 280),
                      child: SingleChildScrollView(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            for (final file in files)
                              AdminField(
                                file.string('path'),
                                file.data['size']?.toString() ?? '—',
                              ),
                          ],
                        ),
                      ),
                    ),
                    if (row.flag('files_truncated'))
                      Text(i.t('admin-skills:review.filesTruncated')),
                  ],
                ),
              ),
            ],
          ],
        );
      }),
      bottomNavigationBar: data == null
          ? null
          : AdminActionBar(
              secondary: data!.string('listing') == 'pending'
                  ? OutlinedButton(
                      onPressed: loading ? null : () => _verdict('reject'),
                      child: Text(i.t('admin-skills:action.reject')),
                    )
                  : null,
              primary: FilledButton(
                onPressed: loading
                    ? null
                    : () => _verdict(
                        data!.string('listing') == 'pending'
                            ? 'approve'
                            : data!.string('listing') == 'listed'
                            ? 'delist'
                            : 'list',
                      ),
                child: Text(
                  i.t(
                    'admin-skills:action.${data!.string('listing') == 'pending'
                        ? 'approve'
                        : data!.string('listing') == 'listed'
                        ? 'delist'
                        : 'list'}',
                  ),
                ),
              ),
            ),
    );
  }
}
