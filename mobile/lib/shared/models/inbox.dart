import 'json.dart';

/// Message-centre models (backend `notifications/inbox.py::public_item`,
/// `api/inbox.py::public_topic`; see docs/MESSAGE_CENTER.md).

/// Inbox tabs, in nav order. `null` category means "all".
const inboxCategories = ['session', 'system', 'notice'];

/// An allow-listed navigation target. Anything the app does not recognise
/// resolves to the inbox itself — never to a URL the server did not vet.
class InboxLink {
  const InboxLink({
    required this.kind,
    this.workspaceId,
    this.sessionId,
    this.jobId,
    this.slug,
    this.url,
    this.panel,
    this.control = false,
  });

  static InboxLink? fromJson(dynamic json) {
    if (json is! Map<String, dynamic>) return null;
    final kind = asString(json['kind']);
    if (kind == null || kind.isEmpty) return null;
    return InboxLink(
      kind: kind,
      workspaceId: asString(json['workspaceId']),
      sessionId: asString(json['sessionId']),
      jobId: asString(json['jobId']),
      slug: asString(json['slug']),
      url: asString(json['url']),
      panel: asString(json['panel']),
      control: asBool(json['control']) ?? false,
    );
  }

  final String kind;
  final String? workspaceId;
  final String? sessionId;
  final String? jobId;
  final String? slug;
  final String? url;
  final String? panel;
  final bool control;
}

class InboxItem {
  const InboxItem({
    required this.id,
    required this.category,
    required this.kind,
    required this.title,
    required this.body,
    required this.createdAt,
    this.link,
    this.workspaceId,
    this.announcementId,
    this.readAt,
    this.resolvedAt,
  });

  factory InboxItem.fromJson(Map<String, dynamic> json) => InboxItem(
    id: asString(json['id']) ?? '',
    category: asString(json['category']) ?? 'system',
    kind: asString(json['kind']) ?? '',
    title: asString(json['title']) ?? '',
    body: asString(json['body']) ?? '',
    link: InboxLink.fromJson(json['link']),
    workspaceId: asString(json['workspaceId']),
    announcementId: asString(json['announcementId']),
    readAt: asDate(json['readAt']),
    resolvedAt: asDate(json['resolvedAt']),
    createdAt: asDate(json['createdAt']) ?? DateTime.now(),
  );

  final String id;
  final String category;
  final String kind;
  final String title;
  final String body;
  final InboxLink? link;
  final String? workspaceId;
  final String? announcementId;
  final DateTime? readAt;
  final DateTime? resolvedAt;
  final DateTime createdAt;

  bool get unread => readAt == null;

  InboxItem asRead() => InboxItem(
    id: id,
    category: category,
    kind: kind,
    title: title,
    body: body,
    link: link,
    workspaceId: workspaceId,
    announcementId: announcementId,
    readAt: readAt ?? DateTime.now(),
    resolvedAt: resolvedAt,
    createdAt: createdAt,
  );
}

class InboxUnread {
  const InboxUnread({
    this.total = 0,
    this.session = 0,
    this.system = 0,
    this.notice = 0,
  });

  factory InboxUnread.fromJson(Map<String, dynamic> json) => InboxUnread(
    total: asInt(json['total']) ?? 0,
    session: asInt(json['session']) ?? 0,
    system: asInt(json['system']) ?? 0,
    notice: asInt(json['notice']) ?? 0,
  );

  final int total;
  final int session;
  final int system;
  final int notice;

  int forCategory(String? category) => switch (category) {
    'session' => session,
    'system' => system,
    'notice' => notice,
    _ => total,
  };
}

class InboxPage {
  const InboxPage({
    required this.items,
    this.nextCursor,
    this.unread = const InboxUnread(),
  });

  factory InboxPage.fromJson(Map<String, dynamic> json) => InboxPage(
    items: asList(
      json['items'],
    ).whereType<Map<String, dynamic>>().map(InboxItem.fromJson).toList(),
    nextCursor: asString(json['nextCursor']),
    unread: InboxUnread.fromJson(asMap(json['unread'])),
  );

  final List<InboxItem> items;
  final String? nextCursor;
  final InboxUnread unread;
}

class TopicPage {
  const TopicPage({
    required this.slug,
    required this.title,
    required this.contentMd,
    this.coverUrl,
    this.ctaLabel,
    this.ctaLink,
    this.publishedAt,
  });

  factory TopicPage.fromJson(Map<String, dynamic> json) => TopicPage(
    slug: asString(json['slug']) ?? '',
    title: asString(json['title']) ?? '',
    contentMd: asString(json['contentMd']) ?? '',
    coverUrl: asString(json['coverUrl']),
    ctaLabel: asString(json['ctaLabel']),
    ctaLink: InboxLink.fromJson(json['ctaLink']),
    publishedAt: asDate(json['publishedAt']),
  );

  final String slug;
  final String title;
  final String contentMd;
  final String? coverUrl;
  final String? ctaLabel;
  final InboxLink? ctaLink;
  final DateTime? publishedAt;
}
