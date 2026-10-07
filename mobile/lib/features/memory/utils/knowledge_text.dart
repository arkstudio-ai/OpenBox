import 'dart:math';

import '../../../shared/i18n/i18n.dart';
import '../models/memory_models.dart';
import '../models/wiki_models.dart';

/// The knowledge page's sections (web `KNOWLEDGE_VIEWS`).
const knowledgeViews = ['overview', 'memories', 'topics', 'files'];

/// Unknown values — including the retired management views (reviews,
/// workflows, concepts, …) — open the overview rather than an error.
String readView(String? value) =>
    knowledgeViews.contains(value) ? value! : 'overview';

/// Case-insensitive containment, the same rule the search box applies.
bool matchesQuery(String? text, String query) =>
    query.isEmpty || (text ?? '').toLowerCase().contains(query.toLowerCase());

/// Splits text around each case-insensitive occurrence of the query, so the
/// matched parts can be highlighted without touching the rest.
List<({String text, bool hit})> splitMatches(String text, String query) {
  final lower = text.toLowerCase();
  final needle = query.toLowerCase();
  // Lower-casing can change a string's length (e.g. "İ"); offsets would drift.
  if (needle.isEmpty || lower.length != text.length) {
    return [(text: text, hit: false)];
  }
  final parts = <({String text, bool hit})>[];
  var start = 0;
  for (
    var index = lower.indexOf(needle);
    index >= 0;
    index = lower.indexOf(needle, start)
  ) {
    if (index > start) {
      parts.add((text: text.substring(start, index), hit: false));
    }
    parts.add((text: text.substring(index, index + needle.length), hit: true));
    start = index + needle.length;
  }
  if (start < text.length) parts.add((text: text.substring(start), hit: false));
  return parts;
}

/// A card preview: Markdown syntax, citation markers and a heading that only
/// repeats the title removed, whitespace collapsed. Display only — never fed
/// back as content. A sentence that merely starts with the title is kept.
String plainExcerpt(String markdown, [String title = '']) {
  final heading = title.trim();
  final body = heading.isEmpty
      ? markdown
      : markdown.replaceFirst(
          RegExp('^\\s*#{1,6}\\s+${RegExp.escape(heading)}\\s*(?:\\n|\$)'),
          '',
        );
  return body
      .replaceAll(RegExp(r'\[source:[^\]\s]+\]'), '')
      .replaceAllMapped(
        RegExp(r'\[\[([^\]|\n]+)\|?([^\]\n]*)\]\]'),
        (m) => (m[2] ?? '').isNotEmpty ? m[2]! : m[1]!,
      )
      .replaceAllMapped(RegExp(r'!\[([^\]]*)\]\([^)]*\)'), (m) => m[1]!)
      .replaceAllMapped(RegExp(r'\[([^\]]+)\]\([^)]*\)'), (m) => m[1]!)
      .replaceAll(RegExp(r'^\s{0,3}#{1,6}\s+', multiLine: true), '')
      .replaceAll(RegExp(r'^\s*(?:[-*+]|\d+\.)\s+', multiLine: true), '')
      .replaceAll(RegExp(r'^\s*>\s?', multiLine: true), '')
      .replaceAll(
        RegExp(r'^\s*\|?(?:\s*:?-{3,}:?\s*\|)+.*$', multiLine: true),
        '',
      )
      .replaceAll('|', ' ')
      .replaceAll(RegExp(r'[*_`~]'), '')
      .replaceAll(RegExp(r'\s+'), ' ')
      .trim();
}

/// Forgotten, or reported stopped by the server: nothing of its text may show.
bool memoryIsStopped(MemoryRecord memory, [MemoryCleanup? cleanup]) =>
    memory.bodyAvailable == false ||
    memory.status == 'DEPRECATED' ||
    cleanup?.stopped == true ||
    const {'stopped_cleanup_pending', 'cleaned'}.contains(cleanup?.status);

/// How long a page without readable text still counts as "being updated".
/// Rebuilds finish within the maintenance cycle; a page stale for longer has
/// usually lost everything it was built from and has nothing left to show.
const _rebuildWindow = Duration(minutes: 15);

/// [readAt] is when the library was last read; null counts nothing as
/// rebuilding.
bool rebuilding(WikiSummary page, DateTime? readAt) =>
    readAt != null &&
    page.updatedAt != null &&
    readAt.difference(page.updatedAt!) < _rebuildWindow;

/// Newest change first; rows without a time go last.
int byRecent(MemoryRecord a, MemoryRecord b) {
  final left = a.updatedAt, right = b.updatedAt;
  if (left == null || right == null) {
    return left == right ? 0 : (left == null ? 1 : -1);
  }
  return right.compareTo(left);
}

/// A fresh idempotency key (web `crypto.randomUUID()`): one per command, kept
/// until the server answers so a retried tap is the same command.
String newRequestId() {
  final random = Random.secure();
  final bytes = List<int>.generate(16, (_) => random.nextInt(256));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  final hex = bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
  return '${hex.substring(0, 8)}-${hex.substring(8, 12)}-'
      '${hex.substring(12, 16)}-${hex.substring(16, 20)}-${hex.substring(20)}';
}

/// [key] when the bundle has it, else [fallback] — i18next's `defaultValue`.
String tOr(
  I18nState i18n,
  String key,
  String fallback, {
  Map<String, Object?>? vars,
}) {
  final value = i18n.t(key, vars: vars);
  return value == key ? i18n.t(fallback, vars: vars) : value;
}

/// What a document reason code means for the person, or the generic hint.
String documentProblem(I18nState i18n, String code) =>
    tOr(i18n, 'wiki:documents.errors.$code', 'wiki:documents.failedHint');
