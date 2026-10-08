import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/router/paths.dart';
import '../workspace/state/active_workspace_store.dart';
import 'api/billing_api.dart';
import 'state/billing_providers.dart';
import 'widgets/orders_tab.dart';
import 'widgets/purchase_tab.dart';
import 'widgets/usage_tab.dart';

class BillingScreen extends ConsumerStatefulWidget {
  const BillingScreen({super.key, this.initialTab = 'purchase'});

  final String initialTab;

  @override
  ConsumerState<BillingScreen> createState() => _BillingScreenState();
}

class _BillingScreenState extends ConsumerState<BillingScreen>
    with WidgetsBindingObserver {
  static const _tabs = ['purchase', 'usage', 'orders'];
  late String _tab = _tabs.contains(widget.initialTab)
      ? widget.initialTab
      : 'purchase';
  Timer? _reconcileTimer;
  bool _reconciling = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _configureReconciliation();
  }

  @override
  void didUpdateWidget(covariant BillingScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.initialTab != widget.initialTab &&
        _tabs.contains(widget.initialTab)) {
      _tab = widget.initialTab;
      _configureReconciliation();
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      unawaited(_reconcilePending());
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _reconcileTimer?.cancel();
    super.dispose();
  }

  void _selectTab(String tab) {
    if (_tab == tab) return;
    setState(() => _tab = tab);
    _configureReconciliation();
    context.replace(Paths.billing(tab));
  }

  void _configureReconciliation() {
    _reconcileTimer?.cancel();
    _reconcileTimer = null;
    if (_tab != 'orders') return;
    unawaited(_reconcilePending());
    _reconcileTimer = Timer.periodic(
      const Duration(seconds: 15),
      (_) => unawaited(_reconcilePending()),
    );
  }

  Future<void> _reconcilePending() async {
    if (_reconciling || !mounted) return;
    final workspaceId = ref
        .read(activeWorkspaceProvider)
        .valueOrNull
        ?.currentId;
    if (workspaceId == null) return;
    _reconciling = true;
    try {
      final api = ref.read(billingApiProvider);
      final providers = await api.getProviders();
      if (!mounted ||
          ref.read(activeWorkspaceProvider).valueOrNull?.currentId !=
              workspaceId) {
        return;
      }
      final queryable = {
        for (final provider in providers)
          if (provider.supportsStatusQuery) provider.id,
      };
      final page = await api.getOrders(page: 1, pageSize: 100);
      final pending = page.items
          .where(
            (order) =>
                (order.pending || order.reconcileRequired) &&
                queryable.contains(order.provider),
          )
          .take(3);
      await Future.wait([
        for (final order in pending)
          api.refreshOrder(order.id).catchError((_) => order),
      ]);
    } catch (_) {
      // The visible order page retains its own error/retry UI. Background
      // reconciliation is best-effort and intentionally quiet.
    } finally {
      if (mounted &&
          ref.read(activeWorkspaceProvider).valueOrNull?.currentId ==
              workspaceId) {
        invalidateBilling(ref);
      }
      _reconciling = false;
    }
  }

  @override
  Widget build(BuildContext context) {
    final tokens = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Scaffold(
      backgroundColor: tokens.bg,
      appBar: AppBar(
        titleSpacing: 0,
        title: Text(
          i18n.t('billing:title'),
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w500,
            color: tokens.ink,
          ),
        ),
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(46),
          // Start-aligned with the page content; an app bar centers a
          // narrower bottom otherwise.
          child: Align(
            alignment: AlignmentDirectional.centerStart,
            child: SingleChildScrollView(
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 10),
              scrollDirection: Axis.horizontal,
              child: Row(
                children: [
                  for (final tab in _tabs)
                    Padding(
                      padding: const EdgeInsets.only(right: 8),
                      child: ChoiceChip(
                        label: Text(
                          i18n.t('billing:tabs.$tab'),
                          style: const TextStyle(fontSize: FontSizes.sm),
                        ),
                        selected: _tab == tab,
                        showCheckmark: false,
                        selectedColor: tokens.a200,
                        backgroundColor: tokens.bg,
                        labelStyle: TextStyle(color: tokens.ink),
                        side: BorderSide(
                          color: _tab == tab ? tokens.a700 : tokens.hair,
                        ),
                        onSelected: (_) => _selectTab(tab),
                      ),
                    ),
                ],
              ),
            ),
          ),
        ),
      ),
      body: switch (_tab) {
        'usage' => const BillingUsageTab(),
        'orders' => const BillingOrdersTab(),
        _ => BillingPurchaseTab(onOpenOrders: () => _selectTab('orders')),
      },
    );
  }
}
