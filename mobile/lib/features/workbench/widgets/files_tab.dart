import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/widgets/toast.dart';
import '../state/workbench_providers.dart';

/// Files tab (web `FilesTab`/`FilesTree`/`FileViewer`), mobile single-column:
/// breadcrumb + one-level listing; tapping a file swaps to the viewer.
/// Scoped to the session's project workdir — never climbs above it (D.4.7).
class FilesTab extends ConsumerStatefulWidget {
  const FilesTab({
    super.key,
    required this.sessionId,
    required this.containerId,
  });

  final String sessionId;
  final String containerId;

  @override
  ConsumerState<FilesTab> createState() => _FilesTabState();
}

class _FilesTabState extends ConsumerState<FilesTab> {
  String? _cwd;
  String? _openFile;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final workdir = ref.watch(sessionWorkdirProvider(widget.sessionId));

    return workdir.when(
      loading: () =>
          const Center(child: CircularProgressIndicator(strokeWidth: 2)),
      error: (_, _) => Center(
        child: Text(i18n.t('workbench:files.loadFailed'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600)),
      ),
      data: (root) {
        if (root == null || root.isEmpty) {
          return Center(
            child: Text(i18n.t('workbench:sandbox.none'),
                style: TextStyle(fontSize: FontSizes.sm, color: t.n600)),
          );
        }
        final cwd = _cwd ?? root;
        if (_openFile != null) {
          return _FileViewer(
            containerId: widget.containerId,
            path: _openFile!,
            onClose: () => setState(() => _openFile = null),
          );
        }
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            _breadcrumb(t, root, cwd),
            Expanded(child: _listing(t, i18n, cwd)),
          ],
        );
      },
    );
  }

  Widget _breadcrumb(BossipTokens t, String root, String cwd) {
    final rootName = root.split('/').where((s) => s.isNotEmpty).lastOrNull ?? '/';
    final relative = cwd == root
        ? ''
        : cwd.startsWith(root)
            ? cwd.substring(root.length)
            : cwd;
    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 10, 14, 6),
      child: Row(
        children: [
          if (cwd != root)
            InkWell(
              onTap: () => setState(() {
                final parent = cwd.substring(0, cwd.lastIndexOf('/'));
                _cwd = parent.length < root.length ? root : parent;
              }),
              child: Padding(
                padding: const EdgeInsets.only(right: 8),
                child: Icon(Icons.arrow_upward, size: 16, color: t.n600),
              ),
            ),
          Expanded(
            child: Text(
              '$rootName$relative',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontSize: FontSizes.sm,
                color: t.n700,
                fontFamily: 'Menlo',
                fontFamilyFallback: const ['monospace'],
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _listing(BossipTokens t, I18nState i18n, String cwd) {
    final entries = ref.watch(
        fileListProvider((containerId: widget.containerId, path: cwd)));
    return entries.when(
      loading: () =>
          const Center(child: CircularProgressIndicator(strokeWidth: 2)),
      error: (_, _) => Center(
        child: Text(i18n.t('workbench:files.loadFailed'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600)),
      ),
      data: (list) {
        if (list.isEmpty) {
          return Center(
            child: Text(i18n.t('workbench:files.empty'),
                style: TextStyle(fontSize: FontSizes.sm, color: t.n600)),
          );
        }
        final dirs = list.where((e) => e.isDir).toList()
          ..sort((a, b) => a.name.compareTo(b.name));
        final files = list.where((e) => !e.isDir).toList()
          ..sort((a, b) => a.name.compareTo(b.name));
        return ListView(
          padding: const EdgeInsets.symmetric(horizontal: 6),
          children: [
            for (final entry in [...dirs, ...files])
              ListTile(
                dense: true,
                visualDensity: VisualDensity.compact,
                leading: entry.isDir
                    ? Icon(Icons.folder_outlined, size: 18, color: t.n600)
                    : _fileBadge(t, entry.name),
                title: Text(
                  entry.name,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: FontSizes.md,
                    color: t.ink,
                    fontFamily: 'Menlo',
                    fontFamilyFallback: const ['monospace'],
                  ),
                ),
                onTap: () => setState(() {
                  final path = entry.path.isNotEmpty
                      ? entry.path
                      : '$cwd/${entry.name}';
                  if (entry.isDir) {
                    _cwd = path;
                  } else {
                    _openFile = path;
                  }
                }),
              ),
          ],
        );
      },
    );
  }

  Widget _fileBadge(BossipTokens t, String name) {
    final ext = name.contains('.') ? name.split('.').last : '';
    final label = ext.isEmpty ? '·' : ext.toUpperCase();
    return Container(
      width: 30,
      padding: const EdgeInsets.symmetric(vertical: 2),
      alignment: Alignment.center,
      decoration: BoxDecoration(
        color: t.n200,
        borderRadius: BorderRadius.circular(Radii.sm),
      ),
      child: Text(
        label.length > 3 ? label.substring(0, 3) : label,
        style: TextStyle(
          fontSize: FontSizes.xs2,
          fontWeight: FontWeight.w600,
          color: t.n700,
        ),
      ),
    );
  }
}

class _FileViewer extends ConsumerWidget {
  const _FileViewer({
    required this.containerId,
    required this.path,
    required this.onClose,
  });

  final String containerId;
  final String path;
  final VoidCallback onClose;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final content =
        ref.watch(fileContentProvider((containerId: containerId, path: path)));
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(6, 6, 6, 0),
          child: Row(
            children: [
              IconButton(
                icon: Icon(Icons.arrow_back, size: 18, color: t.n700),
                onPressed: onClose,
              ),
              Expanded(
                child: Text(
                  path.split('/').last,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    color: t.ink,
                    fontFamily: 'Menlo',
                    fontFamilyFallback: const ['monospace'],
                  ),
                ),
              ),
              IconButton(
                icon: Icon(Icons.copy_outlined, size: 16, color: t.n600),
                tooltip: i18n.t('workbench:files.copyPath'),
                onPressed: () async {
                  await Clipboard.setData(ClipboardData(text: path));
                  ref
                      .read(toastProvider.notifier)
                      .info(i18n.t('workbench:files.pathCopied'));
                },
              ),
            ],
          ),
        ),
        Expanded(
          child: content.when(
            loading: () =>
                const Center(child: CircularProgressIndicator(strokeWidth: 2)),
            error: (_, _) => Center(
              child: Text(i18n.t('workbench:files.notSupported'),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.n600)),
            ),
            data: (file) {
              if (file.content.contains('\u0000')) {
                return Center(
                  child: Text(i18n.t('workbench:files.binary'),
                      style:
                          TextStyle(fontSize: FontSizes.sm, color: t.n600)),
                );
              }
              return SingleChildScrollView(
                padding: const EdgeInsets.all(14),
                child: SingleChildScrollView(
                  scrollDirection: Axis.horizontal,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      if (file.truncated)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 8),
                          child: Text(
                            i18n.t('workbench:files.tooLarge',
                                vars: {'limit': fileContentLineLimit}),
                            style: TextStyle(
                                fontSize: FontSizes.xs, color: t.n500),
                          ),
                        ),
                      SelectableText(
                        file.content,
                        style: TextStyle(
                          fontSize: FontSizes.xs,
                          height: 1.65,
                          color: t.n800,
                          fontFamily: 'Menlo',
                          fontFamilyFallback: const ['monospace'],
                        ),
                      ),
                    ],
                  ),
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}
