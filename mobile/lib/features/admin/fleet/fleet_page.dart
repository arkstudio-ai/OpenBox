import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../state/admin_load_state.dart';
import '../widgets/admin_confirm.dart';
import '../widgets/admin_filters.dart';
import '../widgets/admin_widgets.dart';
import 'adopt_page.dart';
import 'desktop_page.dart';
import 'pool_summary.dart';

class AdminFleetPage extends ConsumerStatefulWidget {
  const AdminFleetPage({super.key, this.active = true});
  final bool active;
  @override
  ConsumerState<AdminFleetPage> createState() => _FleetState();
}

class _FleetState extends AdminLoadState<FleetData, AdminFleetPage>
    with WidgetsBindingObserver {
  String _tab = 'pool';
  final _search = TextEditingController();
  String _q = '', _poolState = '';
  int _offset = 0, _alertOffset = 0;
  bool _foreground = true;
  Timer? _timer;
  String? _feedback;

  @override
  Future<FleetData> fetch(CancelToken cancel) => api.fleet(
    cancel,
    desktopQuery: {
      'q': _q,
      if (_poolState.isNotEmpty) 'pool_state': _poolState,
      'offset': _offset,
      'limit': 20,
    },
    alertOffset: _alertOffset,
  );
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _schedule();
  }

  void _schedule() {
    _timer?.cancel();
    if (widget.active && _foreground) {
      _timer = Timer.periodic(const Duration(seconds: 30), (_) {
        if (!loading && ModalRoute.of(context)?.isCurrent == true) {
          unawaited(reload());
        }
      });
    }
  }

  @override
  void didUpdateWidget(covariant AdminFleetPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    _schedule();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    _schedule();
  }

  @override
  void dispose() {
    _timer?.cancel();
    _search.dispose();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  Future<void> _ensure(bool dry) async {
    final changed = await confirmAdminAction(
      context,
      title: i18n.t(dry ? 'admin:pool.dryRun' : 'admin:pool.ensure'),
      body: i18n.t('admin:pool.confirmEnsure'),
      confirm: i18n.t(dry ? 'admin:pool.dryRun' : 'admin:pool.ensure'),
      run: (_, cancel) async {
        final result = await api.ensurePool(dry, cancel);
        if (!mounted) return;
        setState(
          () => _feedback =
              '${i18n.t('admin:pool.ensureResult', vars: {'status': result.string('status'), 'current': result.integer('current'), 'target': result.integer('target'), 'quantity': result.integer('quantity')})}\n${result.string('message')}',
        );
      },
    );
    if (changed && mounted) await reload();
  }

  Future<void> _alert(AdminRecord row, bool mute) async {
    final key = mute ? 'mute' : 'ack';
    final changed = await confirmAdminAction(
      context,
      title: i18n.t('admin:alerts.$key'),
      body: row.string('message'),
      confirm: i18n.t('admin:alerts.$key'),
      run: (_, cancel) => mute
          ? api.muteAlert(
              row.string('id'),
              DateTime.now().add(const Duration(hours: 24)),
              cancel,
            )
          : api.acknowledgeAlert(row.string('id'), cancel),
    );
    if (changed && mounted) await reload();
  }

  Future<void> _filter() async {
    final states = data?.pool.record('states').data.keys ?? const <String>[];
    final values = await showAdminFilters(
      context,
      values: {'pool_state': _poolState.isEmpty ? 'all' : _poolState},
      fields: [
        AdminFilterField(
          'pool_state',
          i18n.t('admin:desktops.state'),
          options: {
            'all': i18n.t('admin-billing:common.all'),
            for (final state in states)
              state: adminLabel(i18n, 'admin:mobile.states', state),
          },
        ),
      ],
    );
    if (values == null || !mounted) return;
    setState(() {
      _poolState = values['pool_state'] == 'all'
          ? ''
          : values['pool_state'] ?? '';
      _offset = 0;
    });
    await reload(clear: true);
  }

  @override
  Widget build(BuildContext context) {
    final i = i18n;
    return Column(
      children: [
        Row(
          children: [
            Expanded(
              child: AdminTabs(
                labels: {
                  'pool': i.t('admin:mobile.overview'),
                  'desktops': i.t('admin:desktops.title'),
                  'alerts': i.t('admin:alerts.title'),
                },
                value: _tab,
                onChanged: (tab) => setState(() => _tab = tab),
              ),
            ),
            IconButton(
              tooltip: i.t('admin:mobile.refresh'),
              onPressed: loading ? null : reload,
              icon: const Icon(Icons.refresh),
            ),
          ],
        ),
        if (_tab == 'desktops')
          AdminSearchBar(
            controller: _search,
            hint: i.t('admin:mobile.searchDesktops'),
            onFilter: _filter,
            filterCount: _poolState.isEmpty ? 0 : 1,
            onSearch: (q) {
              setState(() {
                _q = q.trim();
                _offset = 0;
              });
              unawaited(reload(clear: true));
            },
          ),
        Expanded(
          child: loadable(
            (value) => AdminList(
              storageKey: switch (_tab) {
                'desktops' => 'fleet-desktops-$_q-$_poolState-$_offset',
                'alerts' => 'fleet-alerts-$_alertOffset',
                _ => 'fleet-pool',
              },
              onRefresh: reload,
              children: [
                if (_feedback != null) AdminCard(child: Text(_feedback!)),
                if (_tab == 'pool') ..._pool(value),
                if (_tab == 'desktops') ..._desktops(value),
                if (_tab == 'alerts') ..._alerts(value),
              ],
            ),
          ),
        ),
      ],
    );
  }

  List<Widget> _pool(FleetData value) => [
    AdminPoolSummary(
      pool: value.pool,
      busy: loading,
      onState: (state) {
        setState(() {
          _tab = 'desktops';
          _poolState = state;
          _offset = 0;
        });
        unawaited(reload(clear: true));
      },
      onEnsure: _ensure,
      onAdopt: () async {
        final changed = await Navigator.push<bool>(
          context,
          MaterialPageRoute(builder: (_) => const AdminAdoptPage()),
        );
        if (changed == true && mounted) await reload();
      },
    ),
  ];

  List<Widget> _desktops(FleetData value) => [
    if (_poolState.isNotEmpty)
      Padding(
        padding: const EdgeInsets.only(bottom: 12),
        child: Align(
          alignment: Alignment.centerLeft,
          child: InputChip(
            label: Text(adminLabel(i18n, 'admin:mobile.states', _poolState)),
            onDeleted: () {
              setState(() {
                _poolState = '';
                _offset = 0;
              });
              unawaited(reload(clear: true));
            },
          ),
        ),
      ),
    if (value.desktops.items.isEmpty)
      AdminCard(child: Text(i18n.t('admin:mobile.noDesktops'))),
    for (final row in value.desktops.items)
      AdminRecordTile(
        leading: Icon(
          Icons.desktop_windows_outlined,
          size: 22,
          color: context.tokens.n600,
        ),
        title: row.string('desktop_id', row.string('id')),
        subtitle: row.string('workspace_id', '—'),
        onTap: loading
            ? null
            : () async {
                final changed = await Navigator.push<bool>(
                  context,
                  MaterialPageRoute(
                    builder: (_) => AdminDesktopPage(desktop: row),
                  ),
                );
                if (changed == true && mounted) await reload();
              },
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
                    'admin:mobile.states',
                    row.string('pool_state'),
                  ),
                  status: row.string('pool_state'),
                ),
                Text(
                  [
                    adminLabel(
                      i18n,
                      'admin:mobile.states',
                      row.string('status').toLowerCase(),
                    ),
                    '${i18n.t('admin:desktops.channel')} · ${adminLabel(i18n, 'admin:mobile.states', row.string('tunnel_state'))}',
                  ].join(' · '),
                  style: TextStyle(
                    color: context.tokens.n600,
                    fontSize: FontSizes.sm,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 10),
            Text(
              row.string('spec', '—'),
              style: TextStyle(color: context.tokens.n600, fontSize: 12),
            ),
          ],
        ),
      ),
    AdminPager(
      total: value.desktops.total,
      offset: _offset,
      limit: 20,
      disabled: loading,
      onChanged: (offset) {
        setState(() => _offset = offset);
        unawaited(reload(clear: true));
      },
    ),
  ];

  List<Widget> _alerts(FleetData value) => [
    AdminCard(
      child: Text(
        i18n.t(
          'admin:snapshot',
          vars: {
            'time': adminDate(value.snapshot.string('taken_at'), i18n.language),
            'sources': value.snapshot
                .records('sources')
                .map(
                  (row) =>
                      '${row.string('source')}: ${row.flag('ok') ? 'ok' : row.string('error', 'error')}',
                )
                .join(' · '),
          },
        ),
        style: TextStyle(color: context.tokens.n600, fontSize: 12),
      ),
    ),
    if (value.alerts.items.isEmpty)
      AdminCard(child: Text(i18n.t('admin:alerts.empty'))),
    for (final row in value.alerts.items)
      AdminRecordTile(
        fullText: true,
        title: row.string('rule'),
        subtitle: row.string('resource_id'),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            AdminPill(row.string('severity'), status: row.string('severity')),
            const SizedBox(height: 10),
            Text(row.string('message')),
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              children: [
                if (row.string('acked_at').isEmpty)
                  OutlinedButton(
                    onPressed: loading ? null : () => _alert(row, false),
                    child: Text(i18n.t('admin:alerts.ack')),
                  ),
                TextButton(
                  onPressed: loading ? null : () => _alert(row, true),
                  child: Text(i18n.t('admin:alerts.mute')),
                ),
              ],
            ),
          ],
        ),
      ),
    if (_alertOffset > 0 || value.alerts.items.length == 50)
      Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          TextButton(
            onPressed: loading || _alertOffset == 0
                ? null
                : () {
                    setState(() => _alertOffset -= 50);
                    unawaited(reload(clear: true));
                  },
            child: Text(i18n.t('admin-skills:list.previous')),
          ),
          TextButton(
            onPressed: loading || value.alerts.items.length < 50
                ? null
                : () {
                    setState(() => _alertOffset += 50);
                    unawaited(reload(clear: true));
                  },
            child: Text(i18n.t('admin-skills:list.next')),
          ),
        ],
      ),
  ];
}
