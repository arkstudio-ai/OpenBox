import '../models/wiki_models.dart';

/// One cited source and the excerpts a page quotes from it (web
/// `wiki/content.ts`). Distinct excerpts of one source revision share a
/// number.
class WikiCitation {
  WikiCitation({
    required this.sourceId,
    required this.revision,
    required String quote,
  }) : quotes = [quote];

  final String sourceId;
  final int revision;
  final List<String> quotes;
}

int? _revision(Object? value) => switch (value) {
  final num number => number.toInt(),
  final String text => int.tryParse(text.trim()),
  _ => null,
};

List<WikiCitation> citationsOf(WikiPage page) {
  if (!page.bodyAvailable) return const [];
  final result = <WikiCitation>[];
  for (final paragraph in page.paragraphs) {
    final citations = paragraph['citations'];
    if (citations is! List) continue;
    for (final raw in citations) {
      if (raw is! Map) continue;
      final sourceId = raw['source_id'];
      final quote = raw['quote'];
      if (sourceId is! String || quote is! String) continue;
      final revision = _revision(
        raw['revision'] ?? raw['source_revision'] ?? 1,
      );
      if (revision == null) continue;
      final existing = result
          .where((c) => c.sourceId == sourceId && c.revision == revision)
          .firstOrNull;
      if (existing == null) {
        result.add(
          WikiCitation(sourceId: sourceId, revision: revision, quote: quote),
        );
      } else if (!existing.quotes.contains(quote)) {
        existing.quotes.add(quote);
      }
    }
  }
  return result;
}

/// Other readable topics that cite one of the same sources.
List<WikiSummary> relatedPages(WikiPage page, List<WikiSummary> pages) {
  if (!page.bodyAvailable) return const [];
  final sourceIds = {for (final c in citationsOf(page)) c.sourceId};
  return [
    for (final other in pages)
      if (other.id != page.id &&
          other.bodyAvailable &&
          other.sourceIds.any(sourceIds.contains))
        other,
  ];
}

/// The page as a Markdown file to keep: its text, then what it cites.
String wikiExport(WikiPage page) {
  final body = page.body;
  if (!page.bodyAvailable || body == null || body.isEmpty) return '';
  final cited = citationsOf(page)
      .map(
        (cite) =>
            '[source:${cite.sourceId}@${cite.revision}]\n\n'
            '${cite.quotes.map((q) => '> ${q.replaceAll('\n', '\n> ')}').join('\n\n')}',
      )
      .join('\n\n');
  return '$body\n\n---\n\n$cited\n';
}

final _scheme = RegExp(r'^[a-z][a-z\d+.-]*:', caseSensitive: false);

/// Where a link in an imported page leads. Local knowledge-pack paths only
/// resolve to pages or sources the reader may see now; anything else stays
/// inert (null).
String? exchangeHref(String? href, WikiPage page) {
  if (href == null || href.isEmpty) return null;
  final path = page.exchangePath;
  if (path == null ||
      path.isEmpty ||
      href.startsWith('#') ||
      _scheme.hasMatch(href)) {
    return href;
  }
  try {
    final target = Uri.parse('https://wiki.invalid/$path').resolve(href);
    if (target.scheme != 'https' ||
        target.host != 'wiki.invalid' ||
        target.hasPort) {
      return null;
    }
    final link =
        page.exchangeLinks[Uri.decodeComponent(target.path.substring(1))];
    if (link?.pageId != null) return '$pageAnchor${link!.pageId}';
    if (link?.sourceId != null) return '$importAnchor${link!.sourceId}';
  } on FormatException {
    // Malformed or unresolved local file paths remain inert.
  } on ArgumentError {
    // Same: a broken escape is not a link.
  }
  return null;
}

/// Link targets the reader understands.
const citationAnchor = '#wiki-citation-';
const pageAnchor = '#wiki-page-';
const importAnchor = '#wiki-import-source-';

final _marker = RegExp(r'\[source:([^\]@\s]+)@(\d+)\]|\[\[([^\]\n]+)\]\]');
final _citationLink = RegExp(r'\[\d+\]\(#wiki-citation-\d+\)');
final _citationOnly = RegExp(r'^\s*(?:\[\d+\]\(#wiki-citation-\d+\)\s*)+$');
final _fence = RegExp(r'^\s{0,3}(`{3,}|~{3,})');
final _notAttachable = RegExp(
  r'^\s{0,3}(?:#{1,6}\s|\||(?:[-*_]\s*){3,}$|`{3,}|~{3,})',
);

/// The page body made ready for the Markdown widget, the way the web's
/// remark plugins shape it:
///
/// * the leading `# Title` goes (the page header already shows it);
/// * `[source:id@rev]` becomes the citation's number, linking to its
///   evidence — repeated numbers collapse, and a marker that cites nothing
///   on the page disappears rather than showing an internal id;
/// * `[[Topic|label]]` links to a topic when exactly one readable page in the
///   same scope has that title or address, otherwise it is plain text;
/// * raw HTML and remote images never render (images show their alt text);
/// * a citation compiled onto its own line joins the end of the text it
///   supports.
///
/// Code blocks and inline code are left exactly as written.
String prepareWikiMarkdown(
  WikiPage page, {
  required List<WikiCitation> citations,
  required List<WikiSummary> pages,
}) {
  final body = (page.body ?? '').replaceFirst(RegExp(r'^# [^\n]*\n+'), '');
  final lines = body.split('\n');
  final code = List<bool>.filled(lines.length, false);
  String? fence;
  for (var i = 0; i < lines.length; i++) {
    final match = _fence.firstMatch(lines[i]);
    if (fence != null) {
      code[i] = true;
      if (match != null && match[1]!.startsWith(fence)) fence = null;
    } else if (match != null) {
      code[i] = true;
      fence = match[1]![0] * 3;
    }
  }

  // Inline transforms over each run of prose lines.
  final out = <String>[];
  final isCode = <bool>[];
  var start = 0;
  void flushProse(int end) {
    if (end <= start) return;
    final prose = _transform(
      lines.sublist(start, end).join('\n'),
      page,
      citations,
      pages,
    );
    for (final line in prose.split('\n')) {
      out.add(line);
      isCode.add(false);
    }
  }

  for (var i = 0; i < lines.length; i++) {
    if (!code[i]) continue;
    flushProse(i);
    out.add(lines[i]);
    isCode.add(true);
    start = i + 1;
  }
  flushProse(lines.length);

  // Attach lone citation lines to the text before them.
  final result = <String>[];
  final resultCode = <bool>[];
  for (var i = 0; i < out.length; i++) {
    final line = out[i];
    if (!isCode[i] && _citationOnly.hasMatch(line)) {
      var target = result.length - 1;
      while (target >= 0 &&
          !resultCode[target] &&
          result[target].trim().isEmpty) {
        target--;
      }
      if (target >= 0 &&
          !resultCode[target] &&
          !_notAttachable.hasMatch(result[target])) {
        result[target] =
            '${result[target].trimRight()} '
            '${_citationLink.allMatches(line).map((m) => m[0]).join(' ')}';
        // The blank lines that set the lone citation apart go with it.
        result.removeRange(target + 1, result.length);
        resultCode.removeRange(target + 1, resultCode.length);
        continue;
      }
    }
    // Keep at most one blank line between prose blocks.
    if (!isCode[i] &&
        line.trim().isEmpty &&
        result.isNotEmpty &&
        !resultCode.last &&
        result.last.trim().isEmpty) {
      continue;
    }
    result.add(line);
    resultCode.add(isCode[i]);
  }
  return result.join('\n').trim();
}

String _transform(
  String prose,
  WikiPage page,
  List<WikiCitation> citations,
  List<WikiSummary> pages,
) {
  // Inline code stays literal: split it out, transform only the rest.
  final buffer = StringBuffer();
  var cursor = 0;
  for (final span in RegExp(r'(`+)[^`]*?\1').allMatches(prose)) {
    buffer
      ..write(
        _markers(
          _html(prose.substring(cursor, span.start)),
          page,
          citations,
          pages,
        ),
      )
      ..write(span[0]);
    cursor = span.end;
  }
  buffer.write(
    _markers(_html(prose.substring(cursor)), page, citations, pages),
  );
  return buffer.toString();
}

/// Raw HTML never renders; images show only their description.
String _html(String text) => text
    .replaceAll(
      RegExp(
        r'<(script|style|iframe|textarea)\b[^>]*>[\s\S]*?</\1\s*>',
        caseSensitive: false,
      ),
      '',
    )
    .replaceAll(RegExp(r'<!--[\s\S]*?-->'), '')
    .replaceAll(RegExp(r'</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>'), '')
    .replaceAllMapped(RegExp(r'!\[([^\]]*)\]\([^)]*\)'), (m) => m[1]!);

String _markers(
  String text,
  WikiPage page,
  List<WikiCitation> citations,
  List<WikiSummary> pages,
) {
  final buffer = StringBuffer();
  var cursor = 0;
  String? lastCitation;
  for (final match in _marker.allMatches(text)) {
    final between = text.substring(cursor, match.start);
    if (match[1] != null) {
      final revision = int.parse(match[2]!);
      final index = citations.indexWhere(
        (c) => c.sourceId == match[1] && c.revision == revision,
      );
      final url = index < 0 ? null : '$citationAnchor$index';
      buffer.write(between);
      // A repeat of the number just shown adds nothing; an unknown marker
      // would only show an internal id.
      if (url != null && !(url == lastCitation && between.trim().isEmpty)) {
        buffer.write('[${index + 1}]($url)');
        lastCitation = url;
      }
    } else {
      final parts = match[3]!.split('|');
      final target = parts.first;
      final alias = parts.length > 1 ? parts.sublist(1).join('|') : '';
      final found = pages
          .where(
            (other) =>
                other.bodyAvailable &&
                other.projectId == page.projectId &&
                (other.slug == target || other.title == target),
          )
          .toList();
      buffer.write(between);
      if (found.length == 1) {
        final label = alias.isNotEmpty ? alias : found.single.title;
        buffer.write(
          '[$label]($pageAnchor${Uri.encodeComponent(found.single.id)})',
        );
      } else {
        buffer.write(alias.isNotEmpty ? alias : target);
      }
      lastCitation = null;
    }
    cursor = match.end;
  }
  buffer.write(text.substring(cursor));
  return buffer.toString();
}
