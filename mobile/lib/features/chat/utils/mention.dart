/// Pure trigger detection / replacement for the composer's `@` / `/` mention
/// menu — a 1:1 port of frontend-v2 `features/chat/lib/mention.ts`:
/// - `@` fires at a word boundary (line start, whitespace, bracket,
///   punctuation, or a CJK char before it);
/// - `/` fires only at the start of a line;
/// - whitespace inside the query invalidates the trigger; queries longer
///   than 40 chars are dropped.
library;

enum MentionKind { at, slash }

class MentionTrigger {
  const MentionTrigger({
    required this.kind,
    required this.query,
    required this.start,
    required this.end,
  });

  final MentionKind kind;
  final String query;
  final int start;
  final int end;

  String get key => '${kind.name}:$start:$query';
}

const _maxQuery = 40;

final _boundary = RegExp(
  '[\\[({<,.!?;:\'"`\\u3000-\\u303f\\u3400-\\u9fff\\uff00-\\uffef]',
);
final _whitespace = RegExp(r'\s');

bool _isAtBoundary(String text, int index) {
  if (index == 0) return true;
  final prev = text[index - 1];
  return _whitespace.hasMatch(prev) || _boundary.hasMatch(prev);
}

bool _isLineStart(String text, int index) =>
    index == 0 || text[index - 1] == '\n';

MentionTrigger? resolveTrigger(String text, int caret) {
  final end = caret.clamp(0, text.length);

  var runStart = end;
  while (runStart > 0 && !_whitespace.hasMatch(text[runStart - 1])) {
    runStart--;
  }

  // Leftmost trigger char in the whitespace-free run wins, so "@src/foo"
  // stays an @-mention despite the inner "/".
  for (var i = runStart; i < end; i++) {
    final ch = text[i];
    if (ch != '@' && ch != '/') continue;
    final kind = ch == '@' ? MentionKind.at : MentionKind.slash;
    final valid =
        kind == MentionKind.at ? _isAtBoundary(text, i) : _isLineStart(text, i);
    if (!valid) continue;
    final query = text.substring(i + 1, end);
    if (query.length > _maxQuery) return null;
    return MentionTrigger(kind: kind, query: query, start: i, end: end);
  }
  return null;
}

({String text, int caret}) replaceTrigger(
  String text,
  MentionTrigger trigger,
  String insert,
) {
  final before = text.substring(0, trigger.start);
  final after = text.substring(trigger.end);
  final trailing = after.startsWith(' ') ? '' : ' ';
  final chunk = '$insert$trailing';
  return (text: '$before$chunk$after', caret: trigger.start + chunk.length);
}
