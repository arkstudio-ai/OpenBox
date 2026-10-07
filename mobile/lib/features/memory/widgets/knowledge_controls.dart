import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../models/memory_models.dart';
import '../utils/knowledge_text.dart';
import 'knowledge_sheets.dart';

/// The page title, with the scope in view under it. When there are projects
/// to choose from, the pair is the way to choose (web scope `select`).
class KnowledgeTitle extends ConsumerWidget {
  const KnowledgeTitle({super.key, required this.scope, this.onPickScope});

  /// What is in view: everything, or one project's name.
  final String scope;
  final VoidCallback? onPickScope;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final content = Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          i18n.t('knowledge:title'),
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
        if (scope.isNotEmpty || onPickScope != null)
          Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Flexible(
                child: Text(
                  scope,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
              ),
              if (onPickScope != null) ...[
                const SizedBox(width: 2),
                Icon(Icons.expand_more, size: 15, color: t.n600),
              ],
            ],
          ),
      ],
    );
    if (onPickScope == null) return content;
    return Semantics(
      button: true,
      label: i18n.t('knowledge:scope'),
      child: InkWell(
        key: const ValueKey('knowledge-scope'),
        borderRadius: BorderRadius.circular(Radii.md),
        onTap: onPickScope,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(0, 6, 10, 6),
          child: content,
        ),
      ),
    );
  }
}

/// Everything, or one project (web scope `select`). Resolves with the
/// project id ('' for everything), or null when dismissed.
Future<String?> showScopeSheet(
  BuildContext context,
  I18nState i18n, {
  required List<KnowledgeProject> projects,
  required String projectId,
}) => showActionSheet<String>(
  context,
  title: i18n.t('knowledge:scope'),
  actions: [
    SheetAction(
      value: '',
      label: i18n.t('knowledge:allScopes'),
      icon: Icons.layers_outlined,
      selected: projectId.isEmpty,
    ),
    for (final project in projects)
      SheetAction(
        value: project.id,
        label: project.name,
        icon: Icons.folder_outlined,
        selected: project.id == projectId,
      ),
  ],
);

enum AddChoice { memory, upload }

/// What "+" adds: a memory, or a file when uploading is available.
Future<AddChoice?> showAddSheet(
  BuildContext context,
  I18nState i18n, {
  required bool canUpload,
  required bool uploading,
}) => showActionSheet<AddChoice>(
  context,
  title: i18n.t('wiki:consumer.add'),
  actions: [
    SheetAction(
      key: const ValueKey('knowledge-add-memory'),
      value: AddChoice.memory,
      label: i18n.t('knowledge:addMemory'),
      icon: Icons.edit_note_outlined,
    ),
    if (canUpload)
      SheetAction(
        key: const ValueKey('knowledge-upload'),
        value: AddChoice.upload,
        label: i18n.t(
          uploading ? 'knowledge:uploading' : 'knowledge:uploadFile',
        ),
        icon: Icons.upload_file_outlined,
        enabled: !uploading,
      ),
  ],
);

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
      constraints: const BoxConstraints(minHeight: 40),
      padding: const EdgeInsets.only(left: 10),
      decoration: BoxDecoration(
        color: t.card,
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.md),
      ),
      child: Row(
        children: [
          Icon(Icons.search, size: 18, color: t.n500),
          const SizedBox(width: 6),
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
                  hintMaxLines: 1,
                  hintStyle: TextStyle(fontSize: FontSizes.base, color: t.n500),
                  contentPadding: const EdgeInsets.symmetric(vertical: 10),
                ),
              ),
            ),
          ),
          if (controller.text.isNotEmpty)
            IconButton(
              key: const ValueKey('knowledge-search-clear'),
              tooltip: i18n.t('knowledge:clearSearch'),
              visualDensity: VisualDensity.compact,
              padding: EdgeInsets.zero,
              constraints: const BoxConstraints.tightFor(width: 40, height: 40),
              icon: Icon(Icons.cancel, size: 17, color: t.n500),
              onPressed: () {
                controller.clear();
                onChanged('');
              },
            )
          else
            const SizedBox(width: 10),
        ],
      ),
    );
  }
}

/// 全部 / 记忆 N / 主题 N / 文件 N (web `ViewTabs`): text tabs over a hairline,
/// the one in view underlined.
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
      child: Stack(
        children: [
          Positioned(
            left: 0,
            right: 0,
            bottom: 0,
            child: Container(height: 1, color: t.hair),
          ),
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: Row(
              children: [
                for (final key in knowledgeViews)
                  _Tab(
                    key: ValueKey('knowledge-tab-$key'),
                    label: i18n.t('knowledge:view.$key'),
                    count: counts[key],
                    selected: view == key,
                    onTap: () {
                      FocusManager.instance.primaryFocus?.unfocus();
                      onChanged(key);
                    },
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _Tab extends StatelessWidget {
  const _Tab({
    super.key,
    required this.label,
    required this.count,
    required this.selected,
    required this.onTap,
  });

  final String label;
  final String? count;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Semantics(
      selected: selected,
      button: true,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.only(right: 22),
          child: Container(
            constraints: const BoxConstraints(minHeight: 44),
            decoration: BoxDecoration(
              border: Border(
                bottom: BorderSide(
                  width: 2,
                  color: selected ? t.ink : t.ink.withValues(alpha: 0),
                ),
              ),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  label,
                  style: TextStyle(
                    fontSize: FontSizes.md,
                    fontWeight: selected ? FontWeight.w600 : FontWeight.w400,
                    color: selected ? t.ink : t.n600,
                  ),
                ),
                if (count != null) ...[
                  const SizedBox(width: 4),
                  Text(
                    count!,
                    style: TextStyle(
                      fontSize: FontSizes.sm,
                      color: t.n500,
                      fontFeatures: const [FontFeature.tabularFigures()],
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}
