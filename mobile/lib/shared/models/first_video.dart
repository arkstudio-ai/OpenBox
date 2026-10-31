/// First-video contracts (backend `api/first_video.py`, docs
/// `LIGHT_MODE_IMPL_PLAN.md` §3): the one free 15-second vertical video a new
/// account makes without typing, and its mirror in the account preferences.
///
/// Lives in shared/models because the routing layer (sign-in branching, push
/// landing) reads the same shapes as the feature, and features never import
/// each other.
library;

import 'json.dart';

/// `industry` values the backend accepts; `other` is the server default.
const firstVideoIndustries = ['food', 'beauty', 'retail', 'other'];

/// `template` values, in the order the picker shows them.
const firstVideoTemplateIds = ['signature', 'ambience', 'promo'];

/// Inbox / push kinds that land on the result page rather than in the chat
/// their `session` link points at.
const firstVideoNotificationKinds = {'first_video_ready', 'first_video_failed'};

class FirstVideoError {
  const FirstVideoError({required this.code, required this.message});

  static FirstVideoError? fromJson(dynamic json) {
    if (json is! Map<String, dynamic>) return null;
    return FirstVideoError(
      code: asString(json['code']) ?? '',
      message: asString(json['message']) ?? '',
    );
  }

  final String code;
  final String message;
}

/// `GET/POST /api/first-video` → the workspace's one guided job.
class FirstVideoJob {
  const FirstVideoJob({
    required this.status,
    this.sessionId,
    this.jobId,
    this.assetId,
    this.etaSeconds = 0,
    this.error,
  });

  factory FirstVideoJob.fromJson(Map<String, dynamic> json) => FirstVideoJob(
    status: asString(json['status']) ?? 'none',
    sessionId: asString(json['session_id']),
    jobId: asString(json['job_id']),
    assetId: asString(json['asset_id']),
    etaSeconds: asInt(json['eta_seconds']) ?? 0,
    error: FirstVideoError.fromJson(json['error']),
  );

  static const none = FirstVideoJob(status: 'none');

  /// `none` | `submitted` | `running` | `ready` | `failed`.
  final String status;
  final String? sessionId;
  final String? jobId;

  /// The finished video (`ready` only); played through the asset URL route.
  final String? assetId;
  final int etaSeconds;
  final FirstVideoError? error;

  bool get inFlight => status == 'submitted' || status == 'running';
  bool get ready => status == 'ready';
  bool get failed => status == 'failed';
  bool get started => status != 'none';
}

/// One `{id, label}` (industries) or `{id, label, description}` (templates)
/// row of `GET /api/first-video/templates`.
class FirstVideoOption {
  const FirstVideoOption({
    required this.id,
    required this.label,
    this.description = '',
  });

  static FirstVideoOption? fromJson(dynamic json) {
    if (json is! Map<String, dynamic>) return null;
    final id = asString(json['id']);
    if (id == null || id.isEmpty) return null;
    return FirstVideoOption(
      id: id,
      label: asString(json['label']) ?? id,
      description: asString(json['description']) ?? '',
    );
  }

  final String id;
  final String label;
  final String description;
}

class FirstVideoCatalog {
  const FirstVideoCatalog({
    required this.etaSeconds,
    required this.industries,
    required this.templates,
  });

  factory FirstVideoCatalog.fromJson(Map<String, dynamic> json) =>
      FirstVideoCatalog(
        etaSeconds: asInt(json['eta_seconds']) ?? 0,
        industries: asList(
          json['industries'],
        ).map(FirstVideoOption.fromJson).whereType<FirstVideoOption>().toList(),
        templates: asList(
          json['templates'],
        ).map(FirstVideoOption.fromJson).whereType<FirstVideoOption>().toList(),
      );

  final int etaSeconds;
  final List<FirstVideoOption> industries;
  final List<FirstVideoOption> templates;
}

/// `extra.first_video` of `GET /api/auth/me/preferences`. The server writes
/// every status but `skipped`, which the app sets when the person leaves the
/// flow for good. Absent on an account that never started.
class FirstVideoPrefs {
  const FirstVideoPrefs({
    required this.status,
    this.sessionId,
    this.jobId,
    this.assetId,
    this.template,
    this.shopName,
    this.headline,
    this.updatedAt,
  });

  static FirstVideoPrefs? fromJson(dynamic json) {
    if (json is! Map) return null;
    final map = Map<String, dynamic>.from(json);
    final status = asString(map['status']);
    if (status == null || status.isEmpty) return null;
    return FirstVideoPrefs(
      status: status,
      sessionId: asString(map['session_id']),
      jobId: asString(map['job_id']),
      assetId: asString(map['asset_id']),
      template: asString(map['template']),
      shopName: asString(map['shop_name']),
      headline: asString(map['headline']),
      updatedAt: asDate(map['updated_at']),
    );
  }

  final String status;
  final String? sessionId;
  final String? jobId;
  final String? assetId;
  final String? template;
  final String? shopName;
  final String? headline;
  final DateTime? updatedAt;

  bool get inFlight => status == 'submitted' || status == 'running';

  Map<String, dynamic> toJson() => {
    'status': status,
    'session_id': ?sessionId,
    'job_id': ?jobId,
    'asset_id': ?assetId,
    'template': ?template,
    'shop_name': ?shopName,
    'headline': ?headline,
    'updated_at': ?updatedAt?.toUtc().toIso8601String(),
  };

  FirstVideoPrefs copyWith({
    String? status,
    String? sessionId,
    String? jobId,
    String? assetId,
    String? template,
    String? shopName,
    String? headline,
    DateTime? updatedAt,
  }) => FirstVideoPrefs(
    status: status ?? this.status,
    sessionId: sessionId ?? this.sessionId,
    jobId: jobId ?? this.jobId,
    assetId: assetId ?? this.assetId,
    template: template ?? this.template,
    shopName: shopName ?? this.shopName,
    headline: headline ?? this.headline,
    updatedAt: updatedAt ?? this.updatedAt,
  );
}

/// What a fresh sign-in learned about the account's first video, and so where
/// it lands (`Paths.postAuthDestination`).
enum FirstVideoEntry {
  /// `extra.first_video` absent: the guided flow, from its first screen.
  fresh,

  /// Submitted or running: straight to the making page.
  making,

  /// Ready, failed, skipped — or the preferences could not be read: the
  /// regular destination.
  settled,
}

FirstVideoEntry firstVideoEntryFromPreferences(Map<String, dynamic> prefs) {
  final extra = asMap(prefs['extra']);
  if (!extra.containsKey('first_video')) return FirstVideoEntry.fresh;
  final saved = FirstVideoPrefs.fromJson(extra['first_video']);
  if (saved == null) return FirstVideoEntry.fresh;
  return saved.inFlight ? FirstVideoEntry.making : FirstVideoEntry.settled;
}
