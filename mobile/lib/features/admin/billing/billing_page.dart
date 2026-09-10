import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/i18n/i18n.dart';
import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../state/admin_load_state.dart';
import '../widgets/admin_filters.dart';
import '../widgets/admin_widgets.dart';
import 'billing_rows.dart';
import 'workspace_page.dart';

class AdminBillingPage extends ConsumerStatefulWidget {
  const AdminBillingPage({super.key});
  @override
  ConsumerState<AdminBillingPage> createState() => _BillingState();
}

class _BillingState extends ConsumerState<AdminBillingPage> {
  String _tab = 'subscriptions';
  bool _ordersVisited = false;
  @override
  Widget build(BuildContext context) {
    final i = ref.watch(i18nProvider);
    return Column(
      children: [
        AdminTabs(
          labels: {
            'subscriptions': i.t('admin-billing:tabs.subscriptions'),
            'orders': i.t('admin-billing:tabs.orders'),
          },
          value: _tab,
          onChanged: (tab) => setState(() {
            _tab = tab;
            _ordersVisited |= tab == 'orders';
          }),
        ),
        Expanded(
          child: IndexedStack(
            index: _tab == 'subscriptions' ? 0 : 1,
            children: [
              const _BillingList(orders: false),
              _ordersVisited
                  ? const _BillingList(orders: true)
                  : const SizedBox.shrink(),
            ],
          ),
        ),
      ],
    );
  }
}

class _BillingList extends ConsumerStatefulWidget {
  const _BillingList({required this.orders});
  final bool orders;
  @override
  ConsumerState<_BillingList> createState() => _BillingListState();
}

class _BillingListState extends AdminLoadState<AdminPage, _BillingList> {
  final _search = TextEditingController();
  final _providersCancel = CancelToken();
  List<AdminRecord> _providers = [];
  Map<String, String> _filters = {};
  int _offset = 0;
  static const _limit = 25;
  @override
  void initState() {
    super.initState();
    if (widget.orders) {
      scheduleMicrotask(() async {
        try {
          final result = await api.paymentProviders(_providersCancel);
          if (mounted) setState(() => _providers = result.records('items'));
        } catch (_) {
          // Provider registry failure must not block the order ledger.
        }
      });
    }
  }

  @override
  Future<AdminPage> fetch(CancelToken cancel) {
    final query = {
      ...adminFilters(_filters),
      'offset': _offset,
      'limit': _limit,
    };
    return widget.orders
        ? api.orders(query, cancel)
        : api.subscriptions(query, cancel);
  }

  @override
  void dispose() {
    _search.dispose();
    _providersCancel.cancel();
    super.dispose();
  }

  void _apply(Map<String, String> filters) {
    setState(() {
      _filters = filters;
      _offset = 0;
      _search.text = filters['q'] ?? '';
    });
    unawaited(reload(clear: true));
  }

  Map<String, String> _options(String prefix, List<String> values) => {
    'all': i18n.t('admin-billing:common.all'),
    for (final value in values)
      value: adminLabel(i18n, 'admin-billing:$prefix', value),
  };
  Future<void> _filter() async {
    final result = await showAdminFilters(
      context,
      values: _filters,
      fields: widget.orders
          ? [
              AdminFilterField(
                'provider',
                i18n.t('admin-billing:filters.provider'),
                options: {
                  'all': i18n.t('admin-billing:common.all'),
                  for (final row in _providers)
                    row.string('id'): row.string('name'),
                },
              ),
              AdminFilterField(
                'status',
                i18n.t('admin-billing:filters.status'),
                options: _options('orderStatus', [
                  'pending',
                  'paid',
                  'cancelled',
                ]),
              ),
              AdminFilterField(
                'kind',
                i18n.t('admin-billing:filters.kind'),
                options: _options('kinds', ['topup', 'subscription']),
              ),
              AdminFilterField(
                'from',
                i18n.t('admin-billing:filters.from'),
                date: true,
              ),
              AdminFilterField(
                'to',
                i18n.t('admin-billing:filters.to'),
                date: true,
              ),
            ]
          : [
              AdminFilterField(
                'plan',
                i18n.t('admin-billing:filters.plan'),
                options: _options('plans', ['free', 'pro', 'max']),
              ),
              AdminFilterField(
                'state',
                i18n.t('admin-billing:filters.state'),
                options: _options('states', ['active', 'expired', 'free']),
              ),
            ],
    );
    if (result != null && mounted) _apply(result);
  }

  @override
  Widget build(BuildContext context) {
    final prefix = widget.orders ? 'orders' : 'subscriptions';
    final count = adminFilters(_filters).length;
    return Column(
      children: [
        AdminSearchBar(
          controller: _search,
          hint: i18n.t('admin-billing:$prefix.searchPlaceholder'),
          onSearch: (q) => _apply({..._filters, 'q': q}),
          onFilter: _filter,
          filterCount: count,
          onRefresh: reload,
          disabled: loading,
        ),
        if (count > 0)
          Text(
            i18n.t('admin:mobile.filterSummary', count: count),
            style: const TextStyle(fontSize: 12),
          ),
        Expanded(
          child: loadable(
            (value) => AdminList(
              onRefresh: reload,
              children: [
                if (value.items.isEmpty)
                  AdminCard(child: Text(i18n.t('admin-billing:$prefix.empty'))),
                for (final row in value.items)
                  if (widget.orders)
                    AdminOrderCard(order: row)
                  else
                    AdminSubscriptionCard(
                      row: row,
                      onTap: loading
                          ? null
                          : () {
                              Navigator.push<void>(
                                context,
                                MaterialPageRoute(
                                  builder: (_) => AdminWorkspacePage(
                                    workspaceId: row
                                        .record('workspace')
                                        .string('id'),
                                  ),
                                ),
                              );
                            },
                    ),
                AdminPager(
                  total: value.total,
                  offset: _offset,
                  limit: _limit,
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
