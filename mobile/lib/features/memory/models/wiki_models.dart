import '../../../shared/models/json.dart';

/// Wire shapes of the topic (wiki) and document APIs (web `wiki-api.ts`,
/// `wiki/documents-api.ts`).

List<String> _strings(dynamic value) => [
  for (final item in asList(value))
    if (item is String) item,
];

/// A topic page as the library lists it.
class WikiSummary {
  const WikiSummary({
    required this.id,
    required this.title,
    required this.slug,
    required this.bodyAvailable,
    required this.status,
    this.excerpt = '',
    this.sourceCount = 0,
    this.sourceIds = const [],
    this.projectId,
    this.updatedAt,
  });

  factory WikiSummary.fromJson(Map<String, dynamic> json) => WikiSummary(
    id: asString(json['id']) ?? '',
    title: asString(json['title']) ?? '',
    slug: asString(json['slug']) ?? '',
    bodyAvailable: asBool(json['body_available']) ?? false,
    status: asString(json['status']) ?? '',
    excerpt: asString(json['excerpt']) ?? '',
    sourceCount: asInt(json['source_count']) ?? 0,
    sourceIds: _strings(json['source_ids']),
    projectId: asString(json['project_id']),
    updatedAt: asDate(json['updated_at']),
  );

  final String id;
  final String title;
  final String slug;
  final bool bodyAvailable;
  final String status;
  final String excerpt;
  final int sourceCount;
  final List<String> sourceIds;
  final String? projectId;
  final DateTime? updatedAt;
}

/// The file a document page was organized from, and its other sections.
class WikiDocumentInfo {
  const WikiDocumentInfo({
    required this.id,
    required this.filename,
    this.sections = const [],
  });

  factory WikiDocumentInfo.fromJson(Map<String, dynamic> json) =>
      WikiDocumentInfo(
        id: asString(json['id']) ?? '',
        filename: asString(json['filename']) ?? '',
        sections: [
          for (final section in asList(json['sections']))
            if (section is Map<String, dynamic>)
              (
                id: asString(section['id']) ?? '',
                title: asString(section['title']) ?? '',
              ),
        ],
      );

  final String id;
  final String filename;
  final List<({String id, String title})> sections;
}

/// The person's own correcting words behind a reconciled record.
class WikiChange {
  const WikiChange({required this.body, this.sessionId});

  final String body;
  final String? sessionId;
}

/// One source a page cites, as the reader may show it now.
class WikiSourceDetail {
  const WikiSourceDetail({
    required this.id,
    required this.kind,
    required this.body,
    this.sessionId,
    this.path,
    this.filename,
    this.originalPages = const [],
    this.edited = false,
    this.changes = const [],
  });

  factory WikiSourceDetail.fromJson(Map<String, dynamic> json) =>
      WikiSourceDetail(
        id: asString(json['id']) ?? '',
        kind: asString(json['kind']) ?? '',
        body: asString(json['body']) ?? '',
        sessionId: asString(json['session_id']),
        path: asString(json['path']),
        filename: asString(json['filename']),
        originalPages: [
          for (final page in asList(json['original_pages']))
            if (page is num) page.toInt(),
        ],
        edited: asBool(json['edited']) ?? false,
        changes: [
          for (final change in asList(json['changes']))
            if (change is Map<String, dynamic>)
              WikiChange(
                body: asString(change['body']) ?? '',
                sessionId: asString(change['session_id']),
              ),
        ],
      );

  final String id;
  final String kind;
  final String body;
  final String? sessionId;

  /// An imported source's path inside its knowledge pack.
  final String? path;
  final String? filename;
  final List<int> originalPages;
  final bool edited;
  final List<WikiChange> changes;
}

/// One topic or document page in full.
class WikiPage {
  const WikiPage({
    required this.id,
    required this.title,
    required this.slug,
    required this.status,
    required this.bodyAvailable,
    this.body,
    this.paragraphs = const [],
    this.projectId,
    this.updatedAt,
    this.document,
    this.sourceDetails = const [],
    this.exchangePath,
    this.exchangeLinks = const {},
  });

  factory WikiPage.fromJson(Map<String, dynamic> json) => WikiPage(
    id: asString(json['id']) ?? '',
    title: asString(json['title']) ?? '',
    slug: asString(json['slug']) ?? '',
    status: asString(json['status']) ?? '',
    bodyAvailable: asBool(json['body_available']) ?? false,
    body: asString(json['body']),
    paragraphs: [
      for (final paragraph in asList(json['paragraphs']))
        if (paragraph is Map<String, dynamic>) paragraph,
    ],
    projectId: asString(json['project_id']),
    updatedAt: asDate(json['updated_at']),
    document: json['document'] is Map<String, dynamic>
        ? WikiDocumentInfo.fromJson(asMap(json['document']))
        : null,
    sourceDetails: [
      for (final source in asList(json['source_details']))
        if (source is Map<String, dynamic>) WikiSourceDetail.fromJson(source),
    ],
    exchangePath: asString(json['exchange_path']),
    exchangeLinks: {
      for (final entry in asMap(json['exchange_links']).entries)
        entry.key: (
          pageId: asString(asMap(entry.value)['page_id']),
          sourceId: asString(asMap(entry.value)['source_id']),
        ),
    },
  );

  final String id;
  final String title;
  final String slug;
  final String status;
  final bool bodyAvailable;
  final String? body;
  final List<Map<String, dynamic>> paragraphs;
  final String? projectId;
  final DateTime? updatedAt;
  final WikiDocumentInfo? document;
  final List<WikiSourceDetail> sourceDetails;

  /// Where an imported page sat in its knowledge pack, and what its local
  /// links resolve to.
  final String? exchangePath;
  final Map<String, ({String? pageId, String? sourceId})> exchangeLinks;

  /// Retired: its facts live on in memories or a merged topic.
  bool get retired => status == 'retired';
}

/// One editable piece of text behind a page.
class WikiEditEntry {
  const WikiEditEntry({
    required this.id,
    required this.revision,
    required this.text,
    required this.maxLength,
  });

  final String id;
  final int revision;
  final String text;
  final int maxLength;
}

/// The page as it can be edited: the revision and hash the edit is based on.
class WikiEditSnapshot {
  const WikiEditSnapshot({
    required this.id,
    required this.revision,
    required this.contentHash,
    required this.title,
    required this.entries,
  });

  factory WikiEditSnapshot.fromJson(Map<String, dynamic> json) =>
      WikiEditSnapshot(
        id: asString(json['id']) ?? '',
        revision: asInt(json['revision']) ?? 1,
        contentHash: asString(json['content_hash']) ?? '',
        title: asString(json['title']) ?? '',
        entries: [
          for (final entry in asList(json['entries']))
            if (entry is Map<String, dynamic>)
              WikiEditEntry(
                id: asString(entry['id']) ?? '',
                revision: asInt(entry['revision']) ?? 1,
                text: asString(entry['text']) ?? '',
                maxLength: asInt(entry['max_length']) ?? 2000,
              ),
        ],
      );

  final String id;
  final int revision;
  final String contentHash;
  final String title;
  final List<WikiEditEntry> entries;
}

/// An uploaded file and how far its organizing has come.
class KnowledgeDocument {
  const KnowledgeDocument({
    required this.id,
    required this.filename,
    required this.status,
    this.reasonCode,
    this.pageIds = const [],
    this.bytes,
    this.createdAt,
    this.created = true,
  });

  factory KnowledgeDocument.fromJson(Map<String, dynamic> json) =>
      KnowledgeDocument(
        id: asString(json['id']) ?? '',
        filename: asString(json['filename']) ?? '',
        status: asString(json['status']) ?? '',
        reasonCode: asString(json['reason_code']),
        pageIds: _strings(json['page_ids']),
        bytes: asInt(json['bytes']),
        createdAt: asDate(json['created_at']),
        created: asBool(json['created']) ?? true,
      );

  final String id;
  final String filename;
  final String status;
  final String? reasonCode;
  final List<String> pageIds;
  final int? bytes;
  final DateTime? createdAt;

  /// Upload replies only: false when an upload reused an existing document.
  final bool created;

  static const processing = {'pending', 'parsing', 'retry', 'indexing'};

  bool get failed => status == 'failed' || status == 'index_failed';
}
