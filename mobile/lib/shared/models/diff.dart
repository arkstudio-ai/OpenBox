import 'json.dart';

/// Mirrors `DiffLine` / `DiffHunk` / `DiffEntry`
/// (frontend-v2 `shared/types/api.ts:189-208`).
class DiffLine {
  const DiffLine({
    required this.type,
    required this.content,
    this.oldLine,
    this.newLine,
  });

  factory DiffLine.fromJson(Map<String, dynamic> json) => DiffLine(
        type: asString(json['type']) ?? 'context',
        content: asString(json['content']) ?? '',
        oldLine: asInt(json['old_line']),
        newLine: asInt(json['new_line']),
      );

  final String type; // add | del | context
  final String content;
  final int? oldLine;
  final int? newLine;
}

class DiffHunk {
  const DiffHunk({
    required this.oldStart,
    required this.oldCount,
    required this.newStart,
    required this.newCount,
    required this.lines,
  });

  factory DiffHunk.fromJson(Map<String, dynamic> json) => DiffHunk(
        oldStart: asInt(json['old_start']) ?? 0,
        oldCount: asInt(json['old_count']) ?? 0,
        newStart: asInt(json['new_start']) ?? 0,
        newCount: asInt(json['new_count']) ?? 0,
        lines: asList(json['lines'])
            .whereType<Map<String, dynamic>>()
            .map(DiffLine.fromJson)
            .toList(),
      );

  final int oldStart;
  final int oldCount;
  final int newStart;
  final int newCount;
  final List<DiffLine> lines;
}

class DiffEntry {
  const DiffEntry({
    required this.path,
    required this.additions,
    required this.deletions,
    required this.status,
    this.hunks,
  });

  factory DiffEntry.fromJson(Map<String, dynamic> json) => DiffEntry(
        path: asString(json['path']) ?? '',
        additions: asInt(json['additions']) ?? 0,
        deletions: asInt(json['deletions']) ?? 0,
        status: asString(json['status']) ?? 'modified',
        hunks: json['hunks'] is List
            ? asList(json['hunks'])
                .whereType<Map<String, dynamic>>()
                .map(DiffHunk.fromJson)
                .toList()
            : null,
      );

  final String path;
  final int additions;
  final int deletions;
  final String status; // added | modified | deleted
  final List<DiffHunk>? hunks;
}
