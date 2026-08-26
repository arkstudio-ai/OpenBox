import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/resource.dart';
import '../api/resources_api.dart';
import '../utils/resource_display.dart';

/// The checked-list bottom sheets behind the scope bar and the toolbar. On the
/// web these are dropdown menus; a phone gets the same choices as sheets.

class _Choice {
  const _Choice(this.value, this.label, this.icon);

  final String value;
  final String label;
  final IconData icon;
}

Future<String?> _pick(
  BuildContext context,
  BossipTokens t, {
  required String title,
  required List<_Choice> choices,
  required String active,
}) {
  return showModalBottomSheet<String>(
    context: context,
    builder: (sheetContext) => SafeArea(
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 8),
            child: Text(
              title,
              style: TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w600,
                color: t.n600,
              ),
            ),
          ),
          for (final choice in choices)
            ListTile(
              dense: true,
              leading: Icon(choice.icon, size: 18, color: t.n600),
              title: Text(
                choice.label,
                style: TextStyle(fontSize: FontSizes.base, color: t.ink),
              ),
              trailing: choice.value == active
                  ? Icon(Icons.check, size: 18, color: t.a700)
                  : null,
              onTap: () => Navigator.pop(sheetContext, choice.value),
            ),
        ],
      ),
    ),
  );
}

/// First-level scope: every resource, one project, or the unfiled ones.
Future<String?> showProjectScopeSheet(
  BuildContext context,
  WidgetRef ref, {
  required String active,
}) async {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  final projects =
      ref.read(resourceProjectsProvider).valueOrNull ?? const <(String, String)>[];
  return _pick(
    context,
    t,
    title: i18n.t('resources:scope.title'),
    active: active,
    choices: [
      _Choice(allProjects, i18n.t('resources:scope.allProjects'),
          Icons.layers_outlined),
      for (final (id, name) in projects)
        _Choice(id, name, Icons.folder_outlined),
      _Choice(noProject, i18n.t('resources:scope.unfiled'), Icons.inbox_outlined),
    ],
  );
}

/// Type filter (web's 筛选 dropdown).
Future<String?> showKindSheet(
  BuildContext context,
  WidgetRef ref, {
  required String active,
}) {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  return _pick(
    context,
    t,
    title: i18n.t('resources:actions.filter'),
    active: active,
    choices: [
      for (final kind in kindFilters)
        _Choice(
          kind,
          i18n.t(kindLabelKey(kind)),
          kind == 'all' ? Icons.filter_alt_outlined : kindIcon(kind),
        ),
    ],
  );
}

Future<String?> showSortSheet(
  BuildContext context,
  WidgetRef ref, {
  required String active,
}) {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  return _pick(
    context,
    t,
    title: i18n.t('resources:actions.sort'),
    active: active,
    choices: [
      for (final sort in sortOptions)
        _Choice(sort, i18n.t(sortLabelKey(sort)), Icons.swap_vert),
    ],
  );
}

/// Per-row actions (web's hover buttons + the detail header).
enum ResourceAction { rename, download, delete }

Future<ResourceAction?> showResourceActions(
  BuildContext context,
  WidgetRef ref,
  Resource resource,
) {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  return showModalBottomSheet<ResourceAction>(
    context: context,
    builder: (sheetContext) => SafeArea(
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 8),
            child: Text(
              resource.name,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w600,
                color: t.n600,
              ),
            ),
          ),
          ListTile(
            dense: true,
            leading: Icon(Icons.drive_file_rename_outline,
                size: 18, color: t.n600),
            title: Text(i18n.t('resources:actions.rename'),
                style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
            onTap: () => Navigator.pop(sheetContext, ResourceAction.rename),
          ),
          ListTile(
            dense: true,
            leading:
                Icon(Icons.file_download_outlined, size: 18, color: t.n600),
            title: Text(i18n.t('resources:actions.download'),
                style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
            onTap: () => Navigator.pop(sheetContext, ResourceAction.download),
          ),
          ListTile(
            dense: true,
            leading: Icon(Icons.delete_outline, size: 18, color: t.dangerInk),
            title: Text(i18n.t('resources:actions.delete'),
                style: TextStyle(fontSize: FontSizes.base, color: t.dangerInk)),
            onTap: () => Navigator.pop(sheetContext, ResourceAction.delete),
          ),
        ],
      ),
    ),
  );
}

/// Rename dialog — the web row turns into an inline editor; a phone gets a
/// dialog so the keyboard does not fight the list.
Future<String?> showRenameDialog(
  BuildContext context,
  WidgetRef ref,
  Resource resource,
) {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  final controller = TextEditingController(text: resource.name);
  return showDialog<String>(
    context: context,
    builder: (dialogContext) => AlertDialog(
      backgroundColor: t.card,
      title: Text(i18n.t('resources:actions.rename'),
          style: TextStyle(fontSize: FontSizes.lg, color: t.ink)),
      content: TextField(
        controller: controller,
        autofocus: true,
        style: TextStyle(fontSize: FontSizes.base, color: t.ink),
        decoration: const InputDecoration(isDense: true),
        onSubmitted: (value) => Navigator.pop(dialogContext, value.trim()),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(dialogContext),
          child: Text(i18n.t('common:action.cancel'),
              style: TextStyle(color: t.n700)),
        ),
        TextButton(
          onPressed: () =>
              Navigator.pop(dialogContext, controller.text.trim()),
          child: Text(i18n.t('common:action.save'),
              style: TextStyle(color: t.a700)),
        ),
      ],
    ),
  );
}

/// Delete confirmation — one resource or a selection.
Future<bool> confirmDelete(
  BuildContext context,
  WidgetRef ref, {
  required List<Resource> targets,
}) async {
  if (targets.isEmpty) return false;
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  final body = targets.length == 1
      ? i18n.t('resources:delete.one', vars: {'name': targets.first.name})
      : i18n.t('resources:delete.many', count: targets.length);
  final ok = await showDialog<bool>(
    context: context,
    builder: (dialogContext) => AlertDialog(
      backgroundColor: t.card,
      title: Text(i18n.t('resources:delete.title'),
          style: TextStyle(fontSize: FontSizes.lg, color: t.ink)),
      content: Text(body,
          style: TextStyle(fontSize: FontSizes.base, color: t.n700)),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(dialogContext, false),
          child: Text(i18n.t('common:action.cancel'),
              style: TextStyle(color: t.n700)),
        ),
        TextButton(
          onPressed: () => Navigator.pop(dialogContext, true),
          child: Text(i18n.t('resources:actions.delete'),
              style: TextStyle(color: t.dangerInk)),
        ),
      ],
    ),
  );
  return ok ?? false;
}
