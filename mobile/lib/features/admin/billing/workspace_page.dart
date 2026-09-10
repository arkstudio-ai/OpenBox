import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/appearance/tokens.dart';
import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../state/admin_load_state.dart';
import '../widgets/admin_widgets.dart';
import 'billing_rows.dart';

class AdminWorkspacePage extends ConsumerStatefulWidget {
  const AdminWorkspacePage({super.key, required this.workspaceId});
  final String workspaceId;
  @override
  ConsumerState<AdminWorkspacePage> createState() => _WorkspaceState();
}

class _WorkspaceState extends AdminLoadState<AdminRecord, AdminWorkspacePage> {
  String _tab = 'summary';
  @override
  Future<AdminRecord> fetch(CancelToken cancel) =>
      api.workspaceBilling(widget.workspaceId, cancel);
  @override
  Widget build(BuildContext context) {
    final missing = loadError != null && apiErrorOf(loadError!)?.status == 404;
    return Scaffold(
      backgroundColor: context.tokens.bg,
      appBar: AppBar(
        title: Text(
          data?.record('workspace').string('name') ??
              i18n.t('admin-billing:subscriptions.columns.workspace'),
        ),
        actions: [
          IconButton(
            onPressed: loading ? null : reload,
            tooltip: i18n.t('admin:mobile.refresh'),
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: missing
          ? Center(child: Text(i18n.t('admin-billing:workspace.notFound')))
          : Column(
              children: [
                AdminTabs(
                  value: _tab,
                  onChanged: (tab) => setState(() => _tab = tab),
                  labels: {
                    'summary': i18n.t('admin-billing:tabs.subscriptions'),
                    'orders': i18n.t('admin-billing:workspace.orders.title'),
                    'ledger': i18n.t('admin-billing:workspace.ledger.title'),
                    'usage': i18n.t(
                      'admin-billing:workspace.usage.title',
                      vars: {
                        'days': data?.record('usage').integer('days', 30) ?? 30,
                      },
                    ),
                  },
                ),
                Expanded(
                  child: loadable(
                    (value) => AdminList(
                      storageKey: 'workspace-billing-$_tab',
                      onRefresh: reload,
                      children: switch (_tab) {
                        'orders' => [
                          AdminCard(
                            child: Text(
                              i18n.t(
                                'admin-billing:workspace.orders.hint',
                                vars: {'value': 100},
                              ),
                            ),
                          ),
                          if (value.records('orders').isEmpty)
                            AdminCard(
                              child: Text(
                                i18n.t('admin-billing:workspace.orders.empty'),
                              ),
                            ),
                          for (final row in value.records('orders'))
                            AdminOrderCard(order: row, allowWorkspace: false),
                        ],
                        'ledger' => [
                          AdminCard(
                            child: Text(
                              i18n.t(
                                'admin-billing:workspace.ledger.hint',
                                vars: {'value': 50},
                              ),
                            ),
                          ),
                          if (value.records('ledger').isEmpty)
                            AdminCard(
                              child: Text(
                                i18n.t('admin-billing:workspace.ledger.empty'),
                              ),
                            ),
                          for (final row in value.records('ledger'))
                            AdminRecordTile(
                              fullText: true,
                              title: row.string('amount', '—'),
                              subtitle: adminDate(
                                row.string('created_at'),
                                i18n.language,
                              ),
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.stretch,
                                children: [
                                  AdminField(
                                    i18n.t(
                                      'admin-billing:workspace.ledger.columns.kind',
                                    ),
                                    row.string('kind'),
                                  ),
                                  AdminField(
                                    i18n.t(
                                      'admin-billing:workspace.ledger.columns.balanceAfter',
                                    ),
                                    row.string('balance_after', '—'),
                                  ),
                                  AdminField(
                                    i18n.t(
                                      'admin-billing:workspace.ledger.columns.reference',
                                    ),
                                    row.string('reference_id'),
                                  ),
                                ],
                              ),
                            ),
                        ],
                        'usage' => _usage(value.record('usage')),
                        _ => _summary(value),
                      },
                    ),
                  ),
                ),
              ],
            ),
    );
  }

  List<Widget> _summary(AdminRecord value) {
    final workspace = value.record('workspace');
    final subscription = value.record('subscription');
    final queued = value.records('queued');
    return [
      AdminCard(
        title: workspace.string('name'),
        subtitle: workspace.string('id'),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                AdminPill(
                  adminLabel(
                    i18n,
                    'admin-billing:plans',
                    value.string('plan_id'),
                  ),
                ),
                if (workspace.flag('is_deleted'))
                  AdminPill(
                    i18n.t('admin-billing:workspace.deleted'),
                    status: 'deleted',
                  ),
              ],
            ),
            for (final pair in {
              'kind': workspace.string('kind'),
              'owner': adminUser(value.record('owner')),
              'members': value.integer('member_count').toString(),
              'balance': value.string('balance', '—'),
              'expires': adminDate(
                subscription.string('ends_at'),
                i18n.language,
              ),
              'queued': queued.length.toString(),
              'created': adminDate(
                workspace.string('created_at'),
                i18n.language,
              ),
              if (workspace.string('deleted_at').isNotEmpty)
                'deletedAt': adminDate(
                  workspace.string('deleted_at'),
                  i18n.language,
                ),
            }.entries)
              AdminField(
                i18n.t('admin-billing:workspace.fields.${pair.key}'),
                pair.value,
              ),
            const SizedBox(height: 8),
            Text(
              i18n.t('admin-billing:workspace.readOnly'),
              style: TextStyle(
                color: context.tokens.n600,
                fontSize: 12,
                height: 1.5,
              ),
            ),
          ],
        ),
      ),
      AdminCard(
        title: i18n.t('admin-billing:workspace.terms.title'),
        child: value.records('history').isEmpty
            ? Text(i18n.t('admin-billing:workspace.terms.empty'))
            : Column(
                children: [
                  for (final term in value.records('history'))
                    ExpansionTile(
                      tilePadding: EdgeInsets.zero,
                      title: Text(
                        '${adminLabel(i18n, 'admin-billing:plans', term.string('plan_id'))} · ${adminLabel(i18n, 'admin-billing:cycles', term.string('cycle'))}',
                      ),
                      subtitle: Text(
                        '${adminDate(term.string('starts_at'), i18n.language)} → ${adminDate(term.string('ends_at'), i18n.language)}',
                        style: const TextStyle(fontSize: 12),
                      ),
                      children: [
                        if (term.string('order_id') ==
                            subscription.string('order_id'))
                          AdminPill(
                            i18n.t('admin-billing:workspace.terms.current'),
                            status: 'active',
                          ),
                        if (queued.any(
                          (row) =>
                              row.string('order_id') == term.string('order_id'),
                        ))
                          AdminPill(
                            i18n.t('admin-billing:workspace.terms.queued'),
                          ),
                        AdminField(
                          i18n.t(
                            'admin-billing:workspace.terms.columns.orderId',
                          ),
                          term.string('order_id'),
                        ),
                      ],
                    ),
                ],
              ),
      ),
    ];
  }

  List<Widget> _usage(AdminRecord usage) => [
    AdminCard(
      child: Text(
        i18n.t(
          'admin-billing:workspace.usage.since',
          vars: {'time': adminDate(usage.string('since'), i18n.language)},
        ),
      ),
    ),
    if (usage.records('items').isEmpty)
      AdminCard(
        child: Text(
          i18n.t(
            'admin-billing:workspace.usage.empty',
            vars: {'days': usage.integer('days')},
          ),
        ),
      ),
    for (final row in usage.records('items'))
      AdminRecordTile(
        title: adminLabel(
          i18n,
          'admin-billing:usageStatus',
          row.string('status'),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              i18n.t(
                'admin-billing:workspace.usage.events',
                vars: {'value': row.integer('events')},
              ),
            ),
            AdminField(
              i18n.t('admin-billing:workspace.usage.tokens'),
              row.integer('total_tokens').toString(),
            ),
            AdminField(
              i18n.t('admin-billing:workspace.usage.credits'),
              row.string('credits', '—'),
            ),
          ],
        ),
      ),
  ];
}
