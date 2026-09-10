import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../state/admin_load_state.dart';
import '../widgets/admin_confirm.dart';
import '../widgets/admin_widgets.dart';

class _Diagnostics {
  const _Diagnostics(this.events, this.snapshots, this.latest);
  final AdminPage events, snapshots;
  final AdminRecord? latest;
}

class AdminDiagnosticsPage extends ConsumerStatefulWidget {
  const AdminDiagnosticsPage({super.key, required this.desktopId});
  final String desktopId;
  @override
  ConsumerState<AdminDiagnosticsPage> createState() => _DiagnosticsState();
}

class _DiagnosticsState
    extends AdminLoadState<_Diagnostics, AdminDiagnosticsPage> {
  String? _picked;
  AdminRecord? _fresh;
  @override
  Future<_Diagnostics> fetch(CancelToken cancel) async {
    final pages = await Future.wait([
      api.desktopEvents(widget.desktopId, cancel),
      api.desktopDiagnostics(widget.desktopId, cancel),
    ]);
    final records = pages[1].items;
    final id = records.any((row) => row.string('id') == _picked)
        ? _picked
        : records.firstOrNull?.string('id');
    return _Diagnostics(
      pages[0],
      pages[1],
      id == null ? null : await api.diagnostic(id, cancel),
    );
  }

  Future<void> _collect() async {
    final changed = await confirmAdminAction(
      context,
      title: i18n.t('admin:diag.collect'),
      body: widget.desktopId,
      confirm: i18n.t('admin:diag.collect'),
      run: (_, cancel) async {
        final result = await api.collectDiagnostic(widget.desktopId, cancel);
        if (mounted) setState(() => _fresh = result);
      },
    );
    if (changed && mounted) await reload();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: context.tokens.bg,
    appBar: AppBar(
      title: Text(i18n.t('admin:diag.title')),
      actions: [
        IconButton(
          tooltip: i18n.t('admin:mobile.refresh'),
          onPressed: loading ? null : reload,
          icon: const Icon(Icons.refresh),
        ),
      ],
    ),
    body: loadable((value) {
      final report = _fresh ?? value.latest?.record('report');
      final summary = report?.record('summary');
      final lights = summary?.record('lights');
      final records = value.snapshots.items;
      return AdminList(
        onRefresh: reload,
        children: [
          AdminCard(
            title: widget.desktopId,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                OutlinedButton.icon(
                  onPressed: loading ? null : _collect,
                  icon: const Icon(Icons.monitor_heart_outlined),
                  label: Text(i18n.t('admin:diag.collect')),
                ),
                if (records.isNotEmpty) ...[
                  const SizedBox(height: 14),
                  DropdownButtonFormField<String>(
                    key: ValueKey((
                      _picked,
                      _fresh?.string('id'),
                      records.first.string('id'),
                    )),
                    isExpanded: true,
                    initialValue: records.any((r) => r.string('id') == _picked)
                        ? _picked
                        : records.first.string('id'),
                    decoration: InputDecoration(
                      labelText: i18n.t('admin:diag.pick'),
                    ),
                    items: [
                      for (final row in records)
                        DropdownMenuItem(
                          value: row.string('id'),
                          child: Text(
                            '${adminDate(row.string('ts'), i18n.language)} · ${row.string('reason')}',
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                    ],
                    onChanged: loading
                        ? null
                        : (id) {
                            setState(() {
                              _picked = id;
                              _fresh = null;
                            });
                            reload(clear: true);
                          },
                  ),
                ],
                if (report == null || report.data.isEmpty) ...[
                  const SizedBox(height: 12),
                  Text(i18n.t('admin:diag.noSnapshot')),
                  if (value.latest?.string('error').isNotEmpty ?? false)
                    Text(
                      value.latest!.string('error'),
                      style: TextStyle(color: context.tokens.danger),
                    ),
                ] else ...[
                  const SizedBox(height: 14),
                  Text(
                    i18n.t(
                      'admin:diag.latest',
                      vars: {
                        'time': adminDate(
                          _fresh?.string('collected_at') ??
                              value.latest!.string('ts'),
                          i18n.language,
                        ),
                        'reason': _fresh == null
                            ? value.latest?.string('reason')
                            : i18n.t('admin:diag.freshReason'),
                      },
                    ),
                    style: TextStyle(color: context.tokens.n600, fontSize: 12),
                  ),
                  if (_fresh != null)
                    Text(
                      i18n.t(
                        'admin:diag.collectedVia',
                        vars: {
                          'via': _fresh!.string('via'),
                          'ms': _fresh!.integer('elapsed_ms'),
                        },
                      ),
                    ),
                  if (_fresh?.strings('fallback_errors').isNotEmpty ?? false)
                    Text(
                      i18n.t(
                        'admin:diag.fallback',
                        vars: {
                          'error': _fresh!
                              .strings('fallback_errors')
                              .join('; '),
                        },
                      ),
                    ),
                  const SizedBox(height: 12),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      for (final name in [
                        'chrome',
                        'relay',
                        'x',
                        'unit',
                        'runtime',
                      ])
                        AdminPill(
                          '${i18n.t('admin:diag.lights.$name')} · ${adminLabel(i18n, 'admin:diag.light', lights?.string(name, 'unknown') ?? 'unknown')}',
                          status: lights?.string(name) ?? 'unknown',
                        ),
                    ],
                  ),
                  const SizedBox(height: 12),
                  AdminField(
                    i18n.t('admin:diag.findings'),
                    summary!.strings('findings').isEmpty
                        ? i18n.t('admin:diag.noFindings')
                        : summary.strings('findings').join('\n'),
                  ),
                  if (report.records('errors').isNotEmpty)
                    Text(
                      i18n.t(
                        'admin:diag.sections',
                        vars: {
                          'sections': report
                              .records('errors')
                              .map(
                                (row) =>
                                    '${row.string('section')}: ${row.string('error')}',
                              )
                              .join('\n'),
                        },
                      ),
                      style: TextStyle(color: context.tokens.danger),
                    ),
                ],
              ],
            ),
          ),
          if (report != null && report.data.isNotEmpty)
            AdminCard(
              title: i18n.t('admin:diag.logs'),
              child: Column(
                children: [
                  _log('chromeLog', report.record('chrome').record('log')),
                  _log('relayLog', report.record('relay').record('log')),
                  _log('journal', report.record('logs').record('journal')),
                ],
              ),
            ),
          AdminCard(
            title: i18n.t('admin:diag.timeline'),
            child: value.events.items.isEmpty
                ? Text(i18n.t('admin:diag.timelineEmpty'))
                : Column(
                    children: [
                      for (final event in value.events.items) _event(event),
                    ],
                  ),
          ),
        ],
      );
    }),
  );

  Widget _log(String name, AdminRecord log) {
    final lines = log.strings('lines');
    final value = lines.isNotEmpty
        ? lines.join('\n')
        : log.string('error', i18n.t('admin:diag.empty'));
    return ExpansionTile(
      tilePadding: EdgeInsets.zero,
      childrenPadding: const EdgeInsets.only(bottom: 12),
      title: Text(
        i18n.t('admin:diag.$name'),
        style: const TextStyle(fontSize: 14),
      ),
      children: [
        ConstrainedBox(
          constraints: const BoxConstraints(maxHeight: 280),
          child: SingleChildScrollView(
            child: SelectableText(
              value,
              style: TextStyle(
                fontFamily: 'monospace',
                color: context.tokens.n700,
                fontSize: 12,
              ),
            ),
          ),
        ),
      ],
    );
  }

  Widget _event(AdminRecord event) => ExpansionTile(
    tilePadding: EdgeInsets.zero,
    title: Text(
      adminLabel(i18n, 'admin:diag.kinds', event.string('kind')),
      style: const TextStyle(fontSize: 14),
    ),
    subtitle: Text(
      '${adminDate(event.string('ts'), i18n.language)}\n${event.string('summary')}',
      style: const TextStyle(fontSize: 12),
    ),
    children: [
      Align(
        alignment: Alignment.centerLeft,
        child: AdminPill(
          event.string('status'),
          status: event.string('status'),
        ),
      ),
      if (event.data['duration_ms'] != null)
        AdminField(
          i18n.t(
            'admin:diag.duration',
            vars: {'ms': event.integer('duration_ms')},
          ),
          '',
        ),
      for (final pair in {
        'event': 'id',
        'session': 'session_id',
        'toolCall': 'tool_call_id',
        'request': 'request_id',
      }.entries)
        if (event.string(pair.value).isNotEmpty)
          Align(
            alignment: Alignment.centerLeft,
            child: AdminField(
              i18n.t('admin:diag.fields.${pair.key}'),
              event.string(pair.value),
            ),
          ),
      if (event.string('diag_id').isNotEmpty)
        Text(i18n.t('admin:diag.cites', vars: {'id': event.string('diag_id')})),
    ],
  );
}
