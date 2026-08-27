import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/skill.dart';
import '../utils/group_skills.dart';
import 'entry_row.dart';
import 'sheet_scaffold.dart';

/// What one install run should do: which servers to bring along, and the
/// credentials collected for them.
class InstallChoice {
  const InstallChoice({required this.withMcp, required this.env});

  final List<String> withMcp;
  final Map<String, Map<String, String>> env;
}

Map<String, Map<String, String>> _collect(
  Map<String, Map<String, TextEditingController>> controllers,
) =>
    {
      for (final server in controllers.entries)
        server.key: {
          for (final field in server.value.entries) field.key: field.value.text,
        },
    };

/// Confirming a store install, and collecting what the entry needs first
/// (web `InstallDialog`).
///
/// Two things have to happen before an install is safe to fire: any MCP
/// servers the skill depends on must be offered (a skill whose server is
/// missing loads and then fails at its first tool call, which reads as a
/// broken skill), and any credentials the server declares must be collected.
class InstallSheet extends ConsumerStatefulWidget {
  const InstallSheet({
    super.key,
    required this.entry,
    required this.mcpCatalog,
    required this.busy,
    required this.error,
    required this.onConfirm,
  });

  final CatalogEntry entry;
  final List<CatalogEntry> mcpCatalog;
  final bool busy;
  final String? error;
  final void Function(InstallChoice choice) onConfirm;

  @override
  ConsumerState<InstallSheet> createState() => _InstallSheetState();
}

class _InstallSheetState extends ConsumerState<InstallSheet> {
  /// Dependencies default to checked: leaving one off is the unusual choice,
  /// and it is the choice that produces a skill that does not work. Held as
  /// the set of *cleared* ids so the default needs no effect to install it.
  final _cleared = <String>{};
  final _env = <String, Map<String, TextEditingController>>{};

  @override
  void dispose() {
    for (final server in _env.values) {
      for (final controller in server.values) {
        controller.dispose();
      }
    }
    super.dispose();
  }

  List<CatalogEntry> get _missing {
    if (widget.entry.kind != 'skill') return const [];
    return [
      for (final id in widget.entry.missingMcp)
        ...widget.mcpCatalog.where((m) => m.id == id),
    ];
  }

  List<String> get _selected =>
      [for (final m in _missing) if (!_cleared.contains(m.id)) m.id];

  /// Every server about to be installed that wants credentials — the target
  /// itself when installing an MCP entry, plus each selected dependency.
  List<CatalogEntry> get _envNeeded {
    final list = <CatalogEntry>[];
    if (widget.entry.kind == 'mcp' && widget.entry.requiredEnv.isNotEmpty) {
      list.add(widget.entry);
    }
    for (final dep in _missing) {
      if (_selected.contains(dep.id) && dep.requiredEnv.isNotEmpty) {
        list.add(dep);
      }
    }
    return list;
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final entry = widget.entry;
    final missing = _missing;
    final envNeeded = _envNeeded;

    return SkillSheet(
      title: entry.title,
      subtitle: entry.description,
      header: EntryIcon(name: entry.title, icon: entry.icon),
      busy: widget.busy,
      error: widget.error,
      canConfirm: !EnvFields.missingRequired(envNeeded, _env),
      confirmLabel: i18n.t(
        widget.busy ? 'skills:install.installing' : 'skills:install.confirm',
      ),
      onConfirm: () => widget.onConfirm(
        InstallChoice(withMcp: _selected, env: _collect(_env)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (missing.isNotEmpty)
            Container(
              margin: const EdgeInsets.only(top: 6),
              padding: const EdgeInsets.all(11),
              decoration: BoxDecoration(
                border: Border.all(color: t.hair),
                borderRadius: BorderRadius.circular(Radii.lg),
                color: t.hairSoft.withValues(alpha: 0.5),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    i18n.t('skills:install.dependsTitle'),
                    style: TextStyle(
                      fontSize: FontSizes.xs,
                      fontWeight: FontWeight.w500,
                      color: t.ink,
                    ),
                  ),
                  const SizedBox(height: 3),
                  Text(
                    i18n.t('skills:install.dependsHint'),
                    style: TextStyle(
                      fontSize: FontSizes.xs,
                      height: 1.6,
                      color: t.n600,
                    ),
                  ),
                  for (final dep in missing)
                    DependencyCheckRow(
                      icon: dep.icon,
                      title: dep.title,
                      subtitle: dep.description,
                      checked: _selected.contains(dep.id),
                      onChanged: (on) => setState(() {
                        on ? _cleared.remove(dep.id) : _cleared.add(dep.id);
                      }),
                    ),
                  if (_selected.length < missing.length)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: Text(
                        i18n.t('skills:install.dependsWarning'),
                        style: TextStyle(
                          fontSize: FontSizes.xs,
                          height: 1.6,
                          color: t.sage,
                        ),
                      ),
                    ),
                ],
              ),
            ),
          EnvFields(servers: envNeeded, controllers: _env),
        ],
      ),
    );
  }
}

/// Resolving a skill's unmet MCP dependencies for the person, rather than
/// telling them to go and do it (web `DependencyDialog`).
class DependencySheet extends ConsumerStatefulWidget {
  const DependencySheet({
    super.key,
    required this.skillName,
    required this.deps,
    required this.busy,
    required this.error,
    required this.onConfirm,
  });

  final String skillName;
  final List<SkillDependency> deps;
  final bool busy;
  final String? error;
  final void Function(
    List<SkillDependency> deps,
    Map<String, Map<String, String>> env,
  ) onConfirm;

  @override
  ConsumerState<DependencySheet> createState() => _DependencySheetState();
}

class _DependencySheetState extends ConsumerState<DependencySheet> {
  final _cleared = <String>{};
  final _env = <String, Map<String, TextEditingController>>{};

  @override
  void dispose() {
    for (final server in _env.values) {
      for (final controller in server.values) {
        controller.dispose();
      }
    }
    super.dispose();
  }

  // A dependency the store does not carry cannot be installed from here; it is
  // listed so the gap is visible, but it is not offered as an action.
  List<SkillDependency> get _actionable =>
      widget.deps.where((d) => d.actionable).toList();

  List<SkillDependency> get _unknown =>
      widget.deps.where((d) => !d.actionable).toList();

  List<SkillDependency> get _selected =>
      [for (final d in _actionable) if (!_cleared.contains(d.name)) d];

  List<CatalogEntry> get _envNeeded => [
        for (final dep in _selected)
          if (dep.catalog != null && dep.catalog!.requiredEnv.isNotEmpty)
            dep.catalog!,
      ];

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final unknown = _unknown;
    final envNeeded = _envNeeded;

    return SkillSheet(
      title: i18n.t('skills:deps.title'),
      subtitle:
          i18n.t('skills:deps.subtitle', vars: {'skill': widget.skillName}),
      busy: widget.busy,
      error: widget.error,
      cancelLabel: i18n.t('skills:deps.later'),
      canConfirm: _selected.isNotEmpty &&
          !EnvFields.missingRequired(envNeeded, _env),
      confirmLabel: i18n
          .t(widget.busy ? 'skills:deps.working' : 'skills:deps.confirm'),
      onConfirm: () => widget.onConfirm(_selected, _collect(_env)),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          for (final dep in _actionable)
            DependencyCheckRow(
              icon: dep.catalog?.icon,
              title: dep.catalog?.title ?? dep.name,
              subtitle: i18n.t(dep.configured
                  ? 'skills:deps.willConnect'
                  : 'skills:deps.willInstall'),
              checked: !_cleared.contains(dep.name),
              onChanged: (on) => setState(() {
                on ? _cleared.remove(dep.name) : _cleared.add(dep.name);
              }),
            ),
          if (unknown.isNotEmpty)
            Container(
              margin: const EdgeInsets.only(top: 12),
              padding:
                  const EdgeInsets.symmetric(horizontal: 11, vertical: 8),
              decoration: BoxDecoration(
                color: t.hairSoft.withValues(alpha: 0.5),
                borderRadius: BorderRadius.circular(Radii.md),
              ),
              child: Text(
                i18n.t('skills:deps.unknown', vars: {
                  'names': unknown.map((d) => d.name).join(', '),
                }),
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  height: 1.6,
                  color: t.n600,
                ),
              ),
            ),
          EnvFields(servers: envNeeded, controllers: _env),
        ],
      ),
    );
  }
}

/// One checkable dependency, shared by both sheets so they read alike.
class DependencyCheckRow extends StatelessWidget {
  const DependencyCheckRow({
    super.key,
    required this.title,
    required this.subtitle,
    required this.checked,
    required this.onChanged,
    this.icon,
  });

  final String title;
  final String subtitle;
  final bool checked;
  final ValueChanged<bool> onChanged;
  final String? icon;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return GestureDetector(
      onTap: () => onChanged(!checked),
      behavior: HitTestBehavior.opaque,
      child: Padding(
        padding: const EdgeInsets.only(top: 8),
        child: Row(
          children: [
            SizedBox(
              width: 22,
              height: 22,
              child: Checkbox(
                value: checked,
                visualDensity: VisualDensity.compact,
                materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                activeColor: t.accent,
                onChanged: (value) => onChanged(value ?? false),
              ),
            ),
            const SizedBox(width: 10),
            EntryIcon(name: title, icon: icon, small: true),
            const SizedBox(width: 9),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
                  ),
                  Text(
                    subtitle,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
