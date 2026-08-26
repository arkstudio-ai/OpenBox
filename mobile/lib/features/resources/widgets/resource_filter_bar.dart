import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/resource.dart';
import '../api/resources_api.dart';
import '../utils/resource_display.dart';
import 'resource_sheets.dart';

/// The web's left rail (project) and toolbar (source / type / sort / select)
/// folded into two scrollable chip rows — the same two-level "project →
/// who produced it" narrowing, in the width a phone actually has.
class ResourceFilterBar extends ConsumerWidget {
  const ResourceFilterBar({
    super.key,
    required this.query,
    required this.selectedCount,
    required this.canSelect,
    required this.onQueryChanged,
    required this.onSelectAll,
    required this.onClearSelection,
    required this.onDeleteSelected,
  });

  final ResourceQuery query;
  final int selectedCount;
  final bool canSelect;
  final ValueChanged<ResourceQuery> onQueryChanged;
  final VoidCallback onSelectAll;
  final VoidCallback onClearSelection;
  final VoidCallback onDeleteSelected;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final projects = ref.watch(resourceProjectsProvider).valueOrNull ??
        const <(String, String)>[];
    final selecting = selectedCount > 0;

    final projectLabel = switch (query.project) {
      allProjects => i18n.t('resources:scope.allProjects'),
      noProject => i18n.t('resources:scope.unfiled'),
      final id => projects
              .where((p) => p.$1 == id)
              .map((p) => p.$2)
              .firstOrNull ??
          i18n.t('resources:scope.allProjects'),
    };

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        // First level: which project's resources.
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.fromLTRB(12, 2, 12, 0),
          child: Row(
            children: [
              _Chip(
                label: projectLabel,
                icon: Icons.folder_outlined,
                active: query.project != allProjects,
                trailingCaret: true,
                onTap: () async {
                  final picked = await showProjectScopeSheet(context, ref,
                      active: query.project);
                  if (picked != null) {
                    onQueryChanged(query.copyWith(project: picked));
                  }
                },
              ),
              const SizedBox(width: 8),
              Container(width: 1, height: 14, color: t.hair),
              const SizedBox(width: 8),
              // Second level: user input vs model output.
              for (final source in sourceFilters) ...[
                _Chip(
                  label: i18n.t(sourceLabelKey(source)),
                  icon: sourceIcon(source),
                  active: query.source == source,
                  onTap: () => onQueryChanged(query.copyWith(source: source)),
                ),
                const SizedBox(width: 6),
              ],
            ],
          ),
        ),
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.fromLTRB(12, 6, 12, 6),
          child: Row(
            children: [
              _Chip(
                label: selecting
                    ? i18n.t('resources:actions.clearSelection')
                    : i18n.t('resources:actions.selectAll'),
                icon: selecting
                    ? Icons.deselect
                    : Icons.select_all,
                active: false,
                enabled: selecting || canSelect,
                onTap: selecting ? onClearSelection : onSelectAll,
              ),
              const SizedBox(width: 6),
              _Chip(
                label: query.kind == 'all'
                    ? i18n.t('resources:actions.filter')
                    : i18n.t(kindLabelKey(query.kind)),
                icon: Icons.filter_alt_outlined,
                active: query.kind != 'all',
                trailingCaret: true,
                onTap: () async {
                  final picked =
                      await showKindSheet(context, ref, active: query.kind);
                  if (picked != null) {
                    onQueryChanged(query.copyWith(kind: picked));
                  }
                },
              ),
              const SizedBox(width: 6),
              _Chip(
                label: i18n.t(sortLabelKey(query.sort)),
                icon: Icons.swap_vert,
                active: query.sort != 'created',
                trailingCaret: true,
                onTap: () async {
                  final picked =
                      await showSortSheet(context, ref, active: query.sort);
                  if (picked != null) {
                    onQueryChanged(query.copyWith(sort: picked));
                  }
                },
              ),
              if (selecting) ...[
                const SizedBox(width: 6),
                _Chip(
                  label: i18n.t('resources:actions.deleteCount',
                      count: selectedCount),
                  icon: Icons.delete_outline,
                  active: false,
                  danger: true,
                  onTap: onDeleteSelected,
                ),
              ],
            ],
          ),
        ),
      ],
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip({
    required this.label,
    required this.icon,
    required this.active,
    required this.onTap,
    this.enabled = true,
    this.danger = false,
    this.trailingCaret = false,
  });

  final String label;
  final IconData icon;
  final bool active;
  final bool enabled;
  final bool danger;
  final bool trailingCaret;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final foreground = danger ? t.dangerInk : (active ? t.ink : t.n700);
    return Opacity(
      opacity: enabled ? 1 : 0.4,
      child: Material(
        color: active ? t.n200 : Colors.transparent,
        borderRadius: BorderRadius.circular(Radii.full),
        child: InkWell(
          borderRadius: BorderRadius.circular(Radii.full),
          onTap: enabled ? onTap : null,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(icon, size: 13, color: foreground),
                const SizedBox(width: 5),
                Text(
                  label,
                  style: TextStyle(fontSize: FontSizes.xs, color: foreground),
                ),
                if (trailingCaret) ...[
                  const SizedBox(width: 2),
                  Icon(Icons.expand_more, size: 13, color: t.n500),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}
