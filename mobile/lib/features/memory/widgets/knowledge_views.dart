import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../models/memory_models.dart';
import '../state/knowledge_model.dart';
import 'file_list.dart';
import 'knowledge_parts.dart';
import 'memory_list.dart';
import 'topic_list.dart';

/// How many of each the overview previews before "see all".
const _preview = 3;

/// What the views can ask the page to do.
class KnowledgeActions {
  const KnowledgeActions({
    required this.setView,
    required this.scopeName,
    required this.openMemory,
    required this.editMemory,
    required this.forgetMemory,
    required this.addMemory,
    required this.openPage,
    required this.upload,
    required this.startChat,
    required this.loadMoreMemories,
    required this.loadMoreTopics,
    required this.loadMoreFiles,
  });

  final ValueChanged<String> setView;
  final String Function(MemoryRecord memory) scopeName;
  final ValueChanged<MemoryRecord> openMemory;
  final ValueChanged<MemoryRecord> editMemory;
  final ValueChanged<MemoryRecord> forgetMemory;
  final VoidCallback addMemory;

  /// A topic or document page, by id.
  final ValueChanged<String> openPage;
  final VoidCallback upload;
  final VoidCallback startChat;
  final VoidCallback loadMoreMemories;
  final VoidCallback loadMoreTopics;
  final VoidCallback loadMoreFiles;
}

/// The body under the tabs (web `ViewBody`).
class KnowledgeBody extends ConsumerWidget {
  const KnowledgeBody({
    super.key,
    required this.view,
    required this.model,
    required this.query,
    required this.canUpload,
    required this.uploading,
    required this.uploadError,
    required this.loadingMore,
    required this.actions,
  });

  final String view;
  final KnowledgeModel model;
  final String query;
  final bool canUpload;
  final bool uploading;
  final String uploadError;

  /// Which lists have a "load more" in flight.
  final ({bool memories, bool topics, bool files}) loadingMore;
  final KnowledgeActions actions;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final overview = view == 'overview';
    if (overview && model.loading) return const SkeletonRows(rows: 4);
    if (overview && model.empty && query.isEmpty && model.error == null) {
      return _Welcome(canUpload: canUpload, actions: actions);
    }
    if (overview && model.empty && query.isNotEmpty && !model.loading) {
      return _NoResults(query: query, onAsk: actions.startChat);
    }
    return switch (view) {
      'memories' => _memoriesView(i18n),
      'topics' => _topicsView(i18n),
      'files' => _filesView(context, i18n),
      _ => _overview(i18n),
    };
  }

  Widget _loadMore(I18nState i18n, bool pending, VoidCallback onPressed) =>
      LoadMoreRow(
        label: i18n.t(pending ? 'knowledge:loading' : 'knowledge:loadMore'),
        pending: pending,
        onPressed: onPressed,
      );

  Widget _memories(
    List<MemoryRecord> memories, {
    Widget? footer,
    bool timeline = false,
  }) => MemoryList(
    memories: memories,
    query: query,
    scopeName: actions.scopeName,
    onOpen: actions.openMemory,
    onEdit: actions.editMemory,
    onForget: actions.forgetMemory,
    footer: footer,
    timeline: timeline,
  );

  Widget _noMemories(I18nState i18n) => EmptyGroup(
    text: i18n.t('knowledge:empty.memories'),
    action: InlineAction(
      icon: Icons.add,
      label: i18n.t('knowledge:addMemory'),
      onPressed: actions.addMemory,
    ),
  );

  Widget _overview(I18nState i18n) {
    final counts = model.counts;
    final seeAll = i18n.t('knowledge:seeAll');
    final sections = <Widget>[
      if (query.isEmpty || model.memoryList.isNotEmpty)
        Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            SectionHeader(
              title: i18n.t('knowledge:section.memories'),
              count: counts['memories'],
              seeAll: seeAll,
              onSeeAll: model.memoryList.length > _preview || model.moreMemories
                  ? () => actions.setView('memories')
                  : null,
            ),
            if (model.memoryList.isNotEmpty)
              _memories(model.memoryList.take(_preview).toList())
            else
              _noMemories(i18n),
          ],
        ),
      if (query.isEmpty || model.topics.isNotEmpty)
        Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            SectionHeader(
              title: i18n.t('knowledge:section.topics'),
              count: counts['topics'],
              seeAll: seeAll,
              onSeeAll: model.topics.length > _preview || model.moreTopics
                  ? () => actions.setView('topics')
                  : null,
            ),
            if (model.topics.isNotEmpty)
              TopicList(
                topics: model.topics.take(_preview).toList(),
                query: query,
                onOpen: (topic) => actions.openPage(topic.id),
              )
            else
              EmptyGroup(text: i18n.t('knowledge:empty.topics')),
          ],
        ),
      if (query.isEmpty || model.files.isNotEmpty)
        Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            SectionHeader(
              title: i18n.t('knowledge:section.files'),
              count: counts['files'],
              seeAll: seeAll,
              onSeeAll: model.files.length > _preview || model.moreFiles
                  ? () => actions.setView('files')
                  : null,
            ),
            if (model.files.isNotEmpty)
              FileList(
                files: model.files.take(_preview).toList(),
                query: query,
                onRead: actions.openPage,
              )
            else
              UploadRow(
                enabled: canUpload && !uploading,
                onChoose: actions.upload,
              ),
          ],
        ),
    ];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (var i = 0; i < sections.length; i++) ...[
          if (i > 0) const SizedBox(height: 22),
          sections[i],
        ],
      ],
    );
  }

  Widget _memoriesView(I18nState i18n) {
    if (model.memoriesPending) return const SkeletonRows();
    final more = model.moreMemories
        ? _loadMore(i18n, loadingMore.memories, actions.loadMoreMemories)
        : null;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (model.memoryList.isNotEmpty)
          // A search lists matches; browsing reads as a timeline of what was
          // learned when.
          _memories(model.memoryList, footer: more, timeline: query.isEmpty)
        else if (query.isNotEmpty)
          _NoResults(query: query, onAsk: actions.startChat)
        else
          _noMemories(i18n),
        if (model.memoryList.isNotEmpty || query.isEmpty)
          GroupFooter(i18n.t('knowledge:section.memoriesHint')),
      ],
    );
  }

  Widget _topicsView(I18nState i18n) {
    if (model.libraryPending) return const SkeletonRows();
    final more = model.moreTopics
        ? _loadMore(i18n, loadingMore.topics, actions.loadMoreTopics)
        : null;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (model.topics.isNotEmpty)
          TopicList(
            topics: model.topics,
            query: query,
            onOpen: (topic) => actions.openPage(topic.id),
            footer: more,
          )
        else if (query.isNotEmpty)
          _NoResults(query: query, onAsk: actions.startChat)
        else
          EmptyGroup(text: i18n.t('knowledge:empty.topics')),
        if (model.topics.isNotEmpty || query.isEmpty)
          GroupFooter(i18n.t('knowledge:section.topicsHint')),
      ],
    );
  }

  Widget _filesView(BuildContext context, I18nState i18n) {
    final t = context.tokens;
    final more = model.moreFiles
        ? _loadMore(i18n, loadingMore.files, actions.loadMoreFiles)
        : null;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        UploadRow(enabled: canUpload && !uploading, onChoose: actions.upload),
        if (uploadError.isNotEmpty) ...[
          const SizedBox(height: 10),
          KnowledgeBanner(
            key: const ValueKey('knowledge-upload-error'),
            tone: BannerTone.danger,
            leading: Icon(Icons.error_outline, size: 17, color: t.dangerInk),
            text: uploadError,
          ),
        ],
        if (model.cleanupPending > 0)
          GroupFooter(
            i18n.t(
              'knowledge:file.cleanupPending',
              vars: {'count': model.cleanupPending},
            ),
          ),
        const SizedBox(height: 20),
        if (model.documentsPending)
          const SkeletonRows(rows: 2)
        else if (model.files.isNotEmpty)
          FileList(
            files: model.files,
            query: query,
            onRead: actions.openPage,
            footer: more,
          )
        else if (query.isNotEmpty)
          _NoResults(query: query, onAsk: actions.startChat),
        if (model.files.isNotEmpty || query.isEmpty)
          GroupFooter(i18n.t('knowledge:section.filesHint')),
      ],
    );
  }
}

/// The first visit: what this page holds and the three ways it fills up.
class _Welcome extends ConsumerWidget {
  const _Welcome({required this.canUpload, required this.actions});

  final bool canUpload;
  final KnowledgeActions actions;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    const steps = [
      ('chat', Icons.chat_bubble_outline),
      ('topics', Icons.auto_awesome_outlined),
      ('files', Icons.upload_file_outlined),
    ];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(4, 0, 4, 16),
          child: Text(
            i18n.t('knowledge:subtitle'),
            style: TextStyle(
              fontSize: FontSizes.sm,
              height: 1.6,
              color: t.n600,
            ),
          ),
        ),
        KnowledgeGroup(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 24, 20, 20),
              child: Column(
                children: [
                  IconTile(
                    icon: Icons.menu_book_outlined,
                    size: 44,
                    background: t.a100,
                    foreground: t.a700,
                  ),
                  const SizedBox(height: 12),
                  Text(
                    i18n.t('knowledge:welcome.title'),
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      fontSize: FontSizes.xl,
                      fontWeight: FontWeight.w600,
                      color: t.ink,
                    ),
                  ),
                  const SizedBox(height: 6),
                  Text(
                    i18n.t('knowledge:welcome.body'),
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      fontSize: FontSizes.sm,
                      height: 1.6,
                      color: t.n600,
                    ),
                  ),
                  const SizedBox(height: 18),
                  for (final (key, icon) in steps)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 14),
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Padding(
                            padding: const EdgeInsets.only(top: 1),
                            child: Icon(icon, size: 18, color: t.n700),
                          ),
                          const SizedBox(width: 12),
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  i18n.t('knowledge:welcome.steps.$key.title'),
                                  style: TextStyle(
                                    fontSize: FontSizes.md,
                                    fontWeight: FontWeight.w500,
                                    color: t.ink,
                                  ),
                                ),
                                const SizedBox(height: 2),
                                Text(
                                  i18n.t('knowledge:welcome.steps.$key.body'),
                                  style: TextStyle(
                                    fontSize: FontSizes.sm,
                                    height: 1.55,
                                    color: t.n600,
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ],
                      ),
                    ),
                  const SizedBox(height: 4),
                  Wrap(
                    alignment: WrapAlignment.center,
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      KnowledgeButton(
                        tone: PillTone.primary,
                        icon: Icons.chat_bubble_outline,
                        label: i18n.t('knowledge:welcome.startChat'),
                        onPressed: actions.startChat,
                      ),
                      KnowledgeButton(
                        icon: Icons.add,
                        label: i18n.t('knowledge:addMemory'),
                        onPressed: actions.addMemory,
                      ),
                      if (canUpload)
                        KnowledgeButton(
                          icon: Icons.upload_outlined,
                          label: i18n.t('knowledge:uploadFile'),
                          onPressed: actions.upload,
                        ),
                    ],
                  ),
                ],
              ),
            ),
          ],
        ),
      ],
    );
  }
}

class _NoResults extends ConsumerWidget {
  const _NoResults({required this.query, required this.onAsk});

  final String query;
  final VoidCallback onAsk;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 32, 16, 24),
      child: Column(
        children: [
          Icon(Icons.search_off, size: 28, color: t.n500),
          const SizedBox(height: 12),
          Text(
            i18n.t('knowledge:empty.searchTitle', vars: {'query': query}),
            textAlign: TextAlign.center,
            style: TextStyle(
              fontSize: FontSizes.base,
              fontWeight: FontWeight.w500,
              color: t.ink,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            i18n.t('knowledge:empty.searchBody'),
            textAlign: TextAlign.center,
            style: TextStyle(
              fontSize: FontSizes.sm,
              height: 1.6,
              color: t.n600,
            ),
          ),
          const SizedBox(height: 14),
          KnowledgeButton(
            compact: true,
            icon: Icons.chat_bubble_outline,
            label: i18n.t('knowledge:empty.askAssistant'),
            onPressed: onAsk,
          ),
        ],
      ),
    );
  }
}
