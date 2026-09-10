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
import 'desktop_install_page.dart';

class AdminInstallsPage extends ConsumerStatefulWidget {
  const AdminInstallsPage({super.key});
  @override
  ConsumerState<AdminInstallsPage> createState() => _InstallsState();
}

class _InstallsState extends ConsumerState<AdminInstallsPage> {
  String _tab = 'live';
  bool _historyVisited = false;
  @override
  Widget build(BuildContext context) {
    final i = ref.watch(i18nProvider);
    return Column(
      children: [
        AdminScopePicker(
          label: i.t('admin:mobile.installView'),
          options: {
            'live': i.t('admin-skills:desktop.live'),
            'history': i.t('admin-skills:desktop.history'),
          },
          value: _tab,
          onChanged: (tab) => setState(() {
            _tab = tab;
            _historyVisited = _historyVisited || tab == 'history';
          }),
        ),
        Expanded(
          child: IndexedStack(
            index: _tab == 'live' ? 0 : 1,
            children: [
              const _InstallList(live: true),
              _historyVisited
                  ? const _InstallList(live: false)
                  : const SizedBox.shrink(),
            ],
          ),
        ),
      ],
    );
  }
}

class AdminInstallHistoryPage extends ConsumerWidget {
  const AdminInstallHistoryPage({super.key, required this.catalogId});
  final String catalogId;
  @override
  Widget build(BuildContext context, WidgetRef ref) => Scaffold(
    appBar: AppBar(
      title: Text(ref.watch(i18nProvider).t('admin-skills:desktop.history')),
    ),
    body: _InstallList(live: false, catalogId: catalogId),
  );
}

class _InstallList extends ConsumerStatefulWidget {
  const _InstallList({required this.live, this.catalogId});
  final bool live;
  final String? catalogId;
  @override
  ConsumerState<_InstallList> createState() => _ListState();
}

class _ListState extends AdminLoadState<AdminPage, _InstallList> {
  final _search = TextEditingController();
  String _query = '';
  String? _catalog;
  int _offset = 0;
  @override
  void initState() {
    _catalog = widget.catalogId;
    super.initState();
  }

  @override
  Future<AdminPage> fetch(CancelToken cancel) {
    final query = <String, dynamic>{
      if (_query.trim().isNotEmpty) 'q': _query.trim(),
      if (_catalog != null) 'catalog_id': _catalog,
      'offset': _offset,
      'limit': 20,
    };
    return widget.live
        ? api.installedDesktops(query, cancel)
        : api.skillInstalls(query, cancel);
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final i = i18n;
    return Column(
      children: [
        AdminSearchBar(
          controller: _search,
          hint: i.t(
            widget.live
                ? 'admin-skills:desktop.search'
                : 'admin-skills:installs.search',
          ),
          onSearch: (query) {
            setState(() {
              _query = query;
              _offset = 0;
            });
            unawaited(reload(clear: true));
          },
        ),
        if (_catalog != null)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: Column(
              children: [
                Text(
                  i.t(
                    'admin-skills:installs.filtered',
                    vars: {'catalog': _catalog},
                  ),
                ),
                TextButton(
                  onPressed: () {
                    setState(() {
                      _catalog = null;
                      _offset = 0;
                    });
                    unawaited(reload(clear: true));
                  },
                  child: Text(i.t('admin-skills:installs.clearFilter')),
                ),
              ],
            ),
          ),
        Expanded(
          child: loadable(
            (page) => AdminList(
              onRefresh: reload,
              children: [
                Text(
                  i.t(
                    widget.live
                        ? 'admin-skills:desktop.hint'
                        : 'admin-skills:installs.footnote',
                  ),
                ),
                const SizedBox(height: 12),
                if (page.items.isEmpty)
                  AdminCard(
                    child: Text(
                      i.t(
                        widget.live
                            ? 'admin-skills:desktop.empty'
                            : 'admin-skills:installs.empty',
                      ),
                    ),
                  ),
                for (final row in page.items)
                  if (widget.live)
                    AdminRecordTile(
                      title: row.string(
                        'workspace_name',
                        row.string('username', row.string('desktop_id')),
                      ),
                      subtitle: row.string('desktop_id'),
                      onTap: () => Navigator.push<void>(
                        context,
                        MaterialPageRoute(
                          builder: (_) => AdminDesktopInstallPage(desktop: row),
                        ),
                      ),
                      child: Row(
                        children: [
                          Expanded(
                            child: Text(
                              i.t(
                                'admin-skills:desktop.connection',
                                vars: {
                                  'status': row.string('status', '—'),
                                  'channel': row.string('channel_state', '—'),
                                },
                              ),
                            ),
                          ),
                        ],
                      ),
                    )
                  else
                    AdminRecordTile(
                      key: ValueKey(row.string('id')),
                      fullText: true,
                      title: row.string('title'),
                      subtitle: row.string('catalog_id'),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          Row(
                            children: [
                              AdminIcon(row.string('icon')),
                              const SizedBox(width: 12),
                              AdminPill(row.string('kind').toUpperCase()),
                            ],
                          ),
                          AdminField(
                            i.t('admin-skills:installs.column.user'),
                            [
                              row.record('user').string('username'),
                              row.record('user').string('email'),
                            ].where((value) => value.isNotEmpty).join('\n'),
                          ),
                          AdminField(
                            i.t('admin-skills:installs.column.installDir'),
                            row.string('install_dir'),
                          ),
                          AdminField(
                            i.t('admin-skills:installs.column.installedAt'),
                            adminDate(row.string('installed_at'), i.language),
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
