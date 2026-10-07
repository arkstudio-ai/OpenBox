import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../models/memory_models.dart';
import '../utils/knowledge_text.dart';

/// One search over memories, topics and files (web `SearchBar`).
class KnowledgeSearchField extends ConsumerWidget {
  const KnowledgeSearchField({
    super.key,
    required this.controller,
    required this.onChanged,
  });

  final TextEditingController controller;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Container(
      padding: const EdgeInsets.only(left: 12),
      decoration: BoxDecoration(
        color: t.card,
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.xl),
      ),
      child: Row(
        children: [
          Icon(Icons.search, size: 17, color: t.n500),
          const SizedBox(width: 8),
          Expanded(
            child: Semantics(
              label: i18n.t('knowledge:searchLabel'),
              textField: true,
              child: TextField(
                key: const ValueKey('knowledge-search'),
                controller: controller,
                onChanged: onChanged,
                textInputAction: TextInputAction.search,
                inputFormatters: [LengthLimitingTextInputFormatter(200)],
                style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                decoration: InputDecoration(
                  isDense: true,
                  border: InputBorder.none,
                  hintText: i18n.t('knowledge:searchPlaceholder'),
                  hintStyle: TextStyle(fontSize: FontSizes.base, color: t.n500),
                  contentPadding: const EdgeInsets.symmetric(vertical: 13),
                ),
              ),
            ),
          ),
          if (controller.text.isNotEmpty)
            IconButton(
              key: const ValueKey('knowledge-search-clear'),
              tooltip: i18n.t('knowledge:clearSearch'),
              visualDensity: VisualDensity.compact,
              icon: Icon(Icons.close, size: 16, color: t.n500),
              onPressed: () {
                controller.clear();
                onChanged('');
              },
            )
          else
            const SizedBox(width: 12),
        ],
      ),
    );
  }
}

/// Which scope is in view: everything, or one project (web scope `select`).
class KnowledgeScopePicker extends ConsumerWidget {
  const KnowledgeScopePicker({
    super.key,
    required this.projects,
    required this.projectId,
    required this.onChanged,
  });

  final List<KnowledgeProject> projects;
  final String projectId;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final all = i18n.t('knowledge:allScopes');
    final current = projects.where((p) => p.id == projectId).firstOrNull;
    final style = TextStyle(fontSize: FontSizes.base, color: t.ink);
    return PopupMenuButton<String>(
      key: const ValueKey('knowledge-scope'),
      tooltip: i18n.t('knowledge:scope'),
      initialValue: projectId,
      onSelected: onChanged,
      position: PopupMenuPosition.under,
      itemBuilder: (_) => [
        PopupMenuItem(
          value: '',
          child: Text(all, style: style),
        ),
        for (final project in projects)
          PopupMenuItem(
            value: project.id,
            child: Text(
              project.name,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: style,
            ),
          ),
      ],
      child: Container(
        constraints: const BoxConstraints(minHeight: 44),
        padding: const EdgeInsets.symmetric(horizontal: 14),
        decoration: BoxDecoration(
          color: t.card,
          border: Border.all(color: t.hair),
          borderRadius: BorderRadius.circular(Radii.xl),
        ),
        child: Row(
          children: [
            Icon(Icons.folder_outlined, size: 16, color: t.n600),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                current?.name ?? all,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: style,
              ),
            ),
            Icon(Icons.expand_more, size: 18, color: t.n600),
          ],
        ),
      ),
    );
  }
}

/// 全部 / 记忆 N / 主题 N / 文件 N (web `ViewTabs`).
class KnowledgeTabs extends ConsumerWidget {
  const KnowledgeTabs({
    super.key,
    required this.view,
    required this.counts,
    required this.onChanged,
  });

  final String view;
  final Map<String, String?> counts;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Semantics(
      label: i18n.t('knowledge:views'),
      container: true,
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: Row(
          children: [
            for (final key in knowledgeViews)
              Padding(
                padding: const EdgeInsets.only(right: 4),
                child: Semantics(
                  selected: view == key,
                  button: true,
                  child: Material(
                    color: view == key ? t.ink : Colors.transparent,
                    shape: const StadiumBorder(),
                    child: InkWell(
                      key: ValueKey('knowledge-tab-$key'),
                      customBorder: const StadiumBorder(),
                      onTap: () {
                        FocusManager.instance.primaryFocus?.unfocus();
                        onChanged(key);
                      },
                      child: Padding(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 14,
                          vertical: 8,
                        ),
                        child: Row(
                          children: [
                            Text(
                              i18n.t('knowledge:view.$key'),
                              style: TextStyle(
                                fontSize: FontSizes.md,
                                color: view == key ? t.bg : t.n700,
                              ),
                            ),
                            if (counts[key] != null) ...[
                              const SizedBox(width: 6),
                              Text(
                                counts[key]!,
                                style: TextStyle(
                                  fontSize: FontSizes.md,
                                  color: view == key
                                      ? t.bg.withValues(alpha: 0.7)
                                      : t.n500,
                                  fontFeatures: const [
                                    FontFeature.tabularFigures(),
                                  ],
                                ),
                              ),
                            ],
                          ],
                        ),
                      ),
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
