import 'json.dart';

typedef PlatformScope = ({String userId, String workspaceId});

/// Authorization APIs deliberately use camelCase, unlike chat's wire models.
class PlatformInfo {
  const PlatformInfo({
    required this.key,
    required this.display,
    required this.configured,
    this.capabilities = const [],
    this.maxGrantDays,
    this.kind = 'oauth',
    this.reconPending = false,
  });
  final String key;
  final String display;
  final bool configured;
  final List<String> capabilities;
  final int? maxGrantDays;
  final String kind;
  final bool reconPending;

  factory PlatformInfo.fromJson(Map<String, dynamic> j) => PlatformInfo(
    key: asString(j['key']) ?? '',
    display: asString(j['display']) ?? '',
    configured: asBool(j['configured']) ?? false,
    capabilities: asList(j['capabilities']).whereType<String>().toList(),
    maxGrantDays: asInt(j['maxGrantDays']),
    kind: asString(j['kind']) ?? 'oauth',
    reconPending: asBool(j['reconPending']) ?? false,
  );
}

class PlatformAccount {
  const PlatformAccount({
    required this.id,
    required this.platform,
    required this.status,
    this.externalId = '',
    this.nickname,
    this.avatarUrl,
    this.scopes = const [],
    this.refreshExpiresAt,
    this.estimatedExpiresAt,
    this.lastProbeAt,
    this.lastError,
    this.renewalsLeft = 0,
    this.authKind = 'oauth',
    this.desktopId,
    this.siteDisplay,
    this.predictedExpiresAt,
    this.probeDisplay = const {},
  });
  final String id;
  final String platform;
  final String status;
  final String externalId;
  final String? nickname;
  final String? avatarUrl;
  final List<String> scopes;
  final DateTime? refreshExpiresAt;
  final DateTime? estimatedExpiresAt;
  final DateTime? lastProbeAt;
  final String? lastError;
  final int renewalsLeft;
  final String authKind;
  final String? desktopId;
  final String? siteDisplay;
  final DateTime? predictedExpiresAt;
  final Map<String, dynamic> probeDisplay;
  DateTime? get expectedExpiry => estimatedExpiresAt ?? refreshExpiresAt;
  String statusAt(DateTime now) {
    if (status != 'bound') return status;
    final expiry = expectedExpiry;
    if (expiry != null && !expiry.isAfter(now)) return 'expired';
    if (expiry != null && expiry.difference(now) <= const Duration(days: 7)) {
      return 'expiring';
    }
    return 'bound';
  }

  factory PlatformAccount.fromJson(Map<String, dynamic> j) => PlatformAccount(
    id: asString(j['id']) ?? '',
    platform: asString(j['platform']) ?? '',
    status: asString(j['status']) ?? 'unknown',
    externalId: asString(j['externalId']) ?? '',
    nickname: asString(j['nickname']),
    avatarUrl: asString(j['avatarUrl']),
    scopes: asList(j['scopes']).whereType<String>().toList(),
    refreshExpiresAt: asDate(j['refreshExpiresAt']),
    estimatedExpiresAt: asDate(j['estimatedExpiresAt']),
    lastProbeAt: asDate(j['lastProbeAt']),
    lastError: asString(j['lastError']),
    renewalsLeft: asInt(j['renewalsLeft']) ?? 0,
    authKind: asString(j['authKind']) ?? 'oauth',
    desktopId: asString(j['desktopId']),
    siteDisplay: asString(j['siteDisplay']),
    predictedExpiresAt: asDate(j['predictedExpiresAt']),
    probeDisplay: asMap(asMap(j['probeDetail'])['display']),
  );
}

class PlatformNotification {
  const PlatformNotification({
    required this.id,
    required this.title,
    this.body = '',
    this.kind = '',
    this.readAt,
  });
  final String id;
  final String title;
  final String body;
  final String kind;
  final DateTime? readAt;

  factory PlatformNotification.fromJson(Map<String, dynamic> j) =>
      PlatformNotification(
        id: asString(j['id']) ?? '',
        title: asString(j['title']) ?? '',
        body: asString(j['body']) ?? '',
        kind: asString(j['kind']) ?? '',
        readAt: asDate(j['readAt']),
      );
}

class PlatformNotificationPage {
  const PlatformNotificationPage({this.items = const [], this.unread = 0});
  final List<PlatformNotification> items;
  final int unread;

  factory PlatformNotificationPage.fromJson(Map<String, dynamic> j) =>
      PlatformNotificationPage(
        items: asList(j['items'])
            .whereType<Map<String, dynamic>>()
            .map(PlatformNotification.fromJson)
            .toList(),
        unread: asInt(j['unread']) ?? 0,
      );
}

class PublishJob {
  const PublishJob({
    required this.id,
    required this.status,
    this.title = '',
    this.fileAssetId = '',
    this.hashtags = const [],
    this.shareId,
    this.itemId,
    this.error,
    this.expiresAt,
    this.createdAt,
  });
  final String id;
  final String status;
  final String title;
  final String fileAssetId;
  final List<String> hashtags;
  final String? shareId;
  final String? itemId;
  final String? error;
  final DateTime? expiresAt;
  final DateTime? createdAt;
  bool get pending => status == 'pending';
  bool linkValidAt(DateTime now) =>
      pending && expiresAt != null && expiresAt!.isAfter(now);

  factory PublishJob.fromJson(Map<String, dynamic> j) => PublishJob(
    id: asString(j['id']) ?? '',
    status: asString(j['status']) ?? 'unknown',
    title: asString(j['title']) ?? '',
    fileAssetId: asString(j['fileAssetId']) ?? '',
    hashtags: asList(j['hashtags']).whereType<String>().toList(),
    shareId: asString(j['shareId']),
    itemId: asString(j['itemId']),
    error: asString(j['error']),
    expiresAt: asDate(j['expiresAt']),
    createdAt: asDate(j['createdAt']),
  );
}

class PublishResult {
  const PublishResult({required this.job, required this.schema});
  final PublishJob job;

  /// A short-lived signed capability. Keep in memory, never in preferences.
  final String schema;
  factory PublishResult.fromJson(Map<String, dynamic> j) => PublishResult(
    job: PublishJob.fromJson(asMap(j['job'])),
    schema: asString(j['schema']) ?? '',
  );
}
