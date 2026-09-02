import 'json.dart';
import 'token_usage.dart';

/// Mirrors backend `Session.model_dump()` / TS `Session`
/// (frontend-v2 `shared/types/api.ts:17-31`).
enum SessionStatus { idle, busy, retry, error, compacting, finalizing }

SessionStatus sessionStatusFrom(String? value) => switch (value) {
  'busy' => SessionStatus.busy,
  'retry' => SessionStatus.retry,
  'error' => SessionStatus.error,
  'compacting' => SessionStatus.compacting,
  'finalizing' => SessionStatus.finalizing,
  _ => SessionStatus.idle,
};

/// Whether an event belongs to the newest Agent Driver generation observed.
///
/// Status events use [rejectLegacyAfterSeen] because an unversioned idle/error
/// frame must not move a versioned newer run backwards. Other transcript
/// events keep accepting unversioned frames for rolling-deploy compatibility
/// and for user-authored messages that do not belong to an Agent generation.
bool acceptsEventGeneration(
  int? current,
  int? incoming, {
  bool rejectLegacyAfterSeen = false,
}) {
  if (incoming == null) {
    return !rejectLegacyAfterSeen || current == null;
  }
  return current == null || incoming >= current;
}

class Session {
  const Session({
    required this.id,
    required this.title,
    required this.agent,
    required this.model,
    required this.status,
    this.variant,
    this.videoModel,
    this.videoResolution,
    required this.createdAt,
    required this.updatedAt,
    this.sandboxId,
    this.additions = 0,
    this.deletions = 0,
    this.filesChanged = 0,
    this.tokenUsage,
    this.slug = '',
    this.projectId = 'default',
    this.parentId,
    this.directory,
    this.kind = 'chat',
  });

  factory Session.fromJson(Map<String, dynamic> json) => Session(
        id: asString(json['id']) ?? '',
        title: asString(json['title']) ?? '',
        agent: asString(json['agent']) ?? 'build',
        model: asString(json['model']) ?? '',
        videoModel: asString(json['video_model']),
        videoResolution: asString(json['video_resolution']),
        status: sessionStatusFrom(asString(json['status'])),
        variant: asString(json['variant']),
        createdAt: asDate(json['created_at']) ?? DateTime.now(),
        updatedAt: asDate(json['updated_at']) ?? DateTime.now(),
        sandboxId: asString(json['sandbox_id']),
        additions: asInt(json['additions']) ?? 0,
        deletions: asInt(json['deletions']) ?? 0,
        filesChanged: asInt(json['files_changed']) ?? 0,
        tokenUsage: json['token_usage'] is Map<String, dynamic>
            ? TokenUsage.fromJson(json['token_usage'] as Map<String, dynamic>)
            : null,
        slug: asString(json['slug']) ?? '',
        projectId: asString(json['project_id']) ?? 'default',
        parentId: asString(json['parent_id']),
        directory: asString(json['directory']),
        kind: asString(json['kind']) ?? 'chat',
      );

  final String id;
  final String title;
  final String agent;
  final String model;

  /// The video model this conversation generates with, and the resolution
  /// chosen beside it. Independent of `model`: a segment freezes both at
  /// submission, so switching only reaches work not yet started.
  final String? videoModel;
  final String? videoResolution;

  /// Persisted reasoning strength; null delegates to the model's default.
  final String? variant;

  final SessionStatus status;
  final DateTime createdAt;
  final DateTime updatedAt;
  final String? sandboxId;
  final int additions;
  final int deletions;
  final int filesChanged;
  final TokenUsage? tokenUsage;
  final String slug;
  final String projectId;
  final String? parentId;

  /// Only present on `GET /session/{id}` (the canonical project workdir).
  final String? directory;

  /// `chat` (default) or `cron` — a scheduled run's transcript session.
  final String kind;

  bool get isCron => kind == 'cron';

  bool get isLive =>
      status == SessionStatus.busy ||
      status == SessionStatus.retry ||
      status == SessionStatus.compacting ||
      status == SessionStatus.finalizing;

  Session copyWith({String? title, SessionStatus? status, TokenUsage? tokenUsage}) =>
      Session(
        id: id,
        title: title ?? this.title,
        agent: agent,
        model: model,
        variant: variant,
        videoModel: videoModel,
        videoResolution: videoResolution,
        status: status ?? this.status,
        createdAt: createdAt,
        updatedAt: updatedAt,
        sandboxId: sandboxId,
        additions: additions,
        deletions: deletions,
        filesChanged: filesChanged,
        tokenUsage: tokenUsage ?? this.tokenUsage,
        slug: slug,
        projectId: projectId,
        parentId: parentId,
        directory: directory,
        kind: kind,
      );
}

/// Which retry a stalled run is on, carried by `session.status` when the
/// status is `retry` (web `StreamState.retry`).
class RetryProgress {
  const RetryProgress({required this.attempt, required this.maxAttempts});

  final int attempt;
  final int maxAttempts;
}
