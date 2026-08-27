import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';

/// Which half of the centre is showing, limited to which kind, matching what.
class SkillFilters {
  const SkillFilters({this.tab = 'mine', this.kind = 'all', this.query = ''});

  final String tab; // mine | store
  final String kind; // all | skill | mcp
  final String query;

  SkillFilters copyWith({String? tab, String? kind, String? query}) =>
      SkillFilters(
        tab: tab ?? this.tab,
        kind: kind ?? this.kind,
        query: query ?? this.query,
      );
}

/// Tabs, search and the two add entries (web `SkillCenterToolbar`).
class SkillsToolbar extends ConsumerStatefulWidget {
  const SkillsToolbar({
    super.key,
    required this.filters,
    required this.onChanged,
    required this.onCreateChat,
    required this.onAdd,
  });

  final SkillFilters filters;
  final ValueChanged<SkillFilters> onChanged;
  final VoidCallback onCreateChat;
  final VoidCallback onAdd;

  @override
  ConsumerState<SkillsToolbar> createState() => _SkillsToolbarState();
}

class _SkillsToolbarState extends ConsumerState<SkillsToolbar> {
  final _search = TextEditingController();

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final filters = widget.filters;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            for (final tab in const ['mine', 'store'])
              Padding(
                padding: const EdgeInsets.only(right: 4),
                child: _Chip(
                  label: i18n.t('skills:tab.$tab'),
                  selected: filters.tab == tab,
                  solid: true,
                  onTap: () => widget.onChanged(filters.copyWith(tab: tab)),
                ),
              ),
            const Spacer(),
            IconButton(
              onPressed: widget.onCreateChat,
              icon: const Icon(Icons.chat_bubble_outline, size: 19),
              color: t.n700,
              tooltip: i18n.t('skills:action.createWithChat'),
              visualDensity: VisualDensity.compact,
            ),
            IconButton(
              onPressed: widget.onAdd,
              icon: const Icon(Icons.add, size: 21),
              color: t.n700,
              tooltip: i18n.t('skills:action.add'),
              visualDensity: VisualDensity.compact,
            ),
          ],
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            Expanded(
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 10),
                decoration: BoxDecoration(
                  border: Border.all(color: t.hair),
                  borderRadius: BorderRadius.circular(Radii.lg),
                  color: t.bg,
                ),
                child: Row(
                  children: [
                    Icon(Icons.search, size: 15, color: t.n600),
                    const SizedBox(width: 7),
                    Expanded(
                      child: TextField(
                        controller: _search,
                        onChanged: (value) =>
                            widget.onChanged(filters.copyWith(query: value)),
                        style: TextStyle(
                            fontSize: FontSizes.sm, color: t.ink),
                        decoration: InputDecoration(
                          isDense: true,
                          border: InputBorder.none,
                          hintText: i18n.t('skills:searchPlaceholder'),
                          hintStyle: TextStyle(
                              fontSize: FontSizes.sm, color: t.n600),
                          contentPadding:
                              const EdgeInsets.symmetric(vertical: 10),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(width: 6),
            for (final kind in const ['all', 'skill', 'mcp'])
              Padding(
                padding: const EdgeInsets.only(left: 2),
                child: _Chip(
                  label: i18n.t('skills:filter.$kind'),
                  selected: filters.kind == kind,
                  onTap: () => widget.onChanged(filters.copyWith(kind: kind)),
                ),
              ),
          ],
        ),
      ],
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip({
    required this.label,
    required this.selected,
    required this.onTap,
    this.solid = false,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;
  final bool solid;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final background =
        selected ? (solid ? t.ink : t.hairSoft) : Colors.transparent;
    final foreground = selected ? (solid ? t.bg : t.ink) : t.n600;
    return GestureDetector(
      onTap: onTap,
      child: Container(
        padding: EdgeInsets.symmetric(
          horizontal: solid ? 13 : 9,
          vertical: solid ? 6 : 5,
        ),
        decoration: BoxDecoration(
          color: background,
          borderRadius: BorderRadius.circular(Radii.full),
        ),
        child: Text(
          label,
          style: TextStyle(
            fontSize: solid ? FontSizes.sm : FontSizes.xs,
            color: foreground,
          ),
        ),
      ),
    );
  }
}
