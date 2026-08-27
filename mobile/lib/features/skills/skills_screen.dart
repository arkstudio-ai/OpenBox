import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/skill.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/widgets/toast.dart';
import 'api/skills_api.dart';
import 'utils/group_skills.dart';
import 'widgets/create_publish_sheets.dart';
import 'widgets/install_sheet.dart';
import 'widgets/mine_list.dart';
import 'widgets/skill_group_section.dart';
import 'widgets/skills_toolbar.dart';
import 'widgets/store_list.dart';
import 'widgets/upload_sheet.dart';

/// 技能中心 (web `SkillCenter` + `SkillsRoute`), re-flowed for a phone: the
/// two lists become one scroll under a tab row, and every modal becomes a
/// bottom sheet.
///
/// Skills and MCP servers sit in one place because they are one thing to the
/// person using them: a capability the agent gains. Splitting them across two
/// screens hides the dependency between them, which is exactly the
/// relationship that breaks when it goes unnoticed.
class SkillsScreen extends ConsumerStatefulWidget {
  const SkillsScreen({super.key});

  @override
  ConsumerState<SkillsScreen> createState() => _SkillsScreenState();
}

class _SkillsScreenState extends ConsumerState<SkillsScreen> {
  SkillFilters _filters = const SkillFilters();

  String? get _actionError => ref.read(skillsErrorProvider);

  void _setBusy(bool value) =>
      ref.read(skillsBusyProvider.notifier).state = value;

  void _setError(String? value) =>
      ref.read(skillsErrorProvider.notifier).state = value;

  bool _matches(String query, List<String?> fields) {
    final q = query.trim().toLowerCase();
    if (q.isEmpty) return true;
    return fields.any((f) => (f ?? '').toLowerCase().contains(q));
  }

  void _report(Object error) {
    if (!mounted) return;
    final message = errorText(ref.read(i18nProvider), error);
    _setError(message);
    ref.read(toastProvider.notifier).error(message);
  }

  /// Run one write and refresh every list — an install moves more than one.
  Future<bool> _run(Future<void> Function() action) async {
    _setError(null);
    _setBusy(true);
    try {
      await action();
      if (mounted) bumpSkills(ref);
      return true;
    } catch (error) {
      _report(error);
      return false;
    } finally {
      if (mounted) _setBusy(false);
    }
  }

  SkillsApi get _api => ref.read(skillsApiProvider);

  List<SkillDependency> _unmetFor(InstalledSkill skill) => unmetDependencies(
        skill.requiresMcp,
        ref.read(mcpServersProvider).valueOrNull ?? const [],
        ref.read(skillCatalogProvider).valueOrNull?.mcp ?? const [],
      );

  // ---------------------------------------------------------------- flows

  /// Offer to close a freshly installed skill's gaps straight away.
  ///
  /// Left to the row warning alone, a skill installs "successfully" and then
  /// quietly does not work until someone reads the warning and goes hunting
  /// for the server themselves. Asked here, the install finishes usable.
  Future<bool> _promptForDependencies(InstalledSkill skill) async {
    final deps = _unmetFor(skill);
    if (deps.isEmpty) return false;
    if (!mounted) return false;
    await _showSheet(
      (sheetContext, busy, error) => DependencySheet(
        skillName: skill.name,
        deps: deps,
        busy: busy,
        error: error,
        onConfirm: (selected, env) async {
          final ok = await _resolveDependencies(selected, env);
          if (ok && sheetContext.mounted) Navigator.of(sheetContext).pop();
        },
      ),
    );
    return true;
  }

  /// Install what is missing, reconnect what is merely disconnected.
  Future<bool> _resolveDependencies(
    List<SkillDependency> deps,
    Map<String, Map<String, String>> env,
  ) =>
      _run(() async {
        for (final dep in deps) {
          if (dep.configured) {
            await _api.connectServer(dep.name);
          } else if (dep.catalog != null) {
            final catalog = dep.catalog!;
            await _api.installFromCatalog(
              id: catalog.id,
              kind: 'mcp',
              env: catalog.requiredEnv.isEmpty
                  ? const {}
                  : {catalog.id: env[catalog.id] ?? const {}},
            );
          }
        }
      });

  /// Finish a skill install by asking about what it still needs.
  ///
  /// The freshly installed skill is read back from the list rather than the
  /// install response, because requires_mcp is parsed from the SKILL.md the
  /// sandbox unpacked — the caller never had it.
  Future<void> _finishSkillInstall(Future<void> Function() install) async {
    if (!await _run(install)) return;
    final fresh = await ref.refresh(installedSkillsProvider.future);
    // Both lists have to be current before the gaps are computed: a server
    // installed moments ago must not still read as missing.
    await ref.refresh(mcpServersProvider.future).catchError(
          (Object _) => const <McpServer>[],
        );
    if (!mounted) return;
    for (final skill in fresh.where((s) => s.requiresMcp.isNotEmpty)) {
      if (await _promptForDependencies(skill)) break;
    }
  }

  Future<void> _openInstall(CatalogEntry entry) async {
    final catalog = ref.read(skillCatalogProvider).valueOrNull;
    await _showSheet(
      (sheetContext, busy, error) => InstallSheet(
        entry: entry,
        mcpCatalog: catalog?.mcp ?? const [],
        busy: busy,
        error: error,
        onConfirm: (choice) async {
          final ok = await _run(() => _api.installFromCatalog(
                id: entry.id,
                kind: entry.kind,
                withMcp: choice.withMcp,
                env: choice.env,
              ));
          if (ok && sheetContext.mounted) Navigator.of(sheetContext).pop();
        },
      ),
    );
  }

  Future<void> _openUpload() async {
    await _showSheet(
      (sheetContext, busy, error) => UploadSheet(
        busy: busy,
        error: error,
        onSubmit: (request) async {
          final archive = request.archive;
          final skill = request.skill;
          final mcp = request.mcp;
          if (archive != null) {
            await _finishSkillInstall(() => _api.uploadArchive(
                  filename: archive.name,
                  bytes: archive.bytes,
                  name: request.name?.isEmpty ?? true ? null : request.name,
                ));
          } else if (skill != null) {
            await _finishSkillInstall(() => _api.installSkill(
                  url: skill['url'],
                  name: skill['name'],
                  content: skill['content'],
                ));
          } else if (mcp != null) {
            // Sequential: one pasted config can carry several servers, and
            // each stdio server spawns a process on connect. Firing them
            // together races for the same npx cache.
            await _run(() async {
              for (final entry in mcp) {
                await _api.addServer(entry.name, entry.config);
              }
            });
          }
          if (_actionError == null && sheetContext.mounted) {
            Navigator.of(sheetContext).pop();
          }
        },
      ),
    );
  }

  Future<void> _openCreate() async {
    final projects = await ref.read(skillProjectsProvider.future);
    if (!mounted) return;
    await _showSheet(
      (sheetContext, busy, error) => CreateSkillSheet(
        projects: projects,
        loading: false,
        busy: busy,
        error: error,
        onConfirm: (projectId, brief) async {
          _setError(null);
          _setBusy(true);
          try {
            final session = await _api.createSkillChat(
              projectId: projectId,
              brief: brief,
              prompt: ref
                  .read(i18nProvider)
                  .t('skills:create.prompt', vars: {'brief': brief}),
            );
            if (!sheetContext.mounted) return;
            Navigator.of(sheetContext).pop();
            if (mounted) context.go(Paths.chat(session.id));
          } catch (error) {
            _report(error);
          } finally {
            if (mounted) _setBusy(false);
          }
        },
      ),
    );
  }

  Future<void> _openPublish(SkillGroup group) async {
    await _showSheet(
      (sheetContext, busy, error) => PublishSkillSheet(
        group: group,
        busy: busy,
        error: error,
        onConfirm: () async {
          final ok = await _run(() => _api.publishSkill(group.id));
          if (ok && sheetContext.mounted) Navigator.of(sheetContext).pop();
        },
      ),
    );
  }

  Future<void> _download(String installDir) async {
    final i18n = ref.read(i18nProvider);
    _setBusy(true);
    try {
      final path = await _api.downloadArchive(installDir);
      if (mounted) {
        ref.read(toastProvider.notifier).success(
              path,
              title: i18n.t('skills:action.download'),
            );
      }
    } catch (error) {
      _report(error);
    } finally {
      if (mounted) _setBusy(false);
    }
  }

  /// A pack install unpacks into many skills that share one directory, so
  /// removing it removes all of them. Say the number rather than letting one
  /// tap take eighteen others quietly.
  Future<void> _uninstall(String dir, int count) async {
    if (count > 1) {
      final i18n = ref.read(i18nProvider);
      final confirmed = await showDialog<bool>(
        context: context,
        builder: (dialogContext) => AlertDialog(
          content: Text(
            i18n.t('skills:mine.confirmPack',
                vars: {'name': dir, 'count': count}),
            style: const TextStyle(fontSize: FontSizes.sm),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(dialogContext).pop(false),
              child: Text(i18n.t('skills:common.cancel')),
            ),
            TextButton(
              onPressed: () => Navigator.of(dialogContext).pop(true),
              child: Text(i18n.t('skills:action.uninstall')),
            ),
          ],
        ),
      );
      if (confirmed != true) return;
    }
    await _run(() => _api.uninstallSkill(dir));
  }

  Future<void> _showSheet(
    Widget Function(BuildContext context, bool busy, String? error) builder,
  ) =>
      showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        backgroundColor: context.tokens.card,
        shape: const RoundedRectangleBorder(
          borderRadius:
              BorderRadius.vertical(top: Radius.circular(Radii.xl2)),
        ),
        // A modal route builds once, so busy/error come through a provider —
        // otherwise the confirm button would never say "安装中…" and a
        // failure would never reach the sheet that caused it.
        builder: (sheetContext) => Consumer(
          builder: (context, ref, _) => builder(
            sheetContext,
            ref.watch(skillsBusyProvider),
            ref.watch(skillsErrorProvider),
          ),
        ),
      );

  // ----------------------------------------------------------------- view

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final skills = ref.watch(installedSkillsProvider);
    final servers = ref.watch(mcpServersProvider);
    final catalog = ref.watch(skillCatalogProvider);
    final busy = ref.watch(skillsBusyProvider);
    final actionError = ref.watch(skillsErrorProvider);
    final query = _filters.query;
    final mine = _filters.tab == 'mine';
    final loading = mine
        ? skills.isLoading || servers.isLoading
        : catalog.isLoading;

    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        titleSpacing: 0,
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              i18n.t('skills:page.title'),
              style: TextStyle(
                fontSize: FontSizes.lg,
                fontWeight: FontWeight.w500,
                color: t.ink,
              ),
            ),
            Text(
              i18n.t('skills:page.subtitle'),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
          ],
        ),
      ),
      body: RefreshIndicator(
        onRefresh: () async => bumpSkills(ref),
        child: ListView(
          padding: const EdgeInsets.fromLTRB(14, 8, 14, 24),
          children: [
            SkillsToolbar(
              filters: _filters,
              onChanged: (next) => setState(() => _filters = next),
              onCreateChat: _openCreate,
              onAdd: _openUpload,
            ),
            if (actionError != null)
              Container(
                margin: const EdgeInsets.only(top: 10),
                padding:
                    const EdgeInsets.symmetric(horizontal: 11, vertical: 8),
                decoration: BoxDecoration(
                  color: t.dangerSoft,
                  borderRadius: BorderRadius.circular(Radii.md),
                ),
                child: Text(
                  actionError,
                  style: TextStyle(
                    fontSize: FontSizes.xs,
                    height: 1.6,
                    color: t.danger,
                  ),
                ),
              ),
            const SizedBox(height: 12),
            if (loading)
              for (var i = 0; i < 3; i += 1)
                Container(
                  height: 60,
                  margin: const EdgeInsets.only(bottom: 6),
                  decoration: BoxDecoration(
                    color: t.hairSoft.withValues(alpha: 0.5),
                    borderRadius: BorderRadius.circular(Radii.lg),
                  ),
                )
            else if (mine)
              MineList(
                skills: (skills.valueOrNull ?? const [])
                    .where((s) => _matches(query, [s.name, s.description]))
                    .toList(),
                servers: (servers.valueOrNull ?? const [])
                    .where((s) => _matches(query, [s.name, s.subtitle]))
                    .toList(),
                unmetFor: _unmetFor,
                showSkills: _filters.kind != 'mcp',
                showMcp: _filters.kind != 'skill',
                onBrowseStore: () =>
                    setState(() => _filters = _filters.copyWith(tab: 'store')),
                onConnect: (name) => _run(() => _api.connectServer(name)),
                onDisconnect: (name) => _run(() => _api.disconnectServer(name)),
                onRemoveServer: (name) => _run(() => _api.removeServer(name)),
                actions: SkillGroupActions(
                  busy: busy,
                  uninstall: _uninstall,
                  fixDependencies: (skill) async {
                    _setError(null);
                    if (!await _promptForDependencies(skill)) {
                      // Everything it needs is already connected — refresh so
                      // the stale warning clears rather than sitting there.
                      bumpSkills(ref);
                    }
                  },
                  publish: _openPublish,
                  download: _download,
                ),
              )
            else
              StoreList(
                skills: (catalog.valueOrNull?.skills ?? const [])
                    .where((s) => _matches(
                        query, [s.title, s.description, s.name, s.tags.join(' ')]))
                    .toList(),
                mcp: (catalog.valueOrNull?.mcp ?? const [])
                    .where((s) => _matches(
                        query, [s.title, s.description, s.name, s.tags.join(' ')]))
                    .toList(),
                showSkills: _filters.kind != 'mcp',
                showMcp: _filters.kind != 'skill',
                onInstall: _openInstall,
              ),
          ],
        ),
      ),
    );
  }
}
