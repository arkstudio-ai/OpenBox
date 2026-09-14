import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gpt_markdown/gpt_markdown.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/config/env.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/format.dart';
import '../../../shared/widgets/toast.dart';
import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../state/admin_load_state.dart';
import '../widgets/admin_confirm.dart';
import '../widgets/admin_widgets.dart';

/// 消息通知 › 专题 — read, publish / unpublish and share. Bodies are edited
/// on the web console (决策 6): the phone only reviews and flips status.
class AdminTopicsPage extends ConsumerStatefulWidget {
  const AdminTopicsPage({super.key, this.active = true});
  final bool active;
  @override
  ConsumerState<AdminTopicsPage> createState() => _TopicsState();
}

class _TopicsState extends AdminLoadState<AdminPage, AdminTopicsPage> {
  @override
  void didUpdateWidget(AdminTopicsPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.active && !oldWidget.active) unawaited(reload());
  }

  @override
  Future<AdminPage> fetch(CancelToken cancel) => api.topics(cancel);

  Future<void> _flip(AdminRecord row) async {
    final i = i18n;
    final publishing = row.string('status') != 'published';
    final confirmed = await confirmAdminAction(
      context,
      title: i.t(
        publishing
            ? 'admin-messages:topics.publish'
            : 'admin-messages:topics.unpublish',
      ),
      body: row.string('title'),
      confirm: i.t(
        publishing
            ? 'admin-messages:topics.publish'
            : 'admin-messages:topics.unpublish',
      ),
      run: (_, cancel) =>
          api.setTopicPublished(row.string('id'), publishing, cancel),
    );
    if (confirmed && mounted) await reload();
  }

  Future<void> _copyLink(AdminRecord row) async {
    final i = i18n;
    await Clipboard.setData(
      ClipboardData(
        text:
            '${Env.webBase}/topics/${Uri.encodeComponent(row.string('slug'))}',
      ),
    );
    if (mounted) {
      ref
          .read(toastProvider.notifier)
          .success(i.t('admin-messages:topics.linkCopied'));
    }
  }

  void _view(AdminRecord row) {
    Navigator.push<void>(
      context,
      MaterialPageRoute(builder: (_) => _TopicPreviewPage(topic: row)),
    );
  }

  @override
  Widget build(BuildContext context) {
    final i = i18n;
    final t = context.tokens;
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 10, 8, 0),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  i.t('admin-messages:topics.webOnlyEdit'),
                  style: TextStyle(color: t.n600, fontSize: FontSizes.sm),
                ),
              ),
              IconButton(
                onPressed: loading ? null : reload,
                tooltip: i.t('admin-messages:common.refresh'),
                icon: const Icon(Icons.refresh),
              ),
            ],
          ),
        ),
        Expanded(
          child: loadable(
            (page) => AdminList(
              onRefresh: reload,
              storageKey: 'admin-topics',
              children: [
                if (page.items.isEmpty)
                  AdminCard(child: Text(i.t('admin-messages:topics.empty'))),
                for (final row in page.items) _tile(row),
              ],
            ),
          ),
        ),
      ],
    );
  }

  Widget _tile(AdminRecord row) {
    final i = i18n;
    final published = row.string('status') == 'published';
    final stamp = DateTime.tryParse(
      row.string(published ? 'publishedAt' : 'updatedAt'),
    );
    return AdminRecordTile(
      key: ValueKey('topic-${row.string('id')}'),
      title: row.string('title'),
      subtitle: '/topics/${row.string('slug')}',
      trailing: AdminPill(
        i.t('admin-messages:status.${row.string('status')}'),
        status: published ? 'ok' : 'draft',
      ),
      onTap: () => _view(row),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (stamp != null)
            Text(
              i.t(
                published
                    ? 'admin-messages:topics.publishedAt'
                    : 'admin-messages:topics.updatedAt',
                vars: {'time': formatDateTime(stamp, i.language)},
              ),
              style: TextStyle(
                color: context.tokens.n600,
                fontSize: FontSizes.xs,
              ),
            ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 4,
            children: [
              OutlinedButton(
                onPressed: () => _view(row),
                child: Text(i.t('admin-messages:topics.view')),
              ),
              FilledButton(
                key: ValueKey('topic-flip-${row.string('id')}'),
                onPressed: () => _flip(row),
                child: Text(
                  i.t(
                    published
                        ? 'admin-messages:topics.unpublish'
                        : 'admin-messages:topics.publish',
                  ),
                ),
              ),
              if (published)
                OutlinedButton(
                  onPressed: () => _copyLink(row),
                  child: Text(i.t('admin-messages:topics.copyLink')),
                ),
            ],
          ),
        ],
      ),
    );
  }
}

/// Read-only rendering of a topic as users will see it.
class _TopicPreviewPage extends ConsumerWidget {
  const _TopicPreviewPage({required this.topic});
  final AdminRecord topic;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i = ref.watch(i18nProvider);
    final t = context.tokens;
    final cover = topic.string('coverUrl');
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(title: Text(i.t('admin-messages:topics.preview'))),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(20, 8, 20, 40),
        children: [
          if (cover.isNotEmpty)
            ClipRRect(
              borderRadius: BorderRadius.circular(Radii.lg),
              child: Image.network(
                cover,
                fit: BoxFit.cover,
                errorBuilder: (_, _, _) => const SizedBox.shrink(),
              ),
            ),
          if (cover.isNotEmpty) const SizedBox(height: 16),
          Text(
            topic.string('title'),
            style: TextStyle(
              fontSize: FontSizes.xl,
              fontWeight: FontWeight.w600,
              height: 1.3,
              color: t.ink,
            ),
          ),
          const SizedBox(height: 14),
          GptMarkdown(
            topic.string('contentMd'),
            style: TextStyle(
              fontSize: FontSizes.lg,
              height: 1.78,
              color: t.ink,
            ),
          ),
          if (topic.string('ctaLabel').isNotEmpty) ...[
            const SizedBox(height: 24),
            AdminField(
              i.t('admin-messages:topics.ctaLabel'),
              topic.string('ctaLabel'),
            ),
          ],
        ],
      ),
    );
  }
}
