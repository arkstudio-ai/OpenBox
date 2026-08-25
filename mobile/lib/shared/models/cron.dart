import 'json.dart';

/// Wire types for `/api/cron/*`, mirroring frontend-v2
/// `features/cron/types/index.ts` (backend `_job_to_dict`/`_run_to_dict`).
sealed class CronSchedule {
  const CronSchedule();

  static CronSchedule fromJson(Map<String, dynamic> json) {
    switch (asString(json['kind'])) {
      case 'at':
        return CronScheduleAt(at: asString(json['at']) ?? '');
      case 'every':
        return CronScheduleEvery(
          everyMs: asInt(json['every_ms']) ?? 0,
          anchorMs: asInt(json['anchor_ms']),
        );
      default:
        return CronScheduleCron(
          expr: asString(json['expr']) ?? '',
          tz: asString(json['tz']),
        );
    }
  }

  Map<String, dynamic> toJson();
}

class CronScheduleAt extends CronSchedule {
  const CronScheduleAt({required this.at});

  final String at;

  @override
  Map<String, dynamic> toJson() => {'kind': 'at', 'at': at};
}

class CronScheduleEvery extends CronSchedule {
  const CronScheduleEvery({required this.everyMs, this.anchorMs});

  final int everyMs;
  final int? anchorMs;

  @override
  Map<String, dynamic> toJson() => {'kind': 'every', 'every_ms': everyMs};
}

class CronScheduleCron extends CronSchedule {
  const CronScheduleCron({required this.expr, this.tz});

  final String expr;
  final String? tz;

  @override
  Map<String, dynamic> toJson() =>
      {'kind': 'cron', 'expr': expr, 'tz': ?tz};
}

class CronJob {
  const CronJob({
    required this.id,
    required this.projectId,
    required this.sessionId,
    required this.name,
    required this.enabled,
    required this.schedule,
    required this.taskPrompt,
    required this.nextRunAt,
    required this.lastRunAt,
    required this.lastStatus,
    required this.lastError,
    required this.totalRuns,
    required this.totalSuccesses,
    required this.totalFailures,
    required this.running,
    this.projectDirectory,
  });

  factory CronJob.fromJson(Map<String, dynamic> json) => CronJob(
        id: asString(json['id']) ?? '',
        projectId: asString(json['project_id']),
        sessionId: asString(json['session_id']),
        name: asString(json['name']) ?? '',
        enabled: asBool(json['enabled']) ?? false,
        schedule: CronSchedule.fromJson(asMap(json['schedule'])),
        taskPrompt: asString(json['task_prompt']) ?? '',
        nextRunAt: asDate(json['next_run_at']),
        lastRunAt: asDate(json['last_run_at']),
        lastStatus: asString(json['last_status']),
        lastError: asString(json['last_error']),
        totalRuns: asInt(json['total_runs']) ?? 0,
        totalSuccesses: asInt(json['total_successes']) ?? 0,
        totalFailures: asInt(json['total_failures']) ?? 0,
        running: asBool(json['running']) ?? false,
        projectDirectory: asString(json['project_directory']),
      );

  final String id;
  final String? projectId;

  /// Optional conversation results are posted into (chat-created jobs).
  final String? sessionId;
  final String name;
  final bool enabled;
  final CronSchedule schedule;
  final String taskPrompt;
  final DateTime? nextRunAt;
  final DateTime? lastRunAt;
  final String? lastStatus;
  final String? lastError;
  final int totalRuns;
  final int totalSuccesses;
  final int totalFailures;
  final bool running;
  final String? projectDirectory;

  /// Consecutive failures tripped the breaker (web `StateDot`).
  bool get autoDisabled => (lastError ?? '').startsWith('[auto-disabled');
}

class CronRun {
  const CronRun({
    required this.id,
    required this.status,
    required this.tempSessionId,
    required this.summaryText,
    required this.totalTokens,
    required this.durationMs,
    required this.startedAt,
  });

  factory CronRun.fromJson(Map<String, dynamic> json) => CronRun(
        id: asString(json['id']) ?? '',
        status: asString(json['status']) ?? 'skipped',
        tempSessionId: asString(json['temp_session_id']),
        summaryText: asString(json['summary_text']),
        totalTokens: asInt(json['total_tokens']) ?? 0,
        durationMs: asInt(json['duration_ms']) ?? 0,
        startedAt: asDate(json['started_at']),
      );

  final String id;
  final String status; // ok | error | skipped | running
  final String? tempSessionId;
  final String? summaryText;
  final int totalTokens;
  final int durationMs;
  final DateTime? startedAt;
}

class CronStatus {
  const CronStatus({
    required this.healthy,
    required this.lastTickAt,
    required this.enabledJobs,
    required this.runningJobs,
  });

  factory CronStatus.fromJson(Map<String, dynamic> json) => CronStatus(
        healthy: asBool(json['healthy']) ?? false,
        lastTickAt: asDate(json['last_tick_at']),
        enabledJobs: asInt(json['enabled_jobs']) ?? 0,
        runningJobs: asInt(json['running_jobs']) ?? 0,
      );

  final bool healthy;
  final DateTime? lastTickAt;
  final int enabledJobs;
  final int runningJobs;
}
