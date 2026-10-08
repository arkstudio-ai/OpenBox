import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gpt_markdown/gpt_markdown.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../chat/widgets/markdown_view.dart';
import '../models/wiki_models.dart';
import '../utils/wiki_content.dart';

/// A page body, rendered with the chat's Markdown widget (web
/// `WikiMarkdown`): numbered citations open their evidence, `[[links]]`
/// open the topic they name, imported pages' local links resolve only to
/// what may be read now, and nothing else is a link.
class WikiMarkdown extends ConsumerWidget {
  const WikiMarkdown({
    super.key,
    required this.page,
    required this.pages,
    required this.onCitation,
    required this.onPage,
    required this.onImportSource,
  });

  final WikiPage page;

  /// Readable topics in scope, to resolve `[[links]]`.
  final List<WikiSummary> pages;
  final ValueChanged<int> onCitation;
  final ValueChanged<String> onPage;
  final ValueChanged<String> onImportSource;

  static final _external = RegExp(r'^(https?|mailto):', caseSensitive: false);

  /// Where a link leads now, or null for text that only looks like a link.
  String? _target(String url) {
    final href = exchangeHref(url, page);
    if (href == null) return null;
    if (href.startsWith(citationAnchor) ||
        href.startsWith(pageAnchor) ||
        href.startsWith(importAnchor) ||
        _external.hasMatch(href)) {
      return href;
    }
    return null;
  }

  Future<void> _open(String url) async {
    final href = _target(url);
    if (href == null) return;
    if (href.startsWith(citationAnchor)) {
      final index = int.tryParse(href.substring(citationAnchor.length));
      if (index != null) onCitation(index);
    } else if (href.startsWith(pageAnchor)) {
      onPage(Uri.decodeComponent(href.substring(pageAnchor.length)));
    } else if (href.startsWith(importAnchor)) {
      onImportSource(href.substring(importAnchor.length));
    } else {
      final uri = Uri.tryParse(href);
      if (uri != null) {
        await launchUrl(uri, mode: LaunchMode.externalApplication);
      }
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (!page.bodyAvailable) return const SizedBox.shrink();
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final markdown = prepareWikiMarkdown(
      page,
      citations: citationsOf(page),
      pages: pages,
    );
    TextStyle heading(double size, FontWeight weight) => TextStyle(
      fontSize: size,
      height: 1.4,
      fontWeight: weight,
      color: t.ink,
    );
    // Reading sizes, as the web's prose: the page title already leads, so a
    // heading in the text steps down from it instead of competing with it.
    return GptMarkdownTheme(
      gptThemeData: GptMarkdownThemeData(
        brightness: Theme.of(context).brightness,
        h1: heading(FontSizes.xl2, FontWeight.w600),
        h2: heading(FontSizes.xl, FontWeight.w600),
        h3: heading(FontSizes.lg, FontWeight.w500),
        h4: heading(FontSizes.base, FontWeight.w600),
        h5: heading(FontSizes.base, FontWeight.w500),
        h6: heading(FontSizes.sm, FontWeight.w500),
        hrLineColor: t.hair,
        autoAddDividerLineAfterH1: false,
        linkColor: t.a700,
        linkHoverColor: t.a700,
      ),
      child: _body(context, t, i18n, markdown),
    );
  }

  Widget _body(
    BuildContext context,
    BossipTokens t,
    I18nState i18n,
    String markdown,
  ) {
    return MarkdownView(
      markdown,
      onLinkTap: (url, _) => _open(url),
      linkBuilder: (context, label, url, style) {
        final href = _target(url);
        if (href != null && href.startsWith(citationAnchor)) {
          final number =
              (int.tryParse(href.substring(citationAnchor.length)) ?? 0) + 1;
          return Semantics(
            button: true,
            label: i18n.t('wiki:citationNumber', vars: {'number': number}),
            excludeSemantics: true,
            child: Container(
              margin: const EdgeInsets.symmetric(horizontal: 3),
              padding: const EdgeInsets.symmetric(horizontal: 5),
              constraints: const BoxConstraints(minWidth: 22, minHeight: 20),
              decoration: BoxDecoration(
                color: t.a100,
                borderRadius: BorderRadius.circular(Radii.sm),
              ),
              // Sized to the number: inside a line of text an unbounded
              // alignment would stretch the chip across the paragraph.
              child: Align(
                widthFactor: 1,
                heightFactor: 1,
                child: Text(
                  '$number',
                  style: TextStyle(
                    fontSize: FontSizes.xs,
                    height: 1.2,
                    fontWeight: FontWeight.w600,
                    color: t.a700,
                  ),
                ),
              ),
            ),
          );
        }
        if (href == null) {
          // Unreviewed or unresolved: the words stay, the link does not.
          return Tooltip(
            message: i18n.t('wiki:unavailableLink'),
            child: Text(
              label.toPlainText(),
              style: style.copyWith(color: t.n500),
            ),
          );
        }
        return Semantics(
          link: true,
          child: Text(
            label.toPlainText(),
            style: style.copyWith(
              color: t.a700,
              decoration: TextDecoration.underline,
              decorationColor: t.a700,
            ),
          ),
        );
      },
    );
  }
}
