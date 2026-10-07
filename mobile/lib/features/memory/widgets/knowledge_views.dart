import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../models/memory_models.dart';
import '../models/wiki_models.dart';
import '../state/knowledge_model.dart';
import 'file_list.dart';
import 'knowledge_parts.dart';
import 'memory_list.dart';
import 'topic_cards.dart';

/// How many of each the overview previews before "see all".
const _preview = (memories: 5, topics: 6, files: 4);

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
class KnowledgeBody extends StatelessWidget {
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
  Widget build(BuildContext context) {
    final overview = view == 'overview';
    if (overview && model.loading) return const SkeletonRows(rows: 4);
    if (overview && model.empty && query.isEmpty && model.error == null) {
      return _Welcome(canUpload: canUpload, actions: actions);
    }
    if (overview && model.empty && query.isNotEmpty && !model.loading) {
      return _NoResults(query: query, onAsk: actions.startChat);
    }
    return switch (view) {
      'memories' => _memoriesView(context),
      'topics' => _topicsView(context),
      'files' => _filesView(context),
      _ => _overview(context),
    };
  }

  Widget _memories(List<MemoryRecord> memories) => MemoryList(
    memories: memories,
    query: query,
    topicsOf: model.topicsOf,
    scopeName: actions.scopeName,
    onOpen: actions.openMemory,
    onEdit: actions.editMemory,
    onForget: actions.forgetMemory,
    onTopic: (topic) => actions.openPage(topic.pageId!),
  );

  Widget _topics(List<WikiSummary> topics, {bool rail = false}) => TopicCards(
    topics: topics,
    query: query,
    rail: rail,
    onOpen: (topic) => actions.openPage(topic.id),
  );

  Widget _overview(BuildContext context) => Consumer(
    builder: (context, ref, _) {
      final i18n = ref.watch(i18nProvider);
      final counts = model.counts;
      final seeAll = i18n.t('knowledge:seeAll');
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (query.isEmpty || model.memoryList.isNotEmpty) ...[
            SectionHeader(
              title: i18n.t('knowledge:section.memories'),
              count: counts['memories'],
              seeAll: seeAll,
              onSeeAll:
                  model.memoryList.length > _preview.memories ||
                      model.moreMemories
                  ? () => actions.setView('memories')
                  : null,
            ),
            if (model.memoryList.isNotEmpty)
              _memories(model.memoryList.take(_preview.memories).toList())
            else
              QuietCard(
                text: i18n.t('knowledge:empty.memories'),
                action: KnowledgeButton(
                  compact: true,
                  icon: Icons.add,
                  label: i18n.t('knowledge:addMemory'),
                  onPressed: actions.addMemory,
                ),
              ),
            const SizedBox(height: 30),
          ],
          if (query.isEmpty || model.topics.isNotEmpty) ...[
            SectionHeader(
              title: i18n.t('knowledge:section.topics'),
              count: counts['topics'],
              seeAll: seeAll,
              onSeeAll:
                  model.topics.length > _preview.topics || model.moreTopics
                  ? () => actions.setView('topics')
                  : null,
            ),
            if (model.topics.isNotEmpty)
              _topics(model.topics.take(_preview.topics).toList(), rail: true)
            else
              QuietCard(text: i18n.t('knowledge:empty.topics')),
            const SizedBox(height: 30),
          ],
          if (query.isEmpty || model.files.isNotEmpty) ...[
            SectionHeader(
              title: i18n.t('knowledge:section.files'),
              count: counts['files'],
              seeAll: seeAll,
              onSeeAll: model.files.length > _preview.files || model.moreFiles
                  ? () => actions.setView('files')
                  : null,
            ),
            if (model.files.isNotEmpty)
              FileList(
                files: model.files.take(_preview.files).toList(),
                query: query,
                onRead: actions.openPage,
              )
            else
              UploadCard(
                compact: true,
                enabled: canUpload && !uploading,
                onChoose: actions.upload,
              ),
          ],
        ],
      );
    },
  );

  Widget _memoriesView(BuildContext context) => Consumer(
    builder: (context, ref, _) {
      final i18n = ref.watch(i18nProvider);
      if (model.memoriesPending) return const SkeletonRows();
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          IntroText(i18n.t('knowledge:section.memoriesHint')),
          if (model.memoryList.isNotEmpty)
            _memories(model.memoryList)
          else if (query.isNotEmpty)
            _NoResults(query: query, onAsk: actions.startChat)
          else
            QuietCard(
              text: i18n.t('knowledge:empty.memories'),
              action: KnowledgeButton(
                compact: true,
                tone: PillTone.primary,
                icon: Icons.add,
                label: i18n.t('knowledge:addMemory'),
                onPressed: actions.addMemory,
              ),
            ),
          if (model.moreMemories)
            LoadMoreButton(
              label: i18n.t(
                loadingMore.memories
                    ? 'knowledge:loading'
                    : 'knowledge:loadMore',
              ),
              pending: loadingMore.memories,
              onPressed: actions.loadMoreMemories,
            ),
        ],
      );
    },
  );

  Widget _topicsView(BuildContext context) => Consumer(
    builder: (context, ref, _) {
      final i18n = ref.watch(i18nProvider);
      if (model.libraryPending) return const SkeletonRows();
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          IntroText(i18n.t('knowledge:section.topicsHint')),
          if (model.topics.isNotEmpty)
            _topics(model.topics)
          else if (query.isNotEmpty)
            _NoResults(query: query, onAsk: actions.startChat)
          else
            QuietCard(text: i18n.t('knowledge:empty.topics')),
          if (model.moreTopics)
            LoadMoreButton(
              label: i18n.t(
                loadingMore.topics ? 'knowledge:loading' : 'knowledge:loadMore',
              ),
              pending: loadingMore.topics,
              onPressed: actions.loadMoreTopics,
            ),
        ],
      );
    },
  );

  Widget _filesView(BuildContext context) => Consumer(
    builder: (context, ref, _) {
      final t = context.tokens;
      final i18n = ref.watch(i18nProvider);
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          IntroText(i18n.t('knowledge:section.filesHint')),
          UploadCard(
            enabled: canUpload && !uploading,
            onChoose: actions.upload,
          ),
          if (uploadError.isNotEmpty) ...[
            const SizedBox(height: 12),
            ErrorNotice(
              key: const ValueKey('knowledge-upload-error'),
              text: uploadError,
            ),
          ],
          if (model.cleanupPending > 0)
            Padding(
              padding: const EdgeInsets.only(top: 12),
              child: Text(
                i18n.t(
                  'knowledge:file.cleanupPending',
                  vars: {'count': model.cleanupPending},
                ),
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  height: 1.6,
                  color: t.n600,
                ),
              ),
            ),
          const SizedBox(height: 16),
          if (model.documentsPending)
            const SkeletonRows(rows: 2)
          else if (model.files.isNotEmpty)
            FileList(files: model.files, query: query, onRead: actions.openPage)
          else if (query.isNotEmpty)
            _NoResults(query: query, onAsk: actions.startChat),
          if (model.moreFiles)
            LoadMoreButton(
              label: i18n.t(
                loadingMore.files ? 'knowledge:loading' : 'knowledge:loadMore',
              ),
              pending: loadingMore.files,
              onPressed: actions.loadMoreFiles,
            ),
        ],
      );
    },
  );
}

/// The first visit: what this page will hold and the three ways it fills up.
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
    return KnowledgeCard(
      padding: const EdgeInsets.fromLTRB(20, 28, 20, 24),
      child: Column(
        children: [
          IconTile(
            icon: Icons.menu_book_outlined,
            size: 48,
            background: t.a100,
            foreground: t.a700,
          ),
          const SizedBox(height: 14),
          Text(
            i18n.t('knowledge:welcome.title'),
            textAlign: TextAlign.center,
            style: TextStyle(
              fontSize: FontSizes.xl2,
              fontWeight: FontWeight.w600,
              color: t.ink,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            i18n.t('knowledge:welcome.body'),
            textAlign: TextAlign.center,
            style: TextStyle(
              fontSize: FontSizes.md,
              height: 1.6,
              color: t.n600,
            ),
          ),
          const SizedBox(height: 20),
          for (final (key, icon) in steps)
            Container(
              width: double.infinity,
              margin: const EdgeInsets.only(bottom: 10),
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(
                color: t.hairSoft.withValues(alpha: 0.6),
                borderRadius: BorderRadius.circular(Radii.xl),
              ),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(icon, size: 18, color: t.a700),
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
                        const SizedBox(height: 4),
                        Text(
                          i18n.t('knowledge:welcome.steps.$key.body'),
                          style: TextStyle(
                            fontSize: FontSizes.sm,
                            height: 1.6,
                            color: t.n600,
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          const SizedBox(height: 12),
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
              KnowledgeButton(
                icon: Icons.upload_outlined,
                label: i18n.t('knowledge:uploadFile'),
                onPressed: canUpload ? actions.upload : null,
              ),
            ],
          ),
        ],
      ),
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
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 40),
      child: Column(
        children: [
          IconTile(
            icon: Icons.search_off,
            size: 48,
            background: t.hairSoft,
            foreground: t.n600,
          ),
          const SizedBox(height: 14),
          Text(
            i18n.t('knowledge:empty.searchTitle', vars: {'query': query}),
            textAlign: TextAlign.center,
            style: TextStyle(
              fontSize: FontSizes.lg,
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
          const SizedBox(height: 18),
          KnowledgeButton(
            icon: Icons.chat_bubble_outline,
            label: i18n.t('knowledge:empty.askAssistant'),
            onPressed: onAsk,
          ),
        ],
      ),
    );
  }
}
