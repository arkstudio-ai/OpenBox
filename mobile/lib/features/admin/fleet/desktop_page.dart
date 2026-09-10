import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../widgets/admin_confirm.dart';
import '../widgets/admin_widgets.dart';
import 'diagnostics_page.dart';

class AdminDesktopPage extends ConsumerWidget {
  const AdminDesktopPage({super.key, required this.desktop});
  final AdminRecord desktop;
  Future<void> _action(
    BuildContext context,
    WidgetRef ref,
    String action,
  ) async {
    final i = ref.read(i18nProvider);
    final id = desktop.string('desktop_id', desktop.string('id'));
    final confirmKey = switch (action) {
      'release' => 'confirmRelease',
      'recycle' => 'confirmRecycle',
      _ => 'confirmRetire',
    };
    final changed = await confirmAdminAction(
      context,
      target: id,
      title: i.t('admin:desktops.$action'),
      body: '$id\n${i.t('admin:desktops.$confirmKey')}',
      confirm: i.t('admin:desktops.$action'),
      run: (_, cancel) =>
          ref.read(adminApiProvider).desktopAction(id, action, cancel),
    );
    if (changed && context.mounted) Navigator.pop(context, true);
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i = ref.watch(i18nProvider);
    final row = desktop;
    final id = row.string('desktop_id', row.string('id'));
    final state = row.string('pool_state');
    final users = row.data['ecd_end_users'] == null
        ? i.t('admin:desktops.unknown')
        : row.records('ecd_end_users').isEmpty
        ? i.t('admin:desktops.unbound')
        : row
              .records('ecd_end_users')
              .map(
                (user) => '${user.string('username')} · ${user.string('id')}',
              )
              .join('\n');
    return Scaffold(
      backgroundColor: context.tokens.bg,
      appBar: AppBar(title: Text(i.t('admin:desktops.title'))),
      bottomNavigationBar: AdminActionBar(
        primary: FilledButton.icon(
          icon: const Icon(Icons.monitor_heart_outlined),
          onPressed: () => Navigator.push<void>(
            context,
            MaterialPageRoute(
              builder: (_) => AdminDiagnosticsPage(desktopId: id),
            ),
          ),
          label: Text(i.t('admin:diag.open')),
        ),
      ),
      body: AdminList(
        children: [
          AdminCard(
            title: id,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    AdminPill(
                      adminLabel(i, 'admin:mobile.states', state),
                      status: state,
                    ),
                    AdminPill(
                      adminLabel(
                        i,
                        'admin:mobile.states',
                        row.string('status').toLowerCase(),
                      ),
                      status: row.string('status'),
                    ),
                  ],
                ),
                AdminField(
                  i.t('admin:desktops.owner'),
                  row.string('workspace_id'),
                ),
                AdminField(i.t('admin:desktops.ecdUsers'), users),
                AdminField(
                  i.t('admin:desktops.channel'),
                  adminLabel(
                    i,
                    'admin:mobile.states',
                    row.string('tunnel_state'),
                  ),
                ),
                AdminField(
                  i.t('admin:desktops.billing'),
                  '${row.string('charge_type')} · ${row.string('spec')}',
                ),
                AdminField(
                  i.t('admin:desktops.expires'),
                  adminDate(row.string('expires_at'), i.language),
                ),
              ],
            ),
          ),
          if ({
            'assigned',
            'reserve',
            'prewarm',
            'released',
          }.contains(state)) ...[
            AdminSectionHeading(i.t('admin:mobile.operations')),
            for (final action
                in state == 'assigned' ? ['release'] : ['recycle', 'retire'])
              AdminRecordTile(
                title: i.t('admin:desktops.$action'),
                subtitle: i.t(
                  'admin:desktops.${switch (action) {
                    'release' => 'confirmRelease',
                    'recycle' => 'confirmRecycle',
                    _ => 'confirmRetire',
                  }}',
                ),
                leading: Icon(
                  action == 'recycle'
                      ? Icons.restart_alt
                      : Icons.remove_circle_outline,
                  color: context.tokens.danger,
                  size: 22,
                ),
                onTap: () => _action(context, ref, action),
              ),
          ],
        ],
      ),
    );
  }
}
