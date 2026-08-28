import 'json.dart';

/// Skill job snapshot (web `features/jobs/types` / backend
/// `skill_runtime/service.py job_snapshot`). The snapshot GET is the source
/// of truth; WS events only invalidate.
class SkillJobSnapshot {
  const SkillJobSnapshot({
    required this.jobId,
    required this.skillKey,
    required this.operation,
    required this.status,
    required this.phase,
    required this.phaseLabelKey,
    required this.desiredState,
    required this.progress,
    required this.result,
    required this.errorCode,
    required this.errorMessage,
    required this.lastEventSeq,
    required this.updatedAt,
    required this.completedAt,
  });

  final String jobId;
  final String skillKey;
  final String operation;
  final String status;
  final String? phase;
  final String? phaseLabelKey;
  final String desiredState;
  final Map<String, dynamic> progress;
  final Map<String, dynamic> result;
  final String? errorCode;
  final String? errorMessage;
  final int lastEventSeq;
  final DateTime? updatedAt;
  final DateTime? completedAt;

  static const terminalStatuses = {'succeeded', 'failed', 'cancelled'};

  bool get terminal => terminalStatuses.contains(status);

  String get displayName => skillKey.replaceFirst(RegExp(r'^(builtin|user):'), '');

  factory SkillJobSnapshot.fromJson(Map<String, dynamic> json) => SkillJobSnapshot(
        jobId: asString(json['jobId']) ?? '',
        skillKey: asString(json['skillKey']) ?? '',
        operation: asString(json['operation']) ?? '',
        status: asString(json['status']) ?? '',
        phase: asString(json['phase']),
        phaseLabelKey: asString(json['phaseLabelKey']),
        desiredState: asString(json['desiredState']) ?? 'run',
        progress: asMap(json['progress']),
        result: asMap(json['result']),
        errorCode: asString(json['errorCode']),
        errorMessage: asString(json['errorMessage']),
        lastEventSeq: asInt(json['lastEventSeq']) ?? 0,
        updatedAt: DateTime.tryParse(asString(json['updatedAt']) ?? ''),
        completedAt: DateTime.tryParse(asString(json['completedAt']) ?? ''),
      );
}

/// Active jobs first; terminal ones only while fresh (web `visibleJobs`).
List<SkillJobSnapshot> visibleSkillJobs(List<SkillJobSnapshot> jobs, {DateTime? now}) {
  final at = now ?? DateTime.now().toUtc();
  final active = jobs.where((j) => !j.terminal).toList();
  final recentTerminal = jobs
      .where((j) => j.terminal && j.completedAt != null)
      .where((j) => at.difference(j.completedAt!.toUtc()).inMinutes < 10)
      .take(3)
      .toList();
  return [...active, ...recentTerminal];
}
