/// Resource-centre contracts (web `features/resources/types/index.ts`).
///
/// Lives in shared/models like `session.dart` and `cron.dart`: both the
/// resources feature and the chat composer speak this type, and features are
/// not allowed to import each other.
library;

import 'json.dart';

/// Backend-derived classification (`api/asset_kinds.py`) — the icon, the
/// filter and the preview mode all read this one answer.
const resourceKinds = [
  'image',
  'video',
  'audio',
  'document',
  'archive',
  'code',
  'other',
];

/// "all" = every project, "none" = filed under no project.
const allProjects = 'all';
const noProject = 'none';

class Resource {
  const Resource({
    required this.id,
    required this.name,
    required this.mime,
    required this.size,
    required this.kind,
    required this.source,
    required this.sandboxPath,
    required this.url,
    this.projectId,
    this.sessionId,
    this.status = 'ready',
    this.createdAt,
  });

  final String id;
  final String name;
  final String mime;
  final int size;

  /// One of [resourceKinds].
  final String kind;

  /// `user` — a person uploaded it; `agent` — the model produced it.
  final String source;

  /// Where the file lands in the sandbox once a message carries it.
  final String sandboxPath;

  /// Presigned GET. Expires — never persist it.
  final String url;

  final String? projectId;
  final String? sessionId;
  final String status;
  final DateTime? createdAt;

  bool get isAgent => source == 'agent';

  static Resource fromJson(Map<String, dynamic> json) => Resource(
        id: asString(json['id']) ?? '',
        name: asString(json['name']) ?? '',
        mime: asString(json['mime']) ?? 'application/octet-stream',
        size: asInt(json['size']) ?? 0,
        kind: asString(json['kind']) ?? 'other',
        source: asString(json['source']) ?? 'user',
        sandboxPath: asString(json['sandboxPath']) ?? '',
        url: asString(json['url']) ?? '',
        projectId: asString(json['projectId']),
        sessionId: asString(json['sessionId']),
        status: asString(json['status']) ?? 'ready',
        createdAt: asDate(json['createdAt']),
      );
}

class ResourcePage {
  const ResourcePage({
    required this.items,
    required this.total,
    required this.hasMore,
  });

  final List<Resource> items;
  final int total;
  final bool hasMore;

  static ResourcePage fromJson(Map<String, dynamic> json) => ResourcePage(
        items: [
          for (final item in asList(json['items']))
            if (item is Map<String, dynamic>) Resource.fromJson(item),
        ],
        total: asInt(json['total']) ?? 0,
        hasMore: asBool(json['hasMore']) ?? false,
      );
}

/// Everything that narrows the listing. A record-like value so it can key a
/// Riverpod family (structural equality).
class ResourceQuery {
  const ResourceQuery({
    this.project = allProjects,
    this.source = 'all',
    this.kind = 'all',
    this.q = '',
    this.sort = 'created',
    this.limit = 100,
  });

  final String project;
  final String source;
  final String kind;
  final String q;
  final String sort;
  final int limit;

  ResourceQuery copyWith({
    String? project,
    String? source,
    String? kind,
    String? q,
    String? sort,
    int? limit,
  }) =>
      ResourceQuery(
        project: project ?? this.project,
        source: source ?? this.source,
        kind: kind ?? this.kind,
        q: q ?? this.q,
        sort: sort ?? this.sort,
        limit: limit ?? this.limit,
      );

  Map<String, dynamic> toQueryParameters() => {
        'project': project,
        'source': source,
        'kind': kind,
        'sort': sort,
        'limit': limit,
        if (q.trim().isNotEmpty) 'q': q.trim(),
      };

  @override
  bool operator ==(Object other) =>
      other is ResourceQuery &&
      other.project == project &&
      other.source == source &&
      other.kind == kind &&
      other.q == q &&
      other.sort == sort &&
      other.limit == limit;

  @override
  int get hashCode => Object.hash(project, source, kind, q, sort, limit);
}
