import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/utils/format.dart';
import '../../../shared/widgets/toast.dart';
import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../state/admin_load_state.dart';
import '../widgets/admin_confirm.dart';
import '../widgets/admin_widgets.dart';
import 'announcement_editor_page.dart';

/// 消息通知 › 公告 — the list plus publish / revoke / preview; the editor is
/// pushed above the console. A freshly published row polls until the server
/// stamps `fanoutAt`.
class AdminAnnouncementsPage extends ConsumerStatefulWidget {
  const AdminAnnouncementsPage({super.key, this.active = true});
  final bool active;
  @override
  ConsumerState<AdminAnnouncementsPage> createState() => _AnnouncementsState();
}

class _AnnouncementsState
    extends AdminLoadState<AdminPage, AdminAnnouncementsPage> {
  Timer? _poll;
  bool _previewing = false;

  @override
  void initState() {
    super.initState();
    _poll = Timer.periodic(const Duration(seconds: 3), (_) {
      if (!mounted || !widget.active || loading) return;
      final fanningOut =
          data?.items.any(
            (item) =>
                item.string('status') == 'published' &&
                item.string('fanoutAt').isEmpty,
          ) ??
          false;
      if (fanningOut) unawaited(reload());
    });
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  @override
  void didUpdateWidget(AdminAnnouncementsPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.active && !oldWidget.active) unawaited(reload());
  }

  @override
  Future<AdminPage> fetch(CancelToken cancel) => api.announcements(cancel);

  Future<void> _edit([AdminRecord? source]) async {
    final saved = await Navigator.push<bool>(
      context,
      MaterialPageRoute(
        builder: (_) => AdminAnnouncementEditorPage(source: source),
      ),
    );
    if (saved == true && mounted) await reload();
  }

  Future<void> _publish(AdminRecord row) async {
    final i = i18n;
    final id = row.string('id');
    var count = row.integer('recipientCount');
    try {
      count = (await api.announcement(id)).integer('recipientCount');
    } catch (error) {
      if (mounted) {
        ref.read(toastProvider.notifier).error(errorText(i, error));
      }
      return;
    }
    if (!mounted) return;
    final confirmed = await confirmAdminAction(
      context,
      title: i.t('admin-messages:action.publishConfirmTitle'),
      body: i.t(
        'admin-messages:action.publishConfirmBody',
        vars: {'count': count},
      ),
      confirm: i.t('admin-messages:action.publish'),
      run: (_, cancel) => api.publishAnnouncement(id, cancel),
    );
    if (confirmed && mounted) await reload();
  }

  Future<void> _revoke(AdminRecord row) async {
    final i = i18n;
    final confirmed = await confirmAdminAction(
      context,
      title: i.t('admin-messages:action.revokeConfirmTitle'),
      body: i.t('admin-messages:action.revokeConfirmBody'),
      confirm: i.t('admin-messages:action.revoke'),
      run: (_, cancel) => api.revokeAnnouncement(row.string('id'), cancel),
    );
    if (confirmed && mounted) await reload();
  }

  Future<void> _preview(AdminRecord row) async {
    if (_previewing) return;
    setState(() => _previewing = true);
    final i = i18n;
    try {
      await api.previewAnnouncement(row.string('id'));
      if (mounted) {
        ref
            .read(toastProvider.notifier)
            .success(i.t('admin-messages:action.previewSent'));
      }
    } catch (error) {
      if (mounted) {
        ref.read(toastProvider.notifier).error(errorText(i, error));
      }
    } finally {
      if (mounted) setState(() => _previewing = false);
    }
  }

  String _audience(AdminRecord row) {
    final i = i18n;
    final audience = row.record('audience');
    switch (audience.string('kind')) {
      case 'role':
        return i.t(
          'admin-messages:list.audience.role',
          vars: {
            'role': i.t(
              audience.string('role') == 'admin'
                  ? 'admin-messages:form.roleAdmin'
                  : 'admin-messages:form.roleUser',
            ),
          },
        );
      case 'workspace':
        return i.t(
          'admin-messages:list.audience.workspace',
          vars: {'id': audience.string('id')},
        );
      case 'users':
        return i.t(
          'admin-messages:list.audience.users',
          vars: {'count': audience.strings('ids').length},
        );
      default:
        return i.t('admin-messages:list.audience.all');
    }
  }

  String _meta(AdminRecord row) {
    final i = i18n;
    final parts = <String>[
      _audience(row),
      i.t(
        row.flag('push')
            ? 'admin-messages:list.push'
            : 'admin-messages:list.noPush',
      ),
    ];
    final status = row.string('status');
    final publishAt = DateTime.tryParse(row.string('publishAt'));
    if (status == 'scheduled' && publishAt != null) {
      parts.add(
        i.t(
          'admin-messages:list.publishAt',
          vars: {'time': formatDateTime(publishAt, i.language)},
        ),
      );
    }
    if (status == 'published') {
      parts.add(
        i.t(
          'admin-messages:list.fanout',
          vars: {'count': row.integer('fanoutCount')},
        ),
      );
    }
    final link = row.record('link');
    parts.add(switch (link.string('kind')) {
      'topic' => i.t(
        'admin-messages:list.link.topic',
        vars: {'slug': link.string('slug')},
      ),
      'url' => i.t('admin-messages:list.link.url'),
      _ => i.t('admin-messages:list.link.none'),
    });
    return parts.join(' · ');
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
                  i.t('admin-messages:subtitle'),
                  style: TextStyle(color: t.n600, fontSize: FontSizes.sm),
                ),
              ),
              IconButton(
                onPressed: loading ? null : reload,
                tooltip: i.t('admin-messages:common.refresh'),
                icon: const Icon(Icons.refresh),
              ),
              FilledButton.tonal(
                key: const ValueKey('announcement-create'),
                onPressed: () => _edit(),
                child: Text(i.t('admin-messages:list.create')),
              ),
            ],
          ),
        ),
        Expanded(
          child: loadable(
            (page) => AdminList(
              onRefresh: reload,
              storageKey: 'admin-announcements',
              children: [
                if (page.items.isEmpty)
                  AdminCard(child: Text(i.t('admin-messages:list.empty'))),
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
    final status = row.string('status');
    final editable = status == 'draft' || status == 'scheduled';
    final scheduled =
        DateTime.tryParse(row.string('publishAt'))?.isAfter(DateTime.now()) ??
        false;
    return AdminRecordTile(
      key: ValueKey('announcement-${row.string('id')}'),
      title: row.string('title'),
      subtitle: row.string('body'),
      trailing: AdminPill(
        i.t('admin-messages:status.$status'),
        status: status == 'published'
            ? 'ok'
            : status == 'revoked'
            ? 'cancelled'
            : status,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            _meta(row),
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
              if (editable)
                OutlinedButton(
                  onPressed: () => _edit(row),
                  child: Text(i.t('admin-messages:action.edit')),
                ),
              if (editable)
                FilledButton(
                  key: ValueKey('announcement-publish-${row.string('id')}'),
                  onPressed: () => _publish(row),
                  child: Text(
                    i.t(
                      scheduled
                          ? 'admin-messages:action.schedule'
                          : 'admin-messages:action.publish',
                    ),
                  ),
                ),
              if (status == 'scheduled' || status == 'published')
                OutlinedButton(
                  key: ValueKey('announcement-revoke-${row.string('id')}'),
                  onPressed: () => _revoke(row),
                  child: Text(i.t('admin-messages:action.revoke')),
                ),
              OutlinedButton(
                onPressed: _previewing ? null : () => _preview(row),
                child: Text(i.t('admin-messages:action.preview')),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
