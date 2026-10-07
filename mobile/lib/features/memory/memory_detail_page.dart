import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/utils/format.dart';
import '../../shared/widgets/spinner.dart';
import 'models/memory_models.dart';
import 'state/knowledge_providers.dart';
import 'utils/knowledge_text.dart';
import 'widgets/knowledge_parts.dart';
import 'widgets/memory_dialogs.dart';

/// One memory in full (web `MemorySheet`): what it says, where it came from
/// and how it changed. Its text comes from a fresh, re-authorized read on
/// every open, never from the list row; the cleanup status only reports how
/// forgetting is going.
class MemoryDetailPage extends ConsumerStatefulWidget {
  const MemoryDetailPage({
    super.key,
    required this.memory,
    required this.scopeName,
    required this.projectId,
    required this.topics,
    required this.projects,
  });

  final MemoryRecord memory;
  final String scopeName;
  final String projectId;
  final List<MemoryTopic> topics;
  final List<KnowledgeProject> projects;

  @override
  ConsumerState<MemoryDetailPage> createState() => _MemoryDetailPageState();
}

class _MemoryDetailPageState extends ConsumerState<MemoryDetailPage> {
  late MemoryRecord _memory = widget.memory;

  Future<void> _edit() async {
    final saved = await showMemoryEditor(
      context,
      memory: _memory,
      projectId: widget.projectId,
      projects: widget.projects,
    );
    if (saved != null && saved.id == _memory.id && mounted) {
      setState(() => _memory = saved);
    }
  }

  Future<void> _forget() async {
    if (await showForgetDialog(context, _memory) && mounted) {
      setState(() => _memory = _memory.forgotten());
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final id = _memory.id;
    final current = ref.watch(memoryDetailProvider(id));
    final cleanup = ref.watch(memoryCleanupProvider(id));
    final sources = ref.watch(memorySourcesProvider(id));
    final history = ref.watch(memoryHistoryProvider(id));
    // The memory changed since the list was read; continue with the current
    // one, so an edit or a forget names the revision on screen.
    ref.listen(memoryDetailProvider(id), (_, next) {
      final fresh = next.isLoading ? null : next.valueOrNull;
      if (fresh != null &&
          fresh.bodyAvailable == true &&
          fresh.revision != _memory.revision) {
        setState(() => _memory = fresh);
      }
    });

    final stopped = memoryIsStopped(_memory, cleanup.valueOrNull);
    // Forgetting stays possible when the text is no longer readable.
    final forgettable = cleanup.hasValue && !cleanup.isLoading;
    final fresh = current.hasValue && !current.isLoading ? current.value : null;
    final bodyAvailable =
        fresh?.bodyAvailable == true && fresh?.status == 'ACTIVE' && !stopped;
    final error = [
      current,
      cleanup,
      sources,
      history,
    ].where((value) => value.hasError).map((value) => value.error).firstOrNull;
    final topics = [
      for (final topic in widget.topics)
        if (topic.pageId != null) topic,
    ];

    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        titleSpacing: 0,
        leading: IconButton(
          icon: const Icon(Icons.close),
          tooltip: i18n.t('knowledge:detail.close'),
          onPressed: () => Navigator.of(context).maybePop(),
        ),
        title: Text(
          i18n.t('knowledge:detail.title'),
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(20, 8, 20, 24),
        children: [
          if (error != null) ...[
            ErrorNotice(text: errorText(i18n, error)),
            const SizedBox(height: 16),
          ],
          if (stopped)
            _Forgotten(
              cleanup: cleanup.valueOrNull,
              checking: cleanup.isLoading,
              onCheck: () => ref.invalidate(memoryCleanupProvider(id)),
            )
          else if (bodyAvailable)
            Text(
              fresh!.summary,
              key: const ValueKey('memory-detail-text'),
              style: TextStyle(
                fontSize: FontSizes.xl,
                height: 1.6,
                color: t.ink,
              ),
            )
          else if (current.isLoading ||
              (cleanup.isLoading && !cleanup.hasValue))
            const Align(alignment: Alignment.centerLeft, child: Spinner())
          else
            Text(
              i18n.t('knowledge:detail.unavailable'),
              style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
            ),
          const SizedBox(height: 20),
          _Facts(memory: fresh ?? _memory, scopeName: widget.scopeName),
          if (bodyAvailable && topics.isNotEmpty) ...[
            const SizedBox(height: 22),
            _Heading(i18n.t('knowledge:detail.topics')),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final topic in topics)
                  ActionChip(
                    avatar: Icon(
                      Icons.menu_book_outlined,
                      size: 14,
                      color: t.a700,
                    ),
                    label: Text(topic.title),
                    labelStyle: TextStyle(
                      fontSize: FontSizes.sm,
                      color: t.n800,
                    ),
                    side: BorderSide(color: t.hair),
                    backgroundColor: t.card,
                    shape: const StadiumBorder(),
                    onPressed: () => context.push(
                      Paths.wikiPage(
                        topic.pageId!,
                        projectId: widget.projectId,
                      ),
                    ),
                  ),
              ],
            ),
          ],
          if (bodyAvailable) ...[
            const SizedBox(height: 22),
            _Sources(sources: sources),
            const SizedBox(height: 18),
            _History(history: history),
          ],
        ],
      ),
      bottomNavigationBar: stopped
          ? null
          : SafeArea(
              child: Container(
                padding: const EdgeInsets.fromLTRB(20, 10, 20, 10),
                decoration: BoxDecoration(
                  border: Border(top: BorderSide(color: t.hair)),
                ),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: [
                    KnowledgeButton(
                      key: const ValueKey('memory-detail-edit'),
                      icon: Icons.edit_outlined,
                      label: i18n.t('knowledge:memory.edit'),
                      onPressed: bodyAvailable ? _edit : null,
                    ),
                    const SizedBox(width: 8),
                    KnowledgeButton(
                      key: const ValueKey('memory-detail-forget'),
                      icon: Icons.delete_outline,
                      tone: PillTone.danger,
                      label: i18n.t('knowledge:memory.forget'),
                      onPressed: forgettable ? _forget : null,
                    ),
                  ],
                ),
              ),
            ),
    );
  }
}

class _Heading extends StatelessWidget {
  const _Heading(this.text, {this.hint});

  final String text;
  final String? hint;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            text,
            style: TextStyle(
              fontSize: FontSizes.sm,
              fontWeight: FontWeight.w600,
              color: t.ink,
            ),
          ),
          if (hint != null)
            Padding(
              padding: const EdgeInsets.only(top: 2),
              child: Text(
                hint!,
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
            ),
        ],
      ),
    );
  }
}

/// Forgotten: no text, only how the clean-up is going.
class _Forgotten extends ConsumerWidget {
  const _Forgotten({
    required this.cleanup,
    required this.checking,
    required this.onCheck,
  });

  final MemoryCleanup? cleanup;
  final bool checking;
  final VoidCallback onCheck;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final done = cleanup?.status == 'cleaned';
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: t.hairSoft,
        borderRadius: BorderRadius.circular(Radii.xl),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              if (done)
                Icon(Icons.check_circle_outline, size: 16, color: t.sage)
              else
                const Spinner(),
              const SizedBox(width: 8),
              Text(
                i18n.t('knowledge:detail.forgotten'),
                style: TextStyle(
                  fontSize: FontSizes.base,
                  fontWeight: FontWeight.w500,
                  color: t.ink,
                ),
              ),
            ],
          ),
          const SizedBox(height: 6),
          Text(
            i18n.t(
              done
                  ? 'knowledge:detail.cleanupDone'
                  : 'knowledge:detail.cleanupPending',
            ),
            style: TextStyle(
              fontSize: FontSizes.sm,
              height: 1.6,
              color: t.n700,
            ),
          ),
          if (!done) ...[
            const SizedBox(height: 12),
            KnowledgeButton(
              key: const ValueKey('memory-detail-check'),
              compact: true,
              icon: Icons.refresh,
              label: i18n.t('knowledge:detail.checkAgain'),
              onPressed: checking ? null : onCheck,
            ),
          ],
        ],
      ),
    );
  }
}

/// Where a memory lives and when it was saved and last changed.
class _Facts extends ConsumerWidget {
  const _Facts({required this.memory, required this.scopeName});

  final MemoryRecord memory;
  final String scopeName;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final rows = [
      ('knowledge:detail.scope', scopeName),
      if (memory.createdAt != null)
        (
          'knowledge:detail.created',
          formatDateTime(memory.createdAt!, i18n.language),
        ),
      if (memory.updatedAt != null)
        (
          'knowledge:detail.updated',
          formatDateTime(memory.updatedAt!, i18n.language),
        ),
    ].where((row) => row.$2.isNotEmpty);
    return Column(
      children: [
        for (final (label, value) in rows)
          Padding(
            padding: const EdgeInsets.only(bottom: 6),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox(
                  width: 84,
                  child: Text(
                    i18n.t(label),
                    style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                  ),
                ),
                Expanded(
                  child: Text(
                    value,
                    style: TextStyle(fontSize: FontSizes.sm, color: t.n800),
                  ),
                ),
              ],
            ),
          ),
      ],
    );
  }
}

class _Sources extends ConsumerWidget {
  const _Sources({required this.sources});

  final AsyncValue<List<MemorySource>> sources;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final list = sources.isLoading ? null : sources.valueOrNull;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _Heading(
          i18n.t('knowledge:detail.sources'),
          hint: i18n.t('knowledge:detail.sourcesHint'),
        ),
        if (sources.isLoading)
          const Align(alignment: Alignment.centerLeft, child: Spinner())
        else if (list == null || list.isEmpty)
          Text(
            i18n.t('knowledge:detail.noSources'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
          )
        else
          // What backs the memory now first; replaced wording after it.
          for (final source in [
            ...list.where((s) => !s.superseded),
            ...list.where((s) => s.superseded),
          ])
            _SourceItem(source: source),
      ],
    );
  }
}

class _SourceItem extends ConsumerWidget {
  const _SourceItem({required this.source});

  final MemorySource source;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final small = TextStyle(fontSize: FontSizes.xs, color: t.n600);
    final text = TextStyle(fontSize: FontSizes.sm, height: 1.6, color: t.ink);
    final created = source.createdAt;
    final body = source.body;
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.fromLTRB(14, 11, 14, 11),
      decoration: BoxDecoration(
        color: t.hairSoft.withValues(alpha: 0.7),
        borderRadius: BorderRadius.circular(Radii.lg),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  tOr(
                    i18n,
                    'knowledge:detail.sourceKind.${source.kind}',
                    'knowledge:detail.sourceKind.other',
                  ),
                  style: small,
                ),
              ),
              if (created != null)
                Text(formatSince(created, i18n.language), style: small),
            ],
          ),
          const SizedBox(height: 6),
          if (body != null)
            Text(body, style: text)
          else
            Text(
              i18n.t(
                source.superseded
                    ? 'knowledge:detail.supersededSource'
                    : 'knowledge:detail.sourceUnavailable',
              ),
              style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
            ),
          for (final change in source.changes)
            Container(
              margin: const EdgeInsets.only(top: 10),
              padding: const EdgeInsets.only(top: 10),
              decoration: BoxDecoration(
                border: Border(top: BorderSide(color: t.hair)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(i18n.t('knowledge:detail.yourCorrection'), style: small),
                  const SizedBox(height: 4),
                  Text(change.body, style: text),
                  if (change.sessionId != null) _ChatLink(change.sessionId!),
                ],
              ),
            ),
          if (source.sessionId != null) _ChatLink(source.sessionId!),
        ],
      ),
    );
  }
}

/// "打开对话": the conversation a source was said in.
class _ChatLink extends ConsumerWidget {
  const _ChatLink(this.sessionId);

  final String sessionId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: InkWell(
        onTap: () => context.go(Paths.chat(sessionId)),
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 4),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.chat_bubble_outline, size: 12, color: t.a700),
              const SizedBox(width: 4),
              Text(
                ref.watch(i18nProvider).t('knowledge:detail.openChat'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.a700),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _History extends ConsumerWidget {
  const _History({required this.history});

  final AsyncValue<List<MemoryRevision>> history;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final revisions = history.current;
    return Theme(
      data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
      child: ExpansionTile(
        key: const ValueKey('memory-detail-history'),
        iconColor: t.n600,
        collapsedIconColor: t.n600,
        tilePadding: EdgeInsets.zero,
        childrenPadding: const EdgeInsets.only(left: 4, bottom: 8),
        expandedCrossAxisAlignment: CrossAxisAlignment.start,
        title: Text.rich(
          TextSpan(
            children: [
              TextSpan(text: i18n.t('knowledge:detail.history')),
              if (revisions != null)
                TextSpan(
                  text: '  ${revisions.length}',
                  style: TextStyle(fontWeight: FontWeight.w400, color: t.n500),
                ),
            ],
          ),
          style: TextStyle(
            fontSize: FontSizes.sm,
            fontWeight: FontWeight.w600,
            color: t.ink,
          ),
        ),
        children: [
          if (history.isLoading && revisions == null)
            const Spinner()
          else if (revisions == null || revisions.isEmpty)
            Text(
              i18n.t('knowledge:detail.noHistory'),
              style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
            )
          else
            for (final revision in revisions)
              Container(
                width: double.infinity,
                margin: const EdgeInsets.only(bottom: 14),
                padding: const EdgeInsets.only(left: 12),
                decoration: BoxDecoration(
                  border: Border(left: BorderSide(color: t.hair, width: 2)),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      [
                        tOr(
                          i18n,
                          'knowledge:detail.reason.${revision.reason}',
                          'knowledge:detail.reason.other',
                        ),
                        if (revision.createdAt != null)
                          formatDateTime(revision.createdAt!, i18n.language),
                      ].join(' · '),
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                    ),
                    const SizedBox(height: 4),
                    if ((revision.text ?? '').isNotEmpty)
                      Text(
                        revision.text!,
                        style: TextStyle(
                          fontSize: FontSizes.sm,
                          height: 1.6,
                          color: t.n800,
                        ),
                      )
                    else
                      Text(
                        i18n.t('knowledge:detail.unavailable'),
                        style: TextStyle(fontSize: FontSizes.sm, color: t.n500),
                      ),
                  ],
                ),
              ),
        ],
      ),
    );
  }
}
