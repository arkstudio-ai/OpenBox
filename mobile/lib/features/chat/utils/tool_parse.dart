/// Pure parsers that turn a tool part's raw input/output/metadata into the
/// structures the layout widgets render — a 1:1 port of frontend-v2
/// `features/chat/lib/tool-parse.ts`. No widgets here: every function is a
/// plain data transform.
library;

class SearchResult {
  const SearchResult({
    required this.title,
    required this.url,
    required this.snippet,
  });

  final String title;
  final String url;
  final String snippet;
}

class DiffEdit {
  const DiffEdit({required this.oldString, required this.newString});

  final String oldString;
  final String newString;
}

String _str(Object? value) => value is String ? value : '';

/// Structured web_search results from a tool part's `metadata.results`.
List<SearchResult> parseSearchResults(Map<String, dynamic>? metadata) {
  final results = metadata?['results'];
  if (results is! List) return const [];
  final out = <SearchResult>[];
  for (final item in results) {
    if (item is! Map<String, dynamic>) continue;
    final url = _str(item['url']).trim();
    if (url.isEmpty) continue;
    out.add(SearchResult(
      title: _str(item['title']).trim(),
      url: url,
      snippet: _str(item['snippet']).trim(),
    ));
  }
  return out;
}

final _urlLine = RegExp(r'^\s+URL:\s*(\S+)', multiLine: true);

/// Fallback: pull URLs out of web_search's numbered plain-text output.
List<String> parseSearchUrls(String? output) {
  if (output == null || output.isEmpty) return const [];
  return [
    for (final match in _urlLine.allMatches(output))
      if (match.group(1) != null) match.group(1)!,
  ];
}

/// Deduplicate URLs preserving first-seen order, capped for the source row.
List<String> dedupeUrls(List<String> urls, {int limit = 8}) {
  final seen = <String>{};
  final out = <String>[];
  for (final raw in urls) {
    final url = raw.trim();
    if (url.isEmpty || !seen.add(url)) continue;
    out.add(url);
    if (out.length >= limit) break;
  }
  return out;
}

/// Hostname for a source pill; falls back to the raw string when unparseable.
String safeHostname(String url) {
  final host = Uri.tryParse(url)?.host ?? '';
  return host.isEmpty ? url : host;
}

final _lineNumber = RegExp(r'^\s*\d+\t');

/// Strip the `     12\t` line-number prefix `read` prepends to every line.
String stripLineNumbers(String text) =>
    text.split('\n').map((line) => line.replaceFirst(_lineNumber, '')).join('\n');

/// old/new pairs for edit (single) and multiedit (`edits` array) inputs.
List<DiffEdit> parseEdits(Object? input) {
  if (input is! Map<String, dynamic>) return const [];
  final edits = input['edits'];
  if (edits is List) {
    return [
      for (final edit in edits)
        if (edit is Map<String, dynamic>)
          DiffEdit(
            oldString: _str(edit['old_string']),
            newString: _str(edit['new_string']),
          ),
    ];
  }
  final oldString = _str(input['old_string']);
  final newString = _str(input['new_string']);
  if (oldString.isNotEmpty || newString.isNotEmpty) {
    return [DiffEdit(oldString: oldString, newString: newString)];
  }
  return const [];
}

/// Bash exit code from `metadata.exit_code` (number or numeric string).
int? parseExitCode(Map<String, dynamic>? metadata) {
  final value = metadata?['exit_code'];
  if (value is num && value.isFinite) return value.toInt();
  if (value is String) return int.tryParse(value.trim());
  return null;
}

/// Whether metadata flags the output as truncated.
bool isTruncated(Map<String, dynamic>? metadata) =>
    metadata?['truncated'] == true;

/// A string field off a tool's input map, trimmed, or empty.
String toolInput(Object? input, String key) =>
    input is Map<String, dynamic> ? _str(input[key]).trim() : '';

/// Pair each question with the labels chosen for it (web `questionPairs`).
List<(String, List<String>)> questionPairs(Map<String, dynamic> metadata) {
  final questions = metadata['questions'];
  final answers = metadata['answers'];
  if (questions is! List) return const [];
  final out = <(String, List<String>)>[];
  for (final (index, question) in questions.indexed) {
    if (question is! String) continue;
    final answer = answers is List && index < answers.length ? answers[index] : null;
    out.add((
      question,
      answer is List ? [for (final a in answer) if (a is String) a] : const <String>[],
    ));
  }
  return out;
}
