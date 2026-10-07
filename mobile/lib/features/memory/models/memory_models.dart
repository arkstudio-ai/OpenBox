import '../../../shared/models/json.dart';

/// Wire shapes of the memory API (web `shared/api/memory.ts`). Parsing is
/// tolerant: a missing field reads as "unknown", never as a crash.

DateTime? _date(dynamic value) => asDate(value);

/// One remembered fact.
class MemoryRecord {
  const MemoryRecord({
    required this.id,
    required this.summary,
    required this.status,
    required this.revision,
    this.bodyAvailable,
    this.projectId,
    this.createdAt,
    this.updatedAt,
  });

  factory MemoryRecord.fromJson(Map<String, dynamic> json) => MemoryRecord(
    id: asString(json['id']) ?? '',
    summary: asString(json['summary']) ?? '',
    status: asString(json['status']) ?? '',
    revision: asInt(json['revision']) ?? 1,
    bodyAvailable: asBool(json['body_available']),
    projectId: asString(json['project_id']),
    createdAt: _date(json['created_at']),
    updatedAt: _date(json['updated_at']),
  );

  final String id;
  final String summary;
  final String status;
  final int revision;

  /// Null when the list did not say; false means the text may not be shown.
  final bool? bodyAvailable;
  final String? projectId;
  final DateTime? createdAt;
  final DateTime? updatedAt;

  /// What a forgotten memory looks like locally: no text, no revision to
  /// reuse (web `useMemoryActions.forgotten`).
  MemoryRecord forgotten() => MemoryRecord(
    id: id,
    summary: '',
    status: 'DEPRECATED',
    revision: revision,
    bodyAvailable: false,
    projectId: projectId,
    createdAt: createdAt,
    updatedAt: updatedAt,
  );
}

/// The person's own correcting words behind a corrected record.
class SourceChange {
  const SourceChange({required this.body, this.sessionId});

  factory SourceChange.fromJson(Map<String, dynamic> json) => SourceChange(
    body: asString(json['body']) ?? '',
    sessionId: asString(json['session_id']),
  );

  final String body;
  final String? sessionId;
}

/// What a memory was learned from.
class MemorySource {
  const MemorySource({
    required this.id,
    required this.kind,
    this.body,
    this.sessionId,
    this.createdAt,
    this.superseded = false,
    this.changes = const [],
  });

  factory MemorySource.fromJson(Map<String, dynamic> json) => MemorySource(
    id: asString(json['id']) ?? '',
    kind:
        asString(json['source_kind']) ??
        asString(json['source_type']) ??
        'other',
    // Only text the server still allows; never a placeholder of our own.
    body: asBool(json['body_available']) == false
        ? null
        : asString(json['body']) ??
              asString(json['content']) ??
              asString(json['text']),
    sessionId: asString(json['session_id']),
    createdAt: _date(json['created_at']),
    superseded: asBool(json['superseded']) ?? false,
    changes: [
      for (final change in asList(json['changes']))
        if (change is Map<String, dynamic>) SourceChange.fromJson(change),
    ],
  );

  final String id;
  final String kind;
  final String? body;
  final String? sessionId;
  final DateTime? createdAt;

  /// A later correction replaced this source; it no longer backs the memory.
  final bool superseded;
  final List<SourceChange> changes;
}

/// One step in a memory's history.
class MemoryRevision {
  const MemoryRevision({
    required this.revision,
    required this.reason,
    this.text,
    this.createdAt,
  });

  factory MemoryRevision.fromJson(Map<String, dynamic> json) => MemoryRevision(
    revision: asInt(json['revision']) ?? 0,
    reason: asString(json['reason']) ?? asString(json['action']) ?? 'other',
    text: asBool(json['body_available']) == false
        ? null
        : asString(json['summary']),
    createdAt: _date(json['created_at']),
  );

  final int revision;
  final String reason;
  final String? text;
  final DateTime? createdAt;
}

/// How forgetting a memory is going.
class MemoryCleanup {
  const MemoryCleanup({required this.status, this.stopped = false});

  factory MemoryCleanup.fromJson(Map<String, dynamic> json) => MemoryCleanup(
    status: asString(json['status']) ?? '',
    stopped: asBool(json['stopped']) ?? false,
  );

  final String status;
  final bool stopped;
}

/// A turn that could not be saved as a memory, in the person's own words.
class FailedTurn {
  const FailedTurn({
    required this.id,
    required this.sessionId,
    required this.excerpt,
    this.sessionTitle,
  });

  factory FailedTurn.fromJson(Map<String, dynamic> json) => FailedTurn(
    id: asString(json['id']) ?? '',
    sessionId: asString(json['session_id']) ?? '',
    sessionTitle: asString(json['session_title']),
    excerpt: asString(json['excerpt']) ?? '',
  );

  final String id;
  final String sessionId;
  final String? sessionTitle;
  final String excerpt;
}

/// Turns still being saved as memories, and ones that could not be.
class MemoryProcessing {
  const MemoryProcessing({this.pending = 0, this.failed = const []});

  factory MemoryProcessing.fromJson(Map<String, dynamic> json) =>
      MemoryProcessing(
        pending: asInt(json['pending']) ?? 0,
        failed: [
          for (final item in asList(json['failed']))
            if (item is Map<String, dynamic>) FailedTurn.fromJson(item),
        ],
      );

  final int pending;
  final List<FailedTurn> failed;
}

/// Automatic saving for the account.
class MemorySettings {
  const MemorySettings({required this.autoSave});

  factory MemorySettings.fromJson(Map<String, dynamic> json) =>
      MemorySettings(autoSave: asBool(json['auto_save']) ?? true);

  final bool autoSave;
}

/// A topic a memory belongs to (web `MemoryTopic` + its member ids).
class MemoryTopic {
  const MemoryTopic({
    required this.id,
    required this.title,
    this.pageId,
    this.memoryIds = const [],
  });

  factory MemoryTopic.fromJson(Map<String, dynamic> json) => MemoryTopic(
    id: asString(json['id']) ?? '',
    title: asString(json['title']) ?? '',
    pageId: asString(json['page_id']),
    memoryIds: [
      for (final id in asList(json['memory_ids']))
        if (id is String) id,
    ],
  );

  final String id;
  final String title;

  /// Set only while the topic has a readable page.
  final String? pageId;
  final List<String> memoryIds;
}

/// A project a memory, topic or file can belong to.
class KnowledgeProject {
  const KnowledgeProject({required this.id, required this.name});

  final String id;
  final String name;
}

/// One page of a server-paged list.
class Paged<T> {
  const Paged(
    this.items, {
    this.nextOffset,
    this.loadingMore = false,
    this.cleanupPending = 0,
    this.fetchedAt,
  });

  final List<T> items;
  final int? nextOffset;

  /// A "load more" request is in flight.
  final bool loadingMore;

  /// Documents only: deleted files whose originals are still being removed.
  final int cleanupPending;

  /// When the first page was read; what "being updated" is measured against.
  final DateTime? fetchedAt;

  bool get hasMore => nextOffset != null;

  Paged<T> copyWith({
    List<T>? items,
    int? Function()? nextOffset,
    bool? loadingMore,
  }) => Paged(
    items ?? this.items,
    nextOffset: nextOffset == null ? this.nextOffset : nextOffset(),
    loadingMore: loadingMore ?? this.loadingMore,
    cleanupPending: cleanupPending,
    fetchedAt: fetchedAt,
  );
}
