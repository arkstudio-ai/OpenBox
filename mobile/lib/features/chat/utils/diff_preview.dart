/// Turns a tool's edit into a short preview: only changed lines survive, and
/// every run of untouched context collapses into one "N unmodified lines" bar.
/// A 1:1 port of frontend-v2 `features/chat/lib/diff-preview.ts`.
library;

sealed class PreviewRow {
  const PreviewRow();
}

class ChangeRow extends PreviewRow {
  const ChangeRow({required this.added, required this.text});

  /// True for an inserted line, false for a removed one.
  final bool added;
  final String text;
}

class GapRow extends PreviewRow {
  GapRow(this.count);

  int count;
}

class DiffPreview {
  const DiffPreview({
    required this.rows,
    required this.hiddenChanges,
    required this.totalChanges,
  });

  final List<PreviewRow> rows;

  /// Changed lines that did not fit in the preview.
  final int hiddenChanges;
  final int totalChanges;

  bool get isEmpty => rows.isEmpty;
}

void _pushGap(List<PreviewRow> rows, int count) {
  if (count <= 0) return;
  final last = rows.isEmpty ? null : rows.last;
  if (last is GapRow) {
    last.count += count;
    return;
  }
  rows.add(GapRow(count));
}

/// Preview rows for an in-place edit, built from the tool's own
/// old_string/new_string. Shared leading/trailing lines collapse into gap bars
/// so a one-line change inside a big block reads as one line, not two blocks.
DiffPreview editPreview(String oldText, String newText, {int maxChanges = 8}) {
  final before = oldText.isEmpty ? <String>[] : oldText.split('\n');
  final after = newText.isEmpty ? <String>[] : newText.split('\n');

  var head = 0;
  while (head < before.length && head < after.length && before[head] == after[head]) {
    head += 1;
  }

  var tail = 0;
  while (tail < before.length - head &&
      tail < after.length - head &&
      before[before.length - 1 - tail] == after[after.length - 1 - tail]) {
    tail += 1;
  }

  final removed = before.sublist(head, before.length - tail);
  final added = after.sublist(head, after.length - tail);
  final total = removed.length + added.length;

  final rows = <PreviewRow>[];
  _pushGap(rows, head);
  var shown = 0;
  for (final text in removed) {
    if (shown >= maxChanges) break;
    rows.add(ChangeRow(added: false, text: text));
    shown += 1;
  }
  for (final text in added) {
    if (shown >= maxChanges) break;
    rows.add(ChangeRow(added: true, text: text));
    shown += 1;
  }
  if (rows.isNotEmpty) _pushGap(rows, tail);

  return DiffPreview(
    rows: rows,
    hiddenChanges: total - shown < 0 ? 0 : total - shown,
    totalChanges: total,
  );
}
