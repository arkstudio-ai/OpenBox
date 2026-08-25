import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/cron.dart';
import '../../../shared/ws/ws_client.dart';

/// Cron transport + providers (web `features/cron/api/cron.ts` +
/// `useCronLiveEvents`): jobs poll at 30s, status at 60s, and every
/// `cron.job.*` WS lifecycle event invalidates both.
class CronApi {
  CronApi(this._dio);

  final Dio _dio;

  Future<List<CronJob>> listJobs() async {
    final resp = await _dio.get<List<dynamic>>('/api/cron/jobs');
    return (resp.data ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(CronJob.fromJson)
        .toList();
  }

  Future<CronStatus> status() async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/cron/status');
    return CronStatus.fromJson(resp.data ?? const {});
  }

  Future<List<CronRun>> listRuns(String jobId) async {
    final resp = await _dio.get<List<dynamic>>('/api/cron/jobs/$jobId/runs');
    return (resp.data ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(CronRun.fromJson)
        .toList();
  }

  Future<void> create({
    required String projectId,
    required String name,
    required CronSchedule schedule,
    required String taskPrompt,
  }) async {
    await _dio.post<dynamic>('/api/cron/jobs', data: {
      'project_id': projectId,
      'name': name,
      'schedule': schedule.toJson(),
      'task_prompt': taskPrompt,
    });
  }

  Future<void> update(String jobId, Map<String, dynamic> patch) async {
    await _dio.patch<dynamic>('/api/cron/jobs/$jobId', data: patch);
  }

  Future<void> delete(String jobId) async {
    await _dio.delete<dynamic>('/api/cron/jobs/$jobId');
  }

  Future<void> runNow(String jobId) async {
    await _dio.post<dynamic>('/api/cron/jobs/$jobId/run');
  }

  Future<void> pauseAll() async {
    await _dio.post<dynamic>('/api/cron/jobs/pause-all');
  }

  Future<void> resumeAll() async {
    await _dio.post<dynamic>('/api/cron/jobs/resume-all');
  }

  /// Project options for job creation (web `cron/api/projects.ts` — its own
  /// fetch, features never import each other).
  Future<List<(String, String)>> listProjects() async {
    final resp = await _dio.get<List<dynamic>>('/api/agent/project');
    return [
      for (final item in (resp.data ?? const []))
        if (item is Map<String, dynamic>)
          (
            (item['id'] as String?) ?? '',
            ((item['name'] as String?)?.trim().isNotEmpty ?? false)
                ? (item['name'] as String).trim()
                : (item['id'] as String?) ?? '',
          ),
    ];
  }
}

final cronApiProvider =
    Provider<CronApi>((ref) => CronApi(ref.watch(apiDioProvider)));

const _cronEvents = {
  'cron.job.created',
  'cron.job.updated',
  'cron.job.started',
  'cron.job.completed',
  'cron.job.failed',
  'cron.job.injected',
  'cron.job.auto_disabled',
};

void _wireCronInvalidation(Ref ref, {Duration? poll}) {
  final sub = ref.watch(wsClientProvider).events.listen((event) {
    if (_cronEvents.contains(event.type)) ref.invalidateSelf();
  });
  ref.onDispose(sub.cancel);
  if (poll != null) {
    final timer = Timer.periodic(poll, (_) => ref.invalidateSelf());
    ref.onDispose(timer.cancel);
  }
}

/// A running job flips status server-side without a client action → 30s poll.
final cronJobsProvider = FutureProvider<List<CronJob>>((ref) {
  _wireCronInvalidation(ref, poll: const Duration(seconds: 30));
  return ref.watch(cronApiProvider).listJobs();
});

final cronStatusProvider = FutureProvider<CronStatus>((ref) {
  _wireCronInvalidation(ref, poll: const Duration(seconds: 60));
  return ref.watch(cronApiProvider).status();
});

final cronRunsProvider =
    FutureProvider.family<List<CronRun>, String>((ref, jobId) {
  _wireCronInvalidation(ref);
  return ref.watch(cronApiProvider).listRuns(jobId);
});

final cronProjectsProvider = FutureProvider<List<(String, String)>>(
  (ref) => ref.watch(cronApiProvider).listProjects(),
);
