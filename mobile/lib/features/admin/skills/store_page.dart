import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../state/admin_load_state.dart';
import '../widgets/admin_confirm.dart';
import '../widgets/admin_filters.dart';
import '../widgets/admin_widgets.dart';
import 'editor_page.dart';
import 'installs_page.dart';
import 'review_detail_page.dart';
import 'store_card.dart';
import 'upload_page.dart';

class AdminStorePage extends ConsumerStatefulWidget {
  const AdminStorePage({super.key, this.active = true});
  final bool active;
  @override
  ConsumerState<AdminStorePage> createState() => _StoreState();
}

class _StoreState extends AdminLoadState<AdminPage, AdminStorePage> {
  @override
  void didUpdateWidget(covariant AdminStorePage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.active && !oldWidget.active) {
      unawaited(reload());
    }
  }

  final _search = TextEditingController();
  final _selected = <String>{};
  Map<String, String> _filters = {};
  int _offset = 0;
  bool _trash = false, _selecting = false;
  String? _feedback;
  @override
  Future<AdminPage> fetch(CancelToken cancel) => api.skillStore({
    ...adminFilters(_filters),
    'offset': _offset,
    'limit': 20,
    if (_trash) 'deleted': true,
  }, cancel);
  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  void _apply(Map<String, String> filters) {
    setState(() {
      _filters = filters;
      _offset = 0;
      _selected.clear();
      _search.text = filters['q'] ?? '';
    });
    unawaited(reload(clear: true));
  }

  Future<void> _filter() async {
    final result = await showAdminFilters(
      context,
      values: _filters,
      fields: [
        for (final item in {
          'origin': ['all', 'official', 'community', 'third_party'],
          'kind': ['all', 'skill', 'mcp'],
          if (!_trash)
            'listing': ['all', 'listed', 'delisted', 'pending', 'rejected'],
        }.entries)
          AdminFilterField(
            item.key,
            i18n.t('admin-skills:store.filter.${item.key}'),
            options: {
              for (final value in item.value)
                value: i18n.t('admin-skills:store.${item.key}.$value'),
            },
          ),
      ],
    );
    if (result != null && mounted) _apply(result);
  }

  Future<void> _changed() async {
    if (!mounted) return;
    ref.read(adminSkillsChangedProvider)();
    await reload();
    final page = data;
    if (mounted && page != null && page.items.isEmpty && _offset > 0) {
      setState(
        () => _offset = page.total == 0 ? 0 : ((page.total - 1) ~/ 20) * 20,
      );
      await reload(clear: true);
    }
  }

  Future<void> _editor([String? id]) async {
    final changed = await Navigator.push<bool>(
      context,
      MaterialPageRoute(builder: (_) => AdminSkillEditorPage(catalogId: id)),
    );
    if (changed == true) await _changed();
  }

  Future<void> _delete(List<String> ids) async {
    AdminPage? result;
    final changed = await confirmAdminAction(
      context,
      title: i18n.t('admin-skills:manage.deleteTitle', count: ids.length),
      body: '${ids.join('\n')}\n\n${i18n.t('admin-skills:manage.deleteHint')}',
      confirm: i18n.t('admin-skills:manage.delete'),
      requireReason: true,
      run: (reason, cancel) async {
        result = await api.deleteSkills(ids, reason, cancel);
      },
    );
    if (!changed || !mounted || result == null) return;
    final succeeded = result!.items
        .where((row) => row.flag('ok'))
        .map((row) => row.string('catalog_id'))
        .toSet();
    final failures = ids.where((id) => !succeeded.contains(id)).toList();
    setState(() {
      _selecting = failures.isNotEmpty;
      _selected
        ..clear()
        ..addAll(failures);
      _feedback = i18n.t(
        'admin-skills:manage.deleteResult',
        vars: {'success': succeeded.length, 'failed': failures.length},
      );
      final details = result!.items
          .where((row) => !row.flag('ok'))
          .map((row) => '${row.string('catalog_id')}: ${row.string('error')}')
          .join('\n');
      if (details.isNotEmpty) _feedback = '${_feedback!}\n$details';
    });
    await _changed();
  }

  Future<void> _action(AdminRecord row, String action) async {
    final id = row.string('catalog_id');
    if (action == 'edit') {
      await _editor(id);
      return;
    }
    if (action == 'delete') {
      await _delete([id]);
      return;
    }
    if (action == 'view') {
      await Navigator.push<void>(
        context,
        MaterialPageRoute(builder: (_) => AdminReviewDetailPage(catalogId: id)),
      );
      if (mounted) await reload();
      return;
    }
    if (action == 'installs') {
      await Navigator.push<void>(
        context,
        MaterialPageRoute(
          builder: (_) => AdminInstallHistoryPage(catalogId: id),
        ),
      );
      return;
    }
    final shelf = action == 'list' || action == 'delist';
    final label = action == 'restore'
        ? i18n.t('admin-skills:manage.restore')
        : i18n.t('admin-skills:action.$action');
    final changed = await confirmAdminAction(
      context,
      title: shelf
          ? i18n.t(
              'admin-skills:dialog.$action.title',
              vars: {'title': row.string('title')},
            )
          : '$label · ${row.string('title')}',
      body: shelf ? i18n.t('admin-skills:dialog.$action.body') : id,
      confirm: label,
      requireReason: action == 'delist',
      showNote: shelf,
      run: (note, cancel) async {
        switch (action) {
          case 'restore':
            await api.restoreSkill(id, cancel);
          case 'list' || 'delist':
            await api.setListing(
              id,
              action == 'list' ? 'listed' : 'delisted',
              note,
              cancel,
            );
          case 'feature' || 'unfeature':
            await api.setFeatured(id, action == 'feature', cancel);
          case 'markOfficial' || 'unmarkOfficial':
            await api.setOfficial(id, action == 'markOfficial', cancel);
        }
      },
    );
    if (changed) await _changed();
  }

  @override
  Widget build(BuildContext context) {
    final i = i18n;
    final selected = (data?.items ?? [])
        .where((row) => _selected.contains(row.string('catalog_id')))
        .map((row) => row.string('catalog_id'))
        .toList();
    return LayoutBuilder(
      builder: (context, constraints) {
        // An enclosing Scaffold may consume MediaQuery's keyboard inset. The view
        // still exposes it; LayoutBuilder also reacts to that Scaffold resizing.
        final keyboard = View.of(context).viewInsets.bottom > 0;
        return Column(
          children: [
            if (!keyboard)
              Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 4,
                ),
                child: Wrap(
                  spacing: 8,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    FilledButton.icon(
                      onPressed: loading ? null : () => _editor(),
                      icon: const Icon(Icons.add, size: 18),
                      label: Text(i.t('admin:mobile.createEntry')),
                    ),
                    TextButton.icon(
                      label: Text(i.t('admin:mobile.uploadZip')),
                      icon: const Icon(Icons.upload_file_outlined, size: 18),
                      onPressed: loading
                          ? null
                          : () async {
                              await Navigator.push<void>(
                                context,
                                MaterialPageRoute(
                                  builder: (_) => const AdminStoreUploadPage(),
                                ),
                              );
                              if (mounted) await reload();
                            },
                    ),
                  ],
                ),
              ),
            AdminSearchBar(
              // Keep its editing state when the surrounding toolbars collapse.
              key: const ValueKey('admin-store-search'),
              controller: _search,
              hint: i.t('admin-skills:store.search'),
              onSearch: (q) => _apply({..._filters, 'q': q}),
              onFilter: _filter,
              filterCount: adminFilters(_filters).length,
            ),
            if (!keyboard)
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                child: Row(
                  children: [
                    if (_trash)
                      Expanded(
                        child: Align(
                          alignment: Alignment.centerLeft,
                          child: AdminPill(i.t('admin-skills:manage.trash')),
                        ),
                      )
                    else
                      Expanded(
                        child: TextButton.icon(
                          key: const ValueKey('admin-store-select'),
                          style: TextButton.styleFrom(
                            alignment: Alignment.centerLeft,
                          ),
                          onPressed: loading
                              ? null
                              : () => setState(() {
                                  _selecting = !_selecting;
                                  if (!_selecting) _selected.clear();
                                }),
                          icon: Icon(
                            _selecting ? Icons.close : Icons.checklist,
                            size: 18,
                          ),
                          label: Text(
                            i.t(
                              _selecting
                                  ? 'common:action.cancel'
                                  : 'admin:mobile.selectEntries',
                            ),
                          ),
                        ),
                      ),
                    if (_selecting)
                      TextButton(
                        onPressed: loading
                            ? null
                            : () => setState(() {
                                final ids =
                                    data?.items
                                        .map((row) => row.string('catalog_id'))
                                        .toSet() ??
                                    <String>{};
                                if (selected.length == ids.length) {
                                  _selected.clear();
                                } else {
                                  _selected
                                    ..clear()
                                    ..addAll(ids);
                                }
                              }),
                        child: Text(i.t('admin-skills:manage.selectPage')),
                      ),
                    if (!_selecting)
                      TextButton.icon(
                        key: const ValueKey('admin-store-trash'),
                        icon: Icon(
                          _trash
                              ? Icons.storefront_outlined
                              : Icons.delete_outline,
                          size: 18,
                        ),
                        label: Text(
                          i.t(
                            _trash
                                ? 'admin-skills:tab.store'
                                : 'admin-skills:manage.trash',
                          ),
                        ),
                        onPressed: loading
                            ? null
                            : () {
                                setState(() {
                                  _trash = !_trash;
                                  _selecting = false;
                                  _filters.remove('listing');
                                  _offset = 0;
                                  _selected.clear();
                                });
                                unawaited(reload(clear: true));
                              },
                      ),
                  ],
                ),
              ),
            Expanded(
              child: loadable(
                (value) => AdminList(
                  onRefresh: reload,
                  children: [
                    if (_feedback != null) AdminCard(child: Text(_feedback!)),
                    if (value.items.isEmpty)
                      AdminCard(child: Text(i.t('admin-skills:store.empty'))),
                    for (final row in value.items)
                      AdminStoreCard(
                        key: ValueKey(row.string('catalog_id')),
                        row: row,
                        selected: _selected.contains(row.string('catalog_id')),
                        disabled: loading,
                        onSelect: _trash || !_selecting
                            ? null
                            : (selected) => setState(() {
                                if (selected) {
                                  _selected.add(row.string('catalog_id'));
                                } else {
                                  _selected.remove(row.string('catalog_id'));
                                }
                              }),
                        onAction: (action) => _action(row, action),
                      ),
                    AdminPager(
                      total: value.total,
                      offset: _offset,
                      limit: 20,
                      disabled: loading,
                      onChanged: (offset) {
                        setState(() {
                          _offset = offset;
                          _selected.clear();
                        });
                        unawaited(reload(clear: true));
                      },
                    ),
                  ],
                ),
              ),
            ),
            if (selected.isNotEmpty && !_trash)
              AdminActionBar(
                primary: OutlinedButton(
                  onPressed: loading ? null : () => _delete(selected),
                  child: Text(
                    i.t(
                      'admin-skills:manage.deleteSelected',
                      count: selected.length,
                    ),
                    style: TextStyle(color: context.tokens.danger),
                  ),
                ),
              ),
          ],
        );
      },
    );
  }
}
