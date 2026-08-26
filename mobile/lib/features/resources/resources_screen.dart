import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/resource.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/widgets/toast.dart';
import 'api/resources_api.dart';
import 'resource_detail_page.dart';
import 'utils/resource_display.dart';
import 'widgets/resource_filter_bar.dart';
import 'widgets/resource_row.dart';
import 'widgets/resource_sheets.dart';

/// Resource centre (web `ResourceCenter`), re-flowed for a phone: the rail and
/// the list column collapse into one screen with chip filters, and the preview
/// column becomes a pushed page. A view over the OSS ledger, not over a
/// sandbox directory — these files outlive any one container.
class ResourcesScreen extends ConsumerStatefulWidget {
  const ResourcesScreen({super.key, this.initialProject});

  /// Opens on the project the drawer was showing, like the web entry does.
  final String? initialProject;

  @override
  ConsumerState<ResourcesScreen> createState() => _ResourcesScreenState();
}

/// Rows per page. The backend caps a single request at 500.
const _page = 100;

class _ResourcesScreenState extends ConsumerState<ResourcesScreen> {
  late ResourceQuery _query =
      ResourceQuery(project: widget.initialProject ?? allProjects);
  final _search = TextEditingController();
  bool _searching = false;
  final _selected = <String>{};
  final _uploading = <String, double>{};

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  void _setQuery(ResourceQuery next) {
    setState(() {
      // Narrowing the list can hide a checked row, which would leave the
      // delete chip counting things nobody can see.
      _selected.clear();
      _query = next.copyWith(limit: _page);
    });
  }

  Future<void> _pickAndUpload() async {
    final picked = await FilePickerPlatform.instance.pickFiles();
    if (picked.isEmpty) return;
    // The two virtual scopes have no project to file into, so those uploads
    // stay unfiled — the same rule the web centre follows.
    final project = _query.project == allProjects || _query.project == noProject
        ? null
        : _query.project;
    for (final file in picked) {
      setState(() => _uploading[file.name] = 0);
      try {
        final bytes = await file.readAsBytes();
        await ref.read(resourcesApiProvider).upload(
              name: file.name,
              mime: mimeForName(file.name),
              bytes: bytes,
              projectId: project,
              onProgress: (fraction) {
                if (mounted) setState(() => _uploading[file.name] = fraction);
              },
            );
        if (!mounted) return;
        bumpResources(ref);
      } catch (e) {
        _reportError(e);
      } finally {
        if (mounted) setState(() => _uploading.remove(file.name));
      }
    }
  }

  Future<void> _openActions(Resource resource) async {
    final action = await showResourceActions(context, ref, resource);
    if (!mounted || action == null) return;
    switch (action) {
      case ResourceAction.rename:
        final name = await showRenameDialog(context, ref, resource);
        if (name == null || name.isEmpty || name == resource.name) return;
        await _guard(() async {
          await ref.read(resourcesApiProvider).rename(resource.id, name);
          bumpResources(ref);
        });
      case ResourceAction.download:
        await _guard(() async {
          final url =
              await ref.read(resourcesApiProvider).downloadUrl(resource.id);
          if (url != null) {
            await launchUrl(Uri.parse(url),
                mode: LaunchMode.externalApplication);
          }
        });
      case ResourceAction.delete:
        await _deleteMany([resource]);
    }
  }

  Future<void> _deleteMany(List<Resource> targets) async {
    if (!await confirmDelete(context, ref, targets: targets)) return;
    var done = 0;
    for (final target in targets) {
      try {
        await ref.read(resourcesApiProvider).delete(target.id);
        done++;
      } catch (e) {
        _reportError(e);
      }
    }
    if (!mounted) return;
    bumpResources(ref);
    setState(() => _selected.removeAll(targets.map((r) => r.id)));
    if (done > 0) {
      ref
          .read(toastProvider.notifier)
          .info(ref.read(i18nProvider).t('resources:toast.deleted', count: done));
    }
  }

  Future<void> _guard(Future<void> Function() action) async {
    try {
      await action();
    } catch (e) {
      _reportError(e);
    }
  }

  void _reportError(Object error) {
    if (!mounted) return;
    ref
        .read(toastProvider.notifier)
        .error(errorText(ref.read(i18nProvider), error));
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final page = ref.watch(resourceListProvider(_query));
    final items = page.valueOrNull?.items ?? const <Resource>[];

    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        titleSpacing: 0,
        title: _searching
            ? TextField(
                controller: _search,
                autofocus: true,
                style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                decoration: InputDecoration(
                  isDense: true,
                  border: InputBorder.none,
                  hintText: i18n.t('resources:list.searchPlaceholder'),
                  hintStyle:
                      TextStyle(fontSize: FontSizes.base, color: t.n600),
                ),
                onChanged: (value) => _setQuery(_query.copyWith(q: value)),
              )
            : Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    i18n.t('resources:list.title'),
                    style: TextStyle(
                      fontSize: FontSizes.lg,
                      fontWeight: FontWeight.w500,
                      color: t.ink,
                    ),
                  ),
                  Text(
                    i18n.t('workspace:resourceCenterHint'),
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                  ),
                ],
              ),
        actions: [
          IconButton(
            icon: Icon(_searching ? Icons.close : Icons.search,
                size: 20, color: t.n700),
            tooltip: i18n.t('resources:actions.search'),
            onPressed: () => setState(() {
              _searching = !_searching;
              if (!_searching && _search.text.isNotEmpty) {
                _search.clear();
                _setQuery(_query.copyWith(q: ''));
              }
            }),
          ),
          IconButton(
            icon: Icon(Icons.add, size: 22, color: t.n700),
            tooltip: i18n.t('resources:actions.upload'),
            onPressed: _pickAndUpload,
          ),
          const SizedBox(width: 4),
        ],
      ),
      body: Column(
        children: [
          ResourceFilterBar(
            query: _query,
            selectedCount: _selected.length,
            canSelect: items.isNotEmpty,
            onQueryChanged: _setQuery,
            onSelectAll: () =>
                setState(() => _selected.addAll(items.map((r) => r.id))),
            onClearSelection: () => setState(_selected.clear),
            onDeleteSelected: () => _deleteMany(
              items.where((r) => _selected.contains(r.id)).toList(),
            ),
          ),
          Divider(height: 1, color: t.hair),
          Expanded(
            child: RefreshIndicator(
              onRefresh: () async => bumpResources(ref),
              child: page.when(
                loading: () => const Center(
                    child: CircularProgressIndicator(strokeWidth: 2)),
                error: (error, _) => _Message(
                  title: errorText(i18n, error),
                  hint: i18n.t('common:action.retry'),
                ),
                data: (data) => _list(context, i18n, data),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _list(BuildContext context, I18nState i18n, ResourcePage data) {
    final t = context.tokens;
    final items = data.items;
    if (items.isEmpty && _uploading.isEmpty) {
      return _Message(
        title: i18n.t('resources:list.empty'),
        hint: i18n.t('resources:list.emptyHint'),
      );
    }
    return ListView.separated(
      padding: const EdgeInsets.only(bottom: 12),
      itemCount: items.length + _uploading.length + 1,
      separatorBuilder: (_, _) => Divider(height: 1, indent: 58, color: t.hair),
      itemBuilder: (context, index) {
        if (index < _uploading.length) {
          final entry = _uploading.entries.elementAt(index);
          return _UploadRow(name: entry.key, progress: entry.value);
        }
        final offset = index - _uploading.length;
        if (offset == items.length) {
          return _Footer(
            data: data,
            shown: items.length,
            onLoadMore: () => setState(
              () => _query = _query.copyWith(limit: _query.limit + _page),
            ),
          );
        }
        final resource = items[offset];
        return ResourceRow(
          resource: resource,
          selecting: _selected.isNotEmpty,
          checked: _selected.contains(resource.id),
          onTap: () => Navigator.of(context).push(
            MaterialPageRoute<void>(
              builder: (_) => ResourceDetailPage(resource: resource),
            ),
          ),
          onLongPress: () => _openActions(resource),
          onToggle: (on) => setState(() {
            on ? _selected.add(resource.id) : _selected.remove(resource.id);
          }),
        );
      },
    );
  }
}

class _UploadRow extends ConsumerWidget {
  const _UploadRow({required this.name, required this.progress});

  final String name;
  final double progress;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
      child: Row(
        children: [
          SizedBox(
            width: 18,
            height: 18,
            child: CircularProgressIndicator(
              strokeWidth: 2,
              value: progress > 0 ? progress : null,
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Text(
              name,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
            ),
          ),
          Text('${(progress * 100).round()}%',
              style: TextStyle(fontSize: FontSizes.xs2, color: t.n600)),
        ],
      ),
    );
  }
}

/// "Load more (shown / total)" while the server capped the page, and only
/// then "all N loaded" — the count must not claim more than is on screen.
class _Footer extends ConsumerWidget {
  const _Footer({
    required this.data,
    required this.shown,
    required this.onLoadMore,
  });

  final ResourcePage data;
  final int shown;
  final VoidCallback onLoadMore;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    if (!data.hasMore) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 14),
        child: Center(
          child: Text(
            i18n.t('resources:list.allLoaded', count: data.total),
            style: TextStyle(fontSize: FontSizes.xs2, color: t.n600),
          ),
        ),
      );
    }
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Center(
        child: TextButton(
          onPressed: onLoadMore,
          child: Text(
            i18n.t('resources:list.loadMore',
                vars: {'shown': shown, 'total': data.total}),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
          ),
        ),
      ),
    );
  }
}

class _Message extends StatelessWidget {
  const _Message({required this.title, required this.hint});

  final String title;
  final String hint;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return ListView(
      children: [
        const SizedBox(height: 90),
        Center(
          child: Column(
            children: [
              Text(title,
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
              const SizedBox(height: 4),
              Text(hint,
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600)),
            ],
          ),
        ),
      ],
    );
  }
}
