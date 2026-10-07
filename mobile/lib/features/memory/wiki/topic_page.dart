import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/download/native_download.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/spinner.dart';
import '../../../shared/widgets/toast.dart';
import '../api/knowledge_api.dart';
import '../models/wiki_models.dart';
import '../state/knowledge_providers.dart';
import '../utils/knowledge_text.dart';
import '../utils/wiki_content.dart';
import '../widgets/knowledge_parts.dart';
import '../widgets/knowledge_sheets.dart';
import 'wiki_editor.dart';
import 'wiki_evidence.dart';
import 'wiki_markdown.dart';

enum _PageAction { edit, export, download }

/// One topic or document page, laid out for reading on a phone (web
/// `WikiReader`): the text first, then related topics and the sources it
/// cites. Opened from the knowledge page, back returns to it as it was.
class TopicPage extends ConsumerStatefulWidget {
  const TopicPage({super.key, required this.pageId, this.projectId = ''});

  final String pageId;

  /// The scope the page was opened from (`?project=`).
  final String projectId;

  @override
  ConsumerState<TopicPage> createState() => _TopicPageState();
}

class _TopicPageState extends ConsumerState<TopicPage> {
  bool _exporting = false;
  bool _downloading = false;
  Object? _downloadError;

  void _back() {
    if (context.canPop()) {
      context.pop();
    } else {
      context.go(Paths.wiki(projectId: widget.projectId));
    }
  }

  void _open(String pageId) =>
      context.push(Paths.wikiPage(pageId, projectId: widget.projectId));

  Future<void> _edit() async {
    final saved = await showWikiEditor(context, widget.pageId);
    if (!saved || !mounted) return;
    ref
        .read(toastProvider.notifier)
        .success(ref.read(i18nProvider).t('wiki:consumer.saved'));
    refreshKnowledge(ref);
  }

  /// The page as Markdown, read fresh so the file says what may be read now.
  Future<void> _export() async {
    final downloads = ref.read(nativeDownloadProvider);
    setState(() => _exporting = true);
    try {
      final page = await ref.refresh(wikiPageProvider(widget.pageId).future);
      if (!page.bodyAvailable) return;
      await downloads.saveBytes(
        bytes: utf8Bytes(wikiExport(page)),
        suggestedName: '${page.slug}.md',
        mimeType: 'text/markdown',
      );
    } catch (error) {
      if (mounted) {
        ref
            .read(toastProvider.notifier)
            .error(errorText(ref.read(i18nProvider), error));
      }
    } finally {
      if (mounted) setState(() => _exporting = false);
    }
  }

  Future<void> _downloadOriginal(WikiDocumentInfo document) async {
    final api = ref.read(knowledgeApiProvider);
    final downloads = ref.read(nativeDownloadProvider);
    setState(() {
      _downloading = true;
      _downloadError = null;
    });
    try {
      final original = await api.original(document.id);
      await downloads.saveBytes(
        bytes: original.bytes,
        suggestedName: original.filename ?? document.filename,
        mimeType: 'application/octet-stream',
      );
    } catch (error) {
      if (mounted) setState(() => _downloadError = error);
    } finally {
      if (mounted) setState(() => _downloading = false);
    }
  }

  /// Editing, exporting and the original file, from the bar's "more".
  Future<void> _more(WikiPage page) async {
    final i18n = ref.read(i18nProvider);
    final document = page.document;
    final action = await showActionSheet<_PageAction>(
      context,
      title: page.title,
      actions: [
        SheetAction(
          key: const ValueKey('topic-edit'),
          value: _PageAction.edit,
          label: i18n.t('wiki:consumer.edit'),
          icon: Icons.edit_outlined,
        ),
        SheetAction(
          key: const ValueKey('topic-export'),
          value: _PageAction.export,
          label: i18n.t('wiki:export'),
          icon: Icons.file_download_outlined,
          enabled: page.bodyAvailable && !_exporting,
        ),
        if (document != null)
          SheetAction(
            key: const ValueKey('topic-download'),
            value: _PageAction.download,
            label: i18n.t('wiki:documents.download'),
            icon: Icons.download_outlined,
            enabled: !_downloading,
          ),
      ],
    );
    if (!mounted) return;
    switch (action) {
      case _PageAction.edit:
        await _edit();
      case _PageAction.export:
        await _export();
      case _PageAction.download:
        await _downloadOriginal(document!);
      case null:
        break;
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final state = ref.watch(wikiPageProvider(widget.pageId));
    final library = ref.watch(
      wikiLibraryProvider((projectId: widget.projectId, query: '')),
    );
    final pages = library.hasError
        ? const <WikiSummary>[]
        : library.current?.items ?? const <WikiSummary>[];
    final page = state.current;
    final missing = state.hasError && isMissing(state.error);
    final Widget body;
    if (missing) {
      body = _Missing(onBack: _back);
    } else if (page != null) {
      body = _article(t, i18n, page, pages);
    } else if (state.hasError) {
      body = ListView(
        padding: const EdgeInsets.all(20),
        children: [
          ErrorNotice(text: errorText(i18n, state.error!)),
          const SizedBox(height: 14),
          Align(
            alignment: Alignment.centerLeft,
            child: KnowledgeButton(
              label: i18n.t('wiki:retry'),
              onPressed: () => ref.invalidate(wikiPageProvider(widget.pageId)),
            ),
          ),
        ],
      );
    } else {
      body = Padding(
        padding: const EdgeInsets.all(20),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Spinner(),
            const SizedBox(width: 8),
            Text(
              i18n.t('wiki:loading'),
              style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
            ),
          ],
        ),
      );
    }
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        titleSpacing: 0,
        leading: BackButton(onPressed: _back),
        title: Text(
          i18n.t('knowledge:title'),
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
        actions: [
          // A retired page has nothing left to edit, export or download.
          if (page != null && !missing && !page.retired)
            IconButton(
              key: const ValueKey('topic-more'),
              tooltip: i18n.t('common:action.more'),
              icon: Icon(Icons.more_horiz, color: t.ink),
              onPressed: () => _more(page),
            ),
          const SizedBox(width: 6),
        ],
      ),
      body: body,
    );
  }

  Widget _article(
    BossipTokens t,
    I18nState i18n,
    WikiPage page,
    List<WikiSummary> pages,
  ) {
    final retired = page.retired;
    final document = page.document;
    final related = relatedPages(page, pages);
    final meta = TextStyle(fontSize: FontSizes.xs, height: 1.5, color: t.n600);
    final updating = !page.bodyAvailable && !retired;
    return ListView(
      key: const ValueKey('topic-article'),
      padding: EdgeInsets.fromLTRB(
        20,
        8,
        20,
        40 + MediaQuery.paddingOf(context).bottom,
      ),
      children: [
        Text(
          page.title,
          style: TextStyle(
            fontSize: FontSizes.xl3,
            height: 1.3,
            fontWeight: FontWeight.w600,
            color: t.ink,
          ),
        ),
        const SizedBox(height: 8),
        // The file it came from, whether it is being rebuilt, and when it
        // last changed: one quiet line.
        Text.rich(
          TextSpan(
            children: [
              if (document != null) ...[
                WidgetSpan(
                  alignment: PlaceholderAlignment.middle,
                  child: Padding(
                    padding: const EdgeInsets.only(right: 4),
                    child: Icon(
                      Icons.description_outlined,
                      size: 13,
                      color: t.n600,
                    ),
                  ),
                ),
                TextSpan(text: document.filename),
              ],
              if (updating) ...[
                if (document != null) const TextSpan(text: ' · '),
                TextSpan(text: i18n.t('wiki:consumer.updating')),
              ],
              if (page.updatedAt != null) ...[
                if (document != null || updating) const TextSpan(text: ' · '),
                TextSpan(text: formatDay(page.updatedAt!, i18n.language)),
              ],
            ],
          ),
          style: meta,
        ),
        if (_downloadError != null)
          Padding(
            padding: const EdgeInsets.only(top: 10),
            child: Text(
              errorText(i18n, _downloadError!),
              style: TextStyle(fontSize: FontSizes.sm, color: t.dangerInk),
            ),
          ),
        if (document != null && document.sections.length > 1) ...[
          const SizedBox(height: 16),
          Semantics(
            label: i18n.t('wiki:documents.sections'),
            container: true,
            child: Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final section in document.sections)
                  ChoiceChip(
                    label: Text(section.title),
                    selected: section.id == page.id,
                    showCheckmark: false,
                    labelStyle: TextStyle(
                      fontSize: FontSizes.sm,
                      color: section.id == page.id ? t.bg : t.n700,
                    ),
                    selectedColor: t.ink,
                    backgroundColor: t.card,
                    side: BorderSide(
                      color: section.id == page.id ? t.ink : t.hair,
                    ),
                    shape: const StadiumBorder(),
                    // Sections are siblings: moving between them keeps one
                    // step back to the list. The one open stays as it is,
                    // drawn as chosen rather than as unavailable.
                    onSelected: (_) {
                      if (section.id == page.id) return;
                      context.pushReplacement(
                        Paths.wikiPage(section.id, projectId: widget.projectId),
                      );
                    },
                  ),
              ],
            ),
          ),
        ],
        const SizedBox(height: 20),
        Divider(height: 1, color: t.hair),
        const SizedBox(height: 22),
        if (!page.bodyAvailable)
          Container(
            key: const ValueKey('topic-unavailable'),
            padding: const EdgeInsets.all(18),
            decoration: BoxDecoration(
              color: t.hairSoft.withValues(alpha: 0.6),
              borderRadius: BorderRadius.circular(Radii.xl),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  i18n.t(
                    retired
                        ? 'wiki:consumer.retiredTitle'
                        : 'wiki:consumer.updating',
                  ),
                  style: TextStyle(
                    fontSize: FontSizes.lg,
                    fontWeight: FontWeight.w500,
                    color: t.ink,
                  ),
                ),
                const SizedBox(height: 6),
                Text(
                  i18n.t(
                    retired
                        ? 'wiki:consumer.retiredHint'
                        : 'wiki:consumer.staleHint',
                  ),
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    height: 1.6,
                    color: t.n600,
                  ),
                ),
              ],
            ),
          )
        else ...[
          WikiMarkdown(
            page: page,
            pages: pages,
            onCitation: (index) => showCitationSheet(context, page, index),
            onPage: _open,
            onImportSource: (id) => showImportSourceSheet(context, page, id),
          ),
          if (related.isNotEmpty) ...[
            const SizedBox(height: 32),
            Divider(height: 1, color: t.hair),
            const SizedBox(height: 18),
            Text(
              i18n.t('wiki:related'),
              style: TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w600,
                color: t.ink,
              ),
            ),
            const SizedBox(height: 2),
            Text(
              i18n.t('wiki:relatedHint'),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
            const SizedBox(height: 10),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final other in related)
                  KnowledgeButton(
                    compact: true,
                    label: other.title,
                    onPressed: () => _open(other.id),
                  ),
              ],
            ),
          ],
          const SizedBox(height: 28),
          Divider(height: 1, color: t.hair),
          const SizedBox(height: 18),
          WikiEvidence(page: page),
        ],
      ],
    );
  }
}

/// A page that is gone says so plainly, and asks for nothing more.
class _Missing extends ConsumerWidget {
  const _Missing({required this.onBack});

  final VoidCallback onBack;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return ListView(
      padding: const EdgeInsets.fromLTRB(20, 24, 20, 24),
      children: [
        Text(
          i18n.t('wiki:consumer.missingTitle'),
          style: TextStyle(
            fontSize: FontSizes.xl,
            fontWeight: FontWeight.w600,
            color: t.ink,
          ),
        ),
        const SizedBox(height: 8),
        Text(
          i18n.t('wiki:consumer.missingHint'),
          style: TextStyle(fontSize: FontSizes.sm, height: 1.6, color: t.n600),
        ),
        const SizedBox(height: 18),
        Align(
          alignment: Alignment.centerLeft,
          child: KnowledgeButton(
            label: i18n.t('wiki:consumer.missingBack'),
            onPressed: onBack,
          ),
        ),
      ],
    );
  }
}
