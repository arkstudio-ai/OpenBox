import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../models/wiki_models.dart';
import '../utils/knowledge_text.dart';
import '../utils/wiki_content.dart';

/// What a page's numbered citations quote (web `WikiEvidence`), under the
/// text as the web shows it on a phone. A citation tapped in the text opens
/// the same card in a sheet, so the reading position stays put.
class WikiEvidence extends ConsumerStatefulWidget {
  const WikiEvidence({super.key, required this.page});

  final WikiPage page;

  @override
  ConsumerState<WikiEvidence> createState() => _WikiEvidenceState();
}

class _WikiEvidenceState extends ConsumerState<WikiEvidence> {
  int? _active;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final citations = citationsOf(widget.page);
    final imports = [
      for (final source in widget.page.sourceDetails)
        if (source.kind == 'wiki_import') source,
    ];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          i18n.t('wiki:evidence'),
          style: TextStyle(
            fontSize: FontSizes.md,
            fontWeight: FontWeight.w600,
            color: t.n800,
          ),
        ),
        const SizedBox(height: 2),
        Text(
          i18n.t('wiki:evidenceHint'),
          style: TextStyle(fontSize: FontSizes.xs, height: 1.6, color: t.n500),
        ),
        const SizedBox(height: 10),
        for (var i = 0; i < citations.length; i++)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: EvidenceCard(
              key: ValueKey('wiki-evidence-$i'),
              index: i,
              citation: citations[i],
              source: sourceOf(widget.page, citations[i]),
              expanded: _active == i,
              onToggle: () => setState(() => _active = _active == i ? null : i),
            ),
          ),
        if (citations.isEmpty)
          Text(
            i18n.t('wiki:noEvidence'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n500),
          ),
        for (final source in imports) ImportSourceTile(source: source),
      ],
    );
  }
}

WikiSourceDetail? sourceOf(WikiPage page, WikiCitation citation) => page
    .sourceDetails
    .where((source) => source.id == citation.sourceId)
    .firstOrNull;

/// One citation in a sheet, opened from its number in the text.
Future<void> showCitationSheet(BuildContext context, WikiPage page, int index) {
  final citations = citationsOf(page);
  if (index < 0 || index >= citations.length) return Future.value();
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    showDragHandle: true,
    backgroundColor: context.tokens.card,
    builder: (_) => ConstrainedBox(
      constraints: BoxConstraints(
        maxHeight: MediaQuery.sizeOf(context).height * 0.8,
      ),
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
        child: EvidenceCard(
          index: index,
          citation: citations[index],
          source: sourceOf(page, citations[index]),
          expanded: true,
        ),
      ),
    ),
  );
}

/// An imported source a local link points at, in a sheet.
Future<void> showImportSourceSheet(
  BuildContext context,
  WikiPage page,
  String sourceId,
) {
  final source = page.sourceDetails
      .where((s) => s.id == sourceId && s.kind == 'wiki_import')
      .firstOrNull;
  if (source == null) return Future.value();
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    showDragHandle: true,
    backgroundColor: context.tokens.card,
    builder: (_) => SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
      child: ImportSourceTile(source: source, initiallyExpanded: true),
    ),
  );
}

class EvidenceCard extends ConsumerStatefulWidget {
  const EvidenceCard({
    super.key,
    required this.index,
    required this.citation,
    required this.source,
    required this.expanded,
    this.onToggle,
  });

  final int index;
  final WikiCitation citation;
  final WikiSourceDetail? source;
  final bool expanded;
  final VoidCallback? onToggle;

  @override
  ConsumerState<EvidenceCard> createState() => _EvidenceCardState();
}

class _EvidenceCardState extends ConsumerState<EvidenceCard> {
  bool _original = false;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final source = widget.source;
    final small = TextStyle(fontSize: FontSizes.xs, height: 1.6, color: t.n700);
    final quote = TextStyle(fontSize: FontSizes.sm, height: 1.6, color: t.n800);
    final label = source?.kind == 'verified_memory_revision'
        ? i18n.t('wiki:correctedSource')
        : source?.edited ?? false
        ? i18n.t('wiki:documents.editedSource')
        // Same wording as a memory's sources: where the words came from.
        : tOr(i18n, 'wiki:sourceFrom.${source?.kind}', 'wiki:sourceRevision');
    return Material(
      color: t.card,
      clipBehavior: Clip.antiAlias,
      shape: RoundedRectangleBorder(
        side: BorderSide(
          color: widget.expanded ? t.accent.withValues(alpha: 0.6) : t.hair,
        ),
        borderRadius: BorderRadius.circular(Radii.md),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          // The whole card opens and closes what it quotes.
          Semantics(
            button: widget.onToggle != null,
            expanded: widget.expanded,
            child: InkWell(
              onTap: widget.onToggle,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(12, 10, 12, 4),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Container(
                          width: 18,
                          height: 18,
                          alignment: Alignment.center,
                          decoration: BoxDecoration(
                            color: t.hairSoft,
                            shape: BoxShape.circle,
                          ),
                          child: Text(
                            '${widget.index + 1}',
                            style: TextStyle(
                              fontSize: FontSizes.xs2,
                              color: t.n700,
                              fontFeatures: const [
                                FontFeature.tabularFigures(),
                              ],
                            ),
                          ),
                        ),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            label,
                            style: TextStyle(
                              fontSize: FontSizes.xs,
                              color: t.n600,
                            ),
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 4),
                    for (final text in widget.citation.quotes)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 6),
                        child: Text(text, style: quote),
                      ),
                    if (source?.filename != null)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 6),
                        child: Text(
                          [
                            source!.filename!,
                            if (source.originalPages.isNotEmpty)
                              i18n.t(
                                'wiki:documents.originalPages',
                                vars: {
                                  'pages': source.originalPages.join(', '),
                                },
                              ),
                          ].join(' · '),
                          style: TextStyle(
                            fontSize: FontSizes.xs,
                            color: t.n500,
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ),
          ),
          if (widget.expanded && source != null)
            Container(
              width: double.infinity,
              margin: const EdgeInsets.fromLTRB(12, 0, 12, 6),
              padding: const EdgeInsets.only(top: 6),
              decoration: BoxDecoration(
                border: Border(top: BorderSide(color: t.hair)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  InkWell(
                    key: const ValueKey('wiki-evidence-original'),
                    onTap: () => setState(() => _original = !_original),
                    child: Padding(
                      padding: const EdgeInsets.symmetric(vertical: 4),
                      child: Row(
                        children: [
                          Icon(
                            _original ? Icons.expand_less : Icons.expand_more,
                            size: 16,
                            color: t.n600,
                          ),
                          const SizedBox(width: 4),
                          Text(
                            i18n.t(
                              source.edited
                                  ? 'wiki:documents.currentText'
                                  : 'wiki:sourceOriginal',
                            ),
                            style: TextStyle(
                              fontSize: FontSizes.xs,
                              fontWeight: FontWeight.w500,
                              color: t.ink,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                  if (_original)
                    Padding(
                      padding: const EdgeInsets.only(top: 6, bottom: 6),
                      child: Text(
                        source.body,
                        style: small.copyWith(color: t.n600),
                      ),
                    ),
                  if (source.sessionId != null)
                    _ConversationLink(source.sessionId!),
                  for (final change in source.changes) ...[
                    const SizedBox(height: 8),
                    Text(
                      i18n.t('wiki:correctionOriginal'),
                      style: small.copyWith(fontWeight: FontWeight.w500),
                    ),
                    const SizedBox(height: 4),
                    Text(change.body, style: small),
                    if (change.sessionId != null)
                      _ConversationLink(change.sessionId!),
                  ],
                ],
              ),
            ),
        ],
      ),
    );
  }
}

/// "打开对话" for a source said in a chat.
class _ConversationLink extends ConsumerWidget {
  const _ConversationLink(this.sessionId);

  final String sessionId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    return Semantics(
      link: true,
      child: InkWell(
        onTap: () => context.go(Paths.chat(sessionId)),
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 6),
          child: Text(
            ref.watch(i18nProvider).t('wiki:sourceConversation'),
            style: TextStyle(
              fontSize: FontSizes.xs,
              color: t.a700,
              decoration: TextDecoration.underline,
              decorationColor: t.a700,
            ),
          ),
        ),
      ),
    );
  }
}

/// An imported document a page was built from (web `<details>` per
/// `wiki_import` source).
class ImportSourceTile extends ConsumerWidget {
  const ImportSourceTile({
    super.key,
    required this.source,
    this.initiallyExpanded = false,
  });

  final WikiSourceDetail source;
  final bool initiallyExpanded;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Container(
      margin: const EdgeInsets.only(top: 10),
      decoration: BoxDecoration(
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.md),
      ),
      child: Theme(
        data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
        child: ExpansionTile(
          initiallyExpanded: initiallyExpanded,
          iconColor: t.n600,
          collapsedIconColor: t.n600,
          dense: true,
          tilePadding: const EdgeInsets.symmetric(horizontal: 12),
          childrenPadding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
          expandedCrossAxisAlignment: CrossAxisAlignment.start,
          title: Text(
            [i18n.t('wiki:importSource'), ?source.path].join(' · '),
            style: TextStyle(fontSize: FontSizes.xs, color: t.ink),
          ),
          children: [
            Text(
              source.body,
              style: TextStyle(
                fontSize: FontSizes.xs,
                height: 1.7,
                color: t.n600,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
