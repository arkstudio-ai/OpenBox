import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/container.dart';
import '../../../shared/widgets/toast.dart';
import '../state/workbench_providers.dart';
import '../utils/project_path.dart';

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
  void didUpdateWidget(covariant FilesTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.sessionId != widget.sessionId ||
        oldWidget.containerId != widget.containerId) {
      _cwd = null;
      _openFile = null;
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final workspace = ref.watch(sessionWorkspaceProvider(widget.sessionId));

    return workspace.when(
      loading: () =>
          const Center(child: CircularProgressIndicator(strokeWidth: 2)),
      error: (_, _) => Center(
        child: Text(
          i18n.t('workbench:files.loadFailed'),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
        ),
      ),
      data: (workspace) {
        final root = workspace.directory;
        if (root == null || root.isEmpty) {
          return Center(
            child: Text(
              i18n.t('workbench:sandbox.none'),
              style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
            ),
          );
        }
        final normalizedRoot = normalizeWorkspacePath(root);
        final rootLabel = workspace.projectName?.trim().isNotEmpty == true
            ? workspace.projectName!.trim()
            : i18n.t('workbench:files.projectRoot');
        final rememberedCwd = _cwd;
        final cwd =
            rememberedCwd != null &&
                isWithinProjectRoot(normalizedRoot, rememberedCwd)
            ? normalizeWorkspacePath(rememberedCwd)
            : normalizedRoot;
        final openFile = _openFile;
        if (openFile != null && isWithinProjectRoot(normalizedRoot, openFile)) {
          return _FileViewer(
            containerId: widget.containerId,
            path: openFile,
            root: normalizedRoot,
            rootLabel: rootLabel,
            onClose: () => setState(() => _openFile = null),
          );
        }
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            _breadcrumb(t, normalizedRoot, rootLabel, cwd),
            Expanded(child: _listing(t, i18n, normalizedRoot, cwd)),
          ],
        );
      },
    );
  }

  Widget _breadcrumb(
    BossipTokens t,
    String root,
    String rootLabel,
    String cwd,
  ) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 10, 14, 6),
      child: Row(
        children: [
          if (cwd != root)
            InkWell(
              onTap: () => setState(() {
                _cwd = projectParentPath(root, cwd);
              }),
              child: Padding(
                padding: const EdgeInsets.only(right: 8),
                child: Icon(Icons.arrow_upward, size: 16, color: t.n600),
              ),
            ),
          Expanded(
            child: Text(
              projectDisplayPath(root: root, path: cwd, projectName: rootLabel),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontSize: FontSizes.sm,
                color: t.n700,
                fontFamily: 'Menlo',
                fontFamilyFallback: const [
                  'PingFang SC',
                  'Noto Sans CJK SC',
                  'monospace',
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _listing(BossipTokens t, I18nState i18n, String root, String cwd) {
    final entries = ref.watch(
      fileListProvider((containerId: widget.containerId, path: cwd)),
    );
    return entries.when(
      loading: () =>
          const Center(child: CircularProgressIndicator(strokeWidth: 2)),
      error: (_, _) => Center(
        child: Text(
          i18n.t('workbench:files.loadFailed'),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
        ),
      ),
      data: (list) {
        final resolved = <({FileEntry entry, String path})>[];
        for (final entry in list) {
          final path = resolveProjectEntryPath(
            root: root,
            cwd: cwd,
            entryPath: entry.path,
            entryName: entry.name,
          );
          if (path != null) resolved.add((entry: entry, path: path));
        }
        if (resolved.isEmpty) {
          return Center(
            child: Text(
              i18n.t('workbench:files.empty'),
              style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
            ),
          );
        }
        final dirs = resolved.where((e) => e.entry.isDir).toList()
          ..sort((a, b) => a.entry.name.compareTo(b.entry.name));
        final files = resolved.where((e) => !e.entry.isDir).toList()
          ..sort((a, b) => a.entry.name.compareTo(b.entry.name));
        return ListView(
          padding: const EdgeInsets.symmetric(horizontal: 6),
          children: [
            for (final resolvedEntry in [...dirs, ...files])
              ListTile(
                dense: true,
                visualDensity: VisualDensity.compact,
                leading: resolvedEntry.entry.isDir
                    ? Icon(Icons.folder_outlined, size: 18, color: t.n600)
                    : _fileBadge(t, resolvedEntry.entry.name),
                title: Text(
                  resolvedEntry.entry.name,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: FontSizes.md,
                    color: t.ink,
                    fontFamily: 'Menlo',
                    fontFamilyFallback: const [
                      'PingFang SC',
                      'Noto Sans CJK SC',
                      'monospace',
                    ],
                  ),
                ),
                onTap: () => setState(() {
                  if (resolvedEntry.entry.isDir) {
                    _cwd = resolvedEntry.path;
                  } else {
                    _openFile = resolvedEntry.path;
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
    required this.root,
    required this.rootLabel,
    required this.onClose,
  });

  final String containerId;
  final String path;
  final String root;
  final String rootLabel;
  final VoidCallback onClose;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final content = ref.watch(
      fileContentProvider((containerId: containerId, path: path)),
    );
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
                  projectDisplayPath(
                    root: root,
                    path: path,
                    projectName: rootLabel,
                  ),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    color: t.ink,
                    fontFamily: 'Menlo',
                    fontFamilyFallback: const [
                      'PingFang SC',
                      'Noto Sans CJK SC',
                      'monospace',
                    ],
                  ),
                ),
              ),
              IconButton(
                icon: Icon(Icons.copy_outlined, size: 16, color: t.n600),
                tooltip: i18n.t('workbench:files.copyPath'),
                onPressed: () async {
                  await Clipboard.setData(
                    ClipboardData(text: projectRelativePath(root, path) ?? '.'),
                  );
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
              child: Text(
                i18n.t('workbench:files.notSupported'),
                style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
              ),
            ),
            data: (file) {
              if (file.content.contains('\u0000')) {
                return Center(
                  child: Text(
                    i18n.t('workbench:files.binary'),
                    style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                  ),
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
                            i18n.t(
                              'workbench:files.tooLarge',
                              vars: {'limit': fileContentLineLimit},
                            ),
                            style: TextStyle(
                              fontSize: FontSizes.xs,
                              color: t.n500,
                            ),
                          ),
                        ),
                      SelectableText(
                        file.content,
                        style: TextStyle(
                          fontSize: FontSizes.xs,
                          height: 1.65,
                          color: t.n800,
                          fontFamily: 'Menlo',
                          fontFamilyFallback: const [
                            'PingFang SC',
                            'Noto Sans CJK SC',
                            'monospace',
                          ],
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
