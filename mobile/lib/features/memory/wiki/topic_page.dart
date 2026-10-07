import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/download/native_download.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/utils/format.dart';
import '../../../shared/widgets/spinner.dart';
import '../../../shared/widgets/toast.dart';
import '../api/knowledge_api.dart';
import '../models/wiki_models.dart';
import '../state/knowledge_providers.dart';
import '../utils/wiki_content.dart';
import '../widgets/knowledge_parts.dart';
import 'wiki_editor.dart';
import 'wiki_evidence.dart';
import 'wiki_markdown.dart';

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
    final Widget body;
    if (state.hasError && isMissing(state.error)) {
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
    final meta = TextStyle(fontSize: FontSizes.sm, color: t.n600);
    return ListView(
      key: const ValueKey('topic-article'),
      padding: const EdgeInsets.fromLTRB(20, 8, 20, 48),
      children: [
        Text(
          page.title,
          style: TextStyle(
            fontSize: FontSizes.xl3,
            height: 1.25,
            fontWeight: FontWeight.w600,
            color: t.ink,
          ),
        ),
        const SizedBox(height: 10),
        Wrap(
          spacing: 12,
          runSpacing: 4,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            if (document != null)
              Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.description_outlined, size: 14, color: t.n600),
                  const SizedBox(width: 4),
                  Flexible(child: Text(document.filename, style: meta)),
                ],
              ),
            if (!page.bodyAvailable && !retired)
              Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.sync, size: 14, color: t.n600),
                  const SizedBox(width: 4),
                  Text(i18n.t('wiki:consumer.updating'), style: meta),
                ],
              ),
            if (page.updatedAt != null)
              Text(formatDateTime(page.updatedAt!, i18n.language), style: meta),
          ],
        ),
        if (!retired) ...[
          const SizedBox(height: 16),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              KnowledgeButton(
                key: const ValueKey('topic-edit'),
                compact: true,
                icon: Icons.edit_outlined,
                label: i18n.t('wiki:consumer.edit'),
                onPressed: _edit,
              ),
              KnowledgeButton(
                key: const ValueKey('topic-export'),
                compact: true,
                icon: Icons.file_download_outlined,
                label: i18n.t('wiki:export'),
                onPressed: page.bodyAvailable && !_exporting ? _export : null,
              ),
              if (document != null)
                KnowledgeButton(
                  key: const ValueKey('topic-download'),
                  compact: true,
                  icon: Icons.download_outlined,
                  label: i18n.t('wiki:documents.download'),
                  onPressed: _downloading
                      ? null
                      : () => _downloadOriginal(document),
                ),
            ],
          ),
        ],
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
                    // step back to the list.
                    onSelected: section.id == page.id
                        ? null
                        : (_) => context.pushReplacement(
                            Paths.wikiPage(
                              section.id,
                              projectId: widget.projectId,
                            ),
                          ),
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
