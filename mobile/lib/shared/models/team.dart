import 'json.dart';

typedef TeamScope = ({String userId, String workspaceId});

/// The next message's selection; never a permission grant or a team budget.
class TeamRequest {
  const TeamRequest({this.templateId, this.allowSupplement});
  final String? templateId;
  final bool? allowSupplement;
  Map<String, dynamic> toJson() => {
    'template_id': ?templateId,
    'allow_supplement': ?allowSupplement,
  };
}

class TeamTemplate {
  TeamTemplate.fromJson(Map<String, dynamic> json)
    : id = asString(json['id']) ?? '',
      name = asString(json['name']) ?? '',
      spec = asMap(asMap(json['version'])['spec']);
  final String id;
  final String name;
  final Map<String, dynamic> spec;
  String get description => asString(spec['description']) ?? '';
  bool get allowSupplement =>
      asMap(spec['policy'])['member_selection'] != 'explicit_only';
}

class TeamRun {
  TeamRun.fromJson(Map<String, dynamic> json)
    : id = asString(json['id']) ?? '',
      rootSessionId = asString(json['root_session_id']) ?? '',
      title = asString(json['title']) ?? '',
      state = asString(json['state']) ?? '',
      revision = asInt(json['revision']) ?? 0,
      finalSummary = asString(json['final_summary']) ?? '',
      pauseReason = asString(json['pause_reason']),
      failureReason = asString(json['failure_reason']),
      capacityRetryAt = asString(json['capacity_retry_at']);
  final String id, rootSessionId, title, state, finalSummary;
  final String? pauseReason, failureReason;

  /// Set while the run is waiting for an execution slot: the work is queued
  /// and will retry, which is not the same as stalled.
  final String? capacityRetryAt;
  final int revision;
  bool get terminal =>
      const ['completed', 'canceled', 'failed'].contains(state);
}

class TeamSnapshot {
  TeamSnapshot.fromJson(Map<String, dynamic> json)
    : id = asString(json['id']) ?? '',
      seq = asInt(json['seq']) ?? 0,
      run = TeamRun.fromJson(asMap(json['run'])),
      members = asList(json['members']).map(asMap).toList(),
      tasks = asList(json['tasks']).map(asMap).toList(),
      notices = asList(json['notices']).map(asMap).toList(),
      taskCount = asInt(json['task_count']) ?? 0,
      completedTaskCount = asInt(json['completed_task_count']) ?? 0;
  final String id;
  final int seq, taskCount, completedTaskCount;
  final TeamRun run;
  final List<Map<String, dynamic>> members, tasks, notices;
}
