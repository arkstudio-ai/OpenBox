import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../state/admin_load_state.dart';
import '../widgets/admin_widgets.dart';
import 'review_detail_page.dart';

class AdminReviewPage extends ConsumerStatefulWidget {
  const AdminReviewPage({super.key, this.active = true});
  final bool active;
  @override
  ConsumerState<AdminReviewPage> createState() => _ReviewState();
}

class _ReviewState extends AdminLoadState<AdminPage, AdminReviewPage> {
  @override
  void didUpdateWidget(covariant AdminReviewPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.active && !oldWidget.active) {
      unawaited(reload());
    }
  }

  String _state = 'pending';
  int _offset = 0;
  @override
  Future<AdminPage> fetch(CancelToken cancel) => api.skillReviews({
    'state': _state,
    'offset': _offset,
    'limit': 20,
  }, cancel);
  Future<void> _open(String id) async {
    await Navigator.push<void>(
      context,
      MaterialPageRoute(builder: (_) => AdminReviewDetailPage(catalogId: id)),
    );
    if (!mounted) return;
    await reload();
    if (mounted && data != null && data!.items.isEmpty && _offset > 0) {
      setState(
        () => _offset = data!.total == 0 ? 0 : ((data!.total - 1) ~/ 20) * 20,
      );
      await reload(clear: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i = i18n;
    return Column(
      children: [
        AdminScopePicker(
          label: i.t('admin:mobile.reviewQueue'),
          options: {
            for (final state in ['pending', 'rejected'])
              state: i.t('admin-skills:review.state.$state'),
          },
          value: _state,
          onChanged: loading
              ? null
              : (state) {
                  setState(() {
                    _state = state;
                    _offset = 0;
                  });
                  unawaited(reload(clear: true));
                },
          trailing: IconButton(
            onPressed: loading ? null : reload,
            tooltip: i.t('admin:mobile.refresh'),
            icon: const Icon(Icons.refresh),
          ),
        ),
        Expanded(
          child: loadable(
            (page) => AdminList(
              onRefresh: reload,
              children: [
                if (page.items.isEmpty)
                  AdminCard(
                    child: Text(
                      i.t(
                        _state == 'pending'
                            ? 'admin-skills:review.emptyPending'
                            : 'admin-skills:review.emptyRejected',
                      ),
                    ),
                  ),
                for (final row in page.items)
                  AdminRecordTile(
                    title: row.string('title'),
                    subtitle: row.record('author').string('username', '—'),
                    leading: AdminIcon(row.string('icon')),
                    onTap: () => _open(row.string('catalog_id')),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        AdminPill(
                          adminLabel(
                            i,
                            'admin-skills:status',
                            row.string('listing'),
                          ),
                          status: row.string('listing'),
                        ),
                        AdminField(
                          i.t('admin-skills:review.meta.submittedAt'),
                          adminDate(row.string('published_at'), i.language),
                        ),
                        if (row.string('listing_note').isNotEmpty)
                          AdminField(
                            i.t('admin-skills:dialog.note'),
                            row.string('listing_note'),
                          ),
                      ],
                    ),
                  ),
                AdminPager(
                  total: page.total,
                  offset: _offset,
                  limit: 20,
                  disabled: loading,
                  onChanged: (offset) {
                    setState(() => _offset = offset);
                    unawaited(reload(clear: true));
                  },
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}
