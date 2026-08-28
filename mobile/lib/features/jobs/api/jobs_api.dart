import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/skill_job.dart';
import '../../../shared/ws/ws_client.dart';

/// Skill job transport + providers (web `features/jobs/api/jobs.ts` +
/// `useSkillJobLiveEvents`): the snapshot GET is authoritative, every
/// `skill.job.event` invalidates, 30s poll backstops a lost socket.
class JobsApi {
  JobsApi(this._dio);

  final Dio _dio;

  Future<List<SkillJobSnapshot>> listForSession(String sessionId) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/skill-jobs',
      queryParameters: {'session_id': sessionId},
    );
    final jobs = resp.data?['jobs'];
    return [
      for (final item in (jobs is List ? jobs : const []))
        if (item is Map<String, dynamic>) SkillJobSnapshot.fromJson(item),
    ];
  }

  Future<void> cancel(String jobId) async {
    await _dio.post<dynamic>('/api/skill-jobs/$jobId/cancel');
  }

  Future<void> answer(String jobId, Map<String, dynamic> payload,
      {required String idempotencyKey}) async {
    await _dio.post<dynamic>('/api/skill-jobs/$jobId/inputs', data: {
      'payload': payload,
      'idempotency_key': idempotencyKey,
    });
  }
}

final jobsApiProvider = Provider<JobsApi>((ref) => JobsApi(ref.watch(apiDioProvider)));

void _wireJobsInvalidation(Ref ref) {
  final sub = ref.watch(wsClientProvider).events.listen((event) {
    if (event.type == 'skill.job.event') ref.invalidateSelf();
  });
  ref.onDispose(sub.cancel);
  final timer = Timer.periodic(const Duration(seconds: 30), (_) => ref.invalidateSelf());
  ref.onDispose(timer.cancel);
}

final sessionSkillJobsProvider =
    FutureProvider.family.autoDispose<List<SkillJobSnapshot>, String>((ref, sessionId) {
  _wireJobsInvalidation(ref);
  return ref.watch(jobsApiProvider).listForSession(sessionId);
});
