import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/resource.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/utils/format.dart';
import '../../shared/widgets/toast.dart';
import 'api/resources_api.dart';
import 'utils/resource_display.dart';
import 'widgets/resource_preview.dart';
import 'widgets/resource_sheets.dart';

/// The centre's third column, re-flowed as a pushed page: the meta header the
/// web puts above its preview, then the preview surface itself.
class ResourceDetailPage extends ConsumerStatefulWidget {
  const ResourceDetailPage({super.key, required this.resource});

  final Resource resource;

  @override
  ConsumerState<ResourceDetailPage> createState() => _ResourceDetailPageState();
}

class _ResourceDetailPageState extends ConsumerState<ResourceDetailPage> {
  late Resource _resource = widget.resource;

  Future<void> _download() async {
    try {
      final url =
          await ref.read(resourcesApiProvider).downloadUrl(_resource.id);
      if (url != null) {
        await launchUrl(Uri.parse(url), mode: LaunchMode.externalApplication);
      }
    } catch (e) {
      _reportError(e);
    }
  }

  Future<void> _rename() async {
    final name = await showRenameDialog(context, ref, _resource);
    if (name == null || name.isEmpty || name == _resource.name) return;
    try {
      await ref.read(resourcesApiProvider).rename(_resource.id, name);
      if (!mounted) return;
      bumpResources(ref);
      setState(() => _resource = _copyWithName(_resource, name));
    } catch (e) {
      _reportError(e);
    }
  }

  Future<void> _delete() async {
    if (!await confirmDelete(context, ref, targets: [_resource])) return;
    try {
      await ref.read(resourcesApiProvider).delete(_resource.id);
      if (!mounted) return;
      bumpResources(ref);
      ref
          .read(toastProvider.notifier)
          .info(ref.read(i18nProvider).t('resources:toast.deleted', count: 1));
      Navigator.of(context).pop();
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

    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        titleSpacing: 0,
        title: Text(
          _resource.name,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(
            fontSize: FontSizes.base,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
        actions: [
          IconButton(
            icon: Icon(Icons.drive_file_rename_outline, size: 19, color: t.n700),
            tooltip: i18n.t('resources:actions.rename'),
            onPressed: _rename,
          ),
          IconButton(
            icon: Icon(Icons.file_download_outlined, size: 19, color: t.n700),
            tooltip: i18n.t('resources:actions.download'),
            onPressed: _download,
          ),
          IconButton(
            icon: Icon(Icons.delete_outline, size: 19, color: t.n700),
            tooltip: i18n.t('resources:actions.delete'),
            onPressed: _delete,
          ),
          const SizedBox(width: 4),
        ],
      ),
      body: Column(
        children: [
          _MetaHeader(resource: _resource),
          Expanded(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: ResourcePreview(
                resource: _resource,
                onDownload: _download,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

Resource _copyWithName(Resource resource, String name) => Resource(
      id: resource.id,
      name: name,
      mime: resource.mime,
      size: resource.size,
      kind: resource.kind,
      source: resource.source,
      sandboxPath: resource.sandboxPath,
      url: resource.url,
      projectId: resource.projectId,
      sessionId: resource.sessionId,
      status: resource.status,
      createdAt: resource.createdAt,
    );

/// date | type | size, plus the source badge (web's detail header line).
class _MetaHeader extends ConsumerWidget {
  const _MetaHeader({required this.resource});

  final Resource resource;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final typeLabel = resource.mime.isNotEmpty &&
            resource.mime != 'application/octet-stream'
        ? resource.mime
        : i18n.t(kindLabelKey(resource.kind));
    final created = resource.createdAt;

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 10),
      decoration: BoxDecoration(
        border: Border(bottom: BorderSide(color: t.hair)),
      ),
      child: Wrap(
        spacing: 8,
        runSpacing: 6,
        crossAxisAlignment: WrapCrossAlignment.center,
        children: [
          Text(
            [
              if (created != null) formatDateTime(created, i18n.language),
              typeLabel,
              formatBytes(resource.size),
            ].join('  |  '),
            style: TextStyle(fontSize: FontSizes.xs2, color: t.n600),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
            decoration: BoxDecoration(
              color: t.n200,
              borderRadius: BorderRadius.circular(Radii.md),
            ),
            child: Text(
              i18n.t(sourceLabelKey(resource.source)),
              style: TextStyle(
                fontSize: FontSizes.xs2,
                fontWeight: FontWeight.w500,
                // a700 is the one accent token redefined per colour mode, so
                // this stays legible in dark as well as light.
                color: resource.isAgent ? t.a700 : t.n700,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
