import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../models/admin_data.dart';
import '../widgets/admin_widgets.dart';
import 'workspace_page.dart';

String adminUser(AdminRecord row) {
  final name = row.string('username', '—');
  final email = row.string('email');
  return email.isEmpty ? name : '$name\n$email';
}

class AdminSubscriptionCard extends ConsumerWidget {
  const AdminSubscriptionCard({super.key, required this.row, this.onTap});
  final AdminRecord row;
  final VoidCallback? onTap;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i = ref.watch(i18nProvider);
    final workspace = row.record('workspace');
    final owner = adminUser(row.record('owner'));
    return AdminRecordTile(
      title: workspace.string('name', workspace.string('id')),
      subtitle: owner == workspace.string('name') || owner == '—'
          ? null
          : owner,
      onTap: onTap,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              AdminPill(
                adminLabel(i, 'admin-billing:states', row.string('state')),
                status: row.string('state'),
              ),
              Text(
                [
                  adminLabel(i, 'admin-billing:plans', row.string('plan_id')),
                  if (row.string('cycle').isNotEmpty)
                    adminLabel(i, 'admin-billing:cycles', row.string('cycle')),
                ].join(' · '),
                style: TextStyle(
                  color: context.tokens.n600,
                  fontSize: FontSizes.sm,
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          AdminField(
            i.t('admin-billing:subscriptions.columns.balance'),
            row.string('balance', '—'),
          ),
          if (row.string('ends_at').isNotEmpty)
            AdminField(
              i.t('admin-billing:workspace.fields.expires'),
              adminDate(row.string('ends_at'), i.language),
            ),
          if (row.integer('queued_count') > 0)
            AdminField(
              i.t('admin-billing:subscriptions.columns.queued'),
              row.integer('queued_count').toString(),
            ),
          if (row.string('last_paid_at').isNotEmpty)
            AdminField(
              i.t('admin-billing:subscriptions.columns.lastPaid'),
              adminDate(row.string('last_paid_at'), i.language),
            ),
        ],
      ),
    );
  }
}

class AdminOrderCard extends ConsumerWidget {
  const AdminOrderCard({
    super.key,
    required this.order,
    this.allowWorkspace = true,
  });
  final AdminRecord order;
  final bool allowWorkspace;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i = ref.watch(i18nProvider);
    return AdminRecordTile(
      title: adminMoney(order),
      subtitle: order.string('workspace_name', order.string('workspace_id')),
      onTap: () => Navigator.push<void>(
        context,
        MaterialPageRoute(
          builder: (_) =>
              _OrderDetail(order: order, allowWorkspace: allowWorkspace),
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              AdminPill(
                adminLabel(
                  i,
                  'admin-billing:orderStatus',
                  order.string('status'),
                ),
                status: order.string('status'),
              ),
              Text(
                [
                  adminLabel(i, 'admin-billing:kinds', order.string('kind')),
                  order.string('provider'),
                ].where((part) => part.isNotEmpty).join(' · '),
                style: TextStyle(
                  color: context.tokens.n600,
                  fontSize: FontSizes.sm,
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Text(
            order.string('id'),
            style: TextStyle(color: context.tokens.n600, fontSize: 12),
          ),
          Text(
            adminDate(order.string('created_at'), i.language),
            style: TextStyle(color: context.tokens.n600, fontSize: 12),
          ),
        ],
      ),
    );
  }
}

class _OrderDetail extends ConsumerWidget {
  const _OrderDetail({required this.order, required this.allowWorkspace});
  final AdminRecord order;
  final bool allowWorkspace;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i = ref.watch(i18nProvider);
    final fields = {
      'id': order.string('id'),
      'workspace': order.string('workspace_name', order.string('workspace_id')),
      'user': adminUser(order.record('user')),
      'product': [
        adminLabel(i, 'admin-billing:kinds', order.string('kind')),
        if (order.string('plan_id').isNotEmpty)
          adminLabel(i, 'admin-billing:plans', order.string('plan_id')),
        if (order.string('cycle').isNotEmpty)
          adminLabel(i, 'admin-billing:cycles', order.string('cycle')),
      ].join(' · '),
      'amount': adminMoney(order),
      'credits': order.string('credits', '—'),
      'provider': order.string('provider'),
      'providerOrderId': order.string('provider_order_id'),
      'createdAt': adminDate(order.string('created_at'), i.language),
      'paidAt': adminDate(order.string('paid_at'), i.language),
    };
    return Scaffold(
      backgroundColor: context.tokens.bg,
      appBar: AppBar(title: Text(i.t('admin-billing:tabs.orders'))),
      body: AdminList(
        children: [
          AdminCard(
            title: adminMoney(order),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Align(
                  alignment: Alignment.centerLeft,
                  child: AdminPill(
                    adminLabel(
                      i,
                      'admin-billing:orderStatus',
                      order.string('status'),
                    ),
                    status: order.string('status'),
                  ),
                ),
                for (final field in fields.entries)
                  AdminField(
                    i.t('admin-billing:orders.columns.${field.key}'),
                    field.value,
                  ),
                if (order.string('cancellation_reason').isNotEmpty)
                  Text(
                    i.t(
                      'admin-billing:orders.cancelled',
                      vars: {'reason': order.string('cancellation_reason')},
                    ),
                  ),
                if (allowWorkspace && order.string('workspace_id').isNotEmpty)
                  OutlinedButton(
                    onPressed: () => Navigator.push<void>(
                      context,
                      MaterialPageRoute(
                        builder: (_) => AdminWorkspacePage(
                          workspaceId: order.string('workspace_id'),
                        ),
                      ),
                    ),
                    child: Text(
                      '${i.t('admin-billing:subscriptions.columns.workspace')} · ${i.t('admin:mobile.details')}',
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
