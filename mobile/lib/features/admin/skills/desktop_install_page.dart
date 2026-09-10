import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/utils/error_text.dart';
import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../state/admin_load_state.dart';
import '../widgets/admin_confirm.dart';
import '../widgets/admin_widgets.dart';

class AdminDesktopInstallPage extends ConsumerStatefulWidget {
  const AdminDesktopInstallPage({super.key, required this.desktop});
  final AdminRecord desktop;
  @override
  ConsumerState<AdminDesktopInstallPage> createState() => _DesktopState();
}

class _DesktopState
    extends AdminLoadState<AdminRecord, AdminDesktopInstallPage> {
  String _user = '';
  bool _uninstallFailed = false;
  String? _message;
  @override
  bool get loadOnMount => false;
  String get _desktop => widget.desktop.string('desktop_id');
  @override
  void initState() {
    super.initState();
    _user = widget.desktop.records('members').firstOrNull?.string('id') ?? '';
  }

  @override
  Future<AdminRecord> fetch(CancelToken cancel) =>
      api.scanDesktop(_desktop, _user, cancel);
  Future<void> _scan() async {
    if (_user.isEmpty || loading) return;
    setState(() {
      _uninstallFailed = false;
      _message = null;
    });
    await reload(clear: true);
  }

  Future<void> _uninstall(AdminRecord skill) async {
    if (loading || _uninstallFailed || !skill.flag('removable')) return;
    final i = i18n;
    final user = _user;
    final dir = skill.string('install_dir');
    final changed = await confirmAdminAction(
      context,
      title: i.t(
        'admin-skills:desktop.removeTitle',
        vars: {'name': skill.string('name')},
      ),
      body: i.t(
        'admin-skills:desktop.removeHint',
        vars: {'desktop': _desktop, 'user': user, 'dir': dir},
      ),
      target: dir,
      requireReason: true,
      retryOnError: false,
      confirm: i.t('admin-skills:desktop.uninstall'),
      run: (reason, cancel) async {
        try {
          await api.uninstallSkill(_desktop, user, skill, reason, cancel);
        } catch (_) {
          if (mounted) setState(() => _uninstallFailed = true);
          rethrow;
        }
      },
    );
    if (!mounted) return;
    if (changed) {
      ref.read(adminSkillsChangedProvider)();
      await reload(clear: true);
      if (mounted) {
        setState(() => _message = i.t('admin-skills:desktop.removed'));
      }
    } else if (_uninstallFailed) {
      clearRead();
      setState(() => _message = i.t('admin-skills:desktop.removeFailed'));
    }
  }

  @override
  Widget build(BuildContext context) {
    final i = i18n;
    final members = widget.desktop.records('members');
    final scan = data;
    final unavailable = scan?.strings('unavailable') ?? [];
    return Scaffold(
      appBar: AppBar(title: Text(i.t('admin-skills:desktop.live'))),
      body: AdminList(
        children: [
          AdminCard(
            title: widget.desktop.string('workspace_name', _desktop),
            subtitle: _desktop,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(i.t('admin-skills:desktop.hint')),
                const SizedBox(height: 16),
                if (members.isEmpty)
                  Text(i.t('admin-skills:desktop.noMember'))
                else
                  DropdownButtonFormField<String>(
                    key: const ValueKey('admin-scan-member'),
                    initialValue: _user,
                    isExpanded: true,
                    decoration: InputDecoration(
                      labelText: i.t('admin-skills:desktop.user'),
                    ),
                    items: [
                      for (final member in members)
                        DropdownMenuItem(
                          value: member.string('id'),
                          child: Text(
                            [
                              member.string('username'),
                              member.string('email'),
                            ].where((part) => part.isNotEmpty).join(' · '),
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                    ],
                    onChanged: loading
                        ? null
                        : (user) {
                            clearRead();
                            setState(() {
                              _user = user ?? '';
                              _uninstallFailed = false;
                              _message = null;
                            });
                          },
                  ),
                if (_user.isNotEmpty)
                  AdminField(i.t('admin:mobile.fieldTarget'), _user),
                const SizedBox(height: 12),
                FilledButton.icon(
                  key: const ValueKey('admin-scan-desktop'),
                  onPressed: loading || _user.isEmpty ? null : _scan,
                  icon: const Icon(Icons.search),
                  label: Text(
                    i.t(
                      loading
                          ? 'admin-skills:desktop.scanning'
                          : 'admin-skills:desktop.scan',
                    ),
                  ),
                ),
              ],
            ),
          ),
          if (loading) const LinearProgressIndicator(),
          if (loadError != null)
            AdminCard(
              child: Text(
                '${i.t('admin-skills:desktop.unknown')}\n${errorText(i, loadError!)}',
                style: TextStyle(color: context.tokens.danger),
              ),
            ),
          if (_message != null)
            AdminCard(
              child: Text(
                _message!,
                style: TextStyle(
                  color: _uninstallFailed
                      ? context.tokens.danger
                      : context.tokens.n700,
                ),
              ),
            ),
          if (scan != null) ...[
            Text(
              i.t(
                'admin-skills:desktop.scannedAt',
                vars: {
                  'time': adminDate(scan.string('scanned_at'), i.language),
                },
              ),
            ),
            const SizedBox(height: 12),
            if (unavailable.isNotEmpty)
              AdminCard(
                child: Text(
                  i.t(
                    'admin-skills:desktop.partial',
                    vars: {'kinds': unavailable.join(', ')},
                  ),
                  style: TextStyle(color: context.tokens.danger),
                ),
              ),
            if (scan.records('items').isEmpty && unavailable.isEmpty)
              AdminCard(child: Text(i.t('admin-skills:desktop.noSkills'))),
            for (final skill in scan.records('items'))
              AdminRecordTile(
                fullText: true,
                title: skill.string('name'),
                leading: AdminIcon(skill.string('icon')),
                subtitle: skill.string('kind').toUpperCase(),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    AdminField(
                      i.t('admin-skills:installs.column.installDir'),
                      skill.string('install_dir'),
                    ),
                    AdminField(
                      i.t('admin-skills:store.filter.origin'),
                      skill.string('source'),
                    ),
                    if (skill.strings('names').length > 1)
                      Text(
                        i.t(
                          'admin-skills:desktop.collection',
                          count: skill.strings('names').length,
                          vars: {'names': skill.strings('names').join(', ')},
                        ),
                      ),
                    if (skill.string('description').isNotEmpty)
                      ExpansionTile(
                        tilePadding: EdgeInsets.zero,
                        title: Text(
                          i.t('admin:mobile.details'),
                          style: const TextStyle(fontSize: 13),
                        ),
                        children: [
                          Padding(
                            padding: const EdgeInsets.only(bottom: 12),
                            child: Text(skill.string('description')),
                          ),
                        ],
                      ),
                    OutlinedButton(
                      onPressed:
                          loading ||
                              _uninstallFailed ||
                              !skill.flag('removable') ||
                              unavailable.contains(skill.string('kind'))
                          ? null
                          : () => _uninstall(skill),
                      child: Text(
                        i.t(
                          skill.flag('removable')
                              ? 'admin-skills:desktop.uninstall'
                              : 'admin-skills:desktop.protected',
                        ),
                      ),
                    ),
                  ],
                ),
              ),
          ],
        ],
      ),
    );
  }
}
