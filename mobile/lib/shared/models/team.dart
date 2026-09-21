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

/// One saved Agent or team template. The library lists both kinds and the
/// composer's picker reads the team ones, so they share a shape.
class TeamDefinition {
  TeamDefinition.fromJson(Map<String, dynamic> json)
    : id = asString(json['id']) ?? '',
      name = asString(json['name']) ?? '',
      status = asString(json['status']) ?? '',
      source = asString(json['source']) ?? '',
      runCount = asInt(json['run_count']),
      spec = asMap(asMap(json['version'])['spec']),
      capability = asMap(asMap(json['version'])['capability_summary']),
      memberPreviews = asList(json['member_previews']).map(asMap).toList(),
      sourceSessionId = asString(asMap(json['provenance'])['session_id']);

  final String id, name, status, source;
  final int? runCount;
  final Map<String, dynamic> spec, capability;
  final List<Map<String, dynamic>> memberPreviews;

  /// The conversation this definition was created from, when an Agent made it.
  final String? sourceSessionId;

  /// A team's purpose, or an Agent's "when to use" — the one line the list
  /// shows under the name.
  String get description =>
      asString(spec['description']) ?? asString(spec['when_to_use']) ?? '';
  bool get isTeam => spec.containsKey('preset_members');
  bool get allowSupplement =>
      asMap(spec['policy'])['member_selection'] != 'explicit_only';
  List<String> get memberAliases => [
    for (final member in asList(spec['preset_members']).map(asMap))
      asString(member['alias']) ?? asString(member['name']) ?? '',
  ].where((alias) => alias.isNotEmpty).toList();

  /// Paid media tools spend account credits, so the list calls them out.
  bool get containsPaidTools =>
      asMap(capability['tool_tiers']).values.contains('T2');
}

/// A row of the run history (web `TeamRunInfo`). Read from `team_runs`
/// directly — the list never replays an event stream (§13.8).
class TeamRunInfo {
  TeamRunInfo.fromJson(Map<String, dynamic> json)
    : id = asString(json['id']) ?? '',
      title = asString(json['title']) ?? '',
      rootSessionId = asString(json['root_session_id']) ?? '',
      projectId = asString(json['project_id']) ?? '',
      templateId = asString(json['template_id']),
      state = asString(json['state']) ?? '',
      createdAt = DateTime.tryParse(asString(json['created_at']) ?? ''),
      summary = asMap(json['summary']),
      usage = json['usage'] == null ? null : asMap(json['usage']);

  final String id, title, rootSessionId, projectId, state;
  final String? templateId;
  final DateTime? createdAt;
  final Map<String, dynamic> summary;
  final Map<String, dynamic>? usage;

  bool get terminal =>
      const ['completed', 'canceled', 'failed'].contains(state);
  bool get needsAttention => summary['needs_attention'] == true;
  String? get pauseReason => asString(summary['pause_reason']);
  int? get taskCount => asInt(summary['task_count']);
}

class TeamRun {
  TeamRun.fromJson(Map<String, dynamic> json)
    : id = asString(json['id']) ?? '',
      rootSessionId = asString(json['root_session_id']) ?? '',
      title = asString(json['title']) ?? '',
      state = asString(json['state']) ?? '',
      revision = asInt(json['revision']) ?? 0,
      finalSummary = asString(json['final_summary']) ?? '',
      goal = asString(json['goal']) ?? '',
      pauseReason = asString(json['pause_reason']),
      failureReason = asString(json['failure_reason']),
      capacityRetryAt = asString(json['capacity_retry_at']);
  final String id, rootSessionId, title, state, finalSummary;

  /// What the owner originally asked for. Only the snapshot carries it; the
  /// history list stays lean, so running a past team again reads it here.
  final String goal;
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
