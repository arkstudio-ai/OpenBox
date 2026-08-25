import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/container.dart';
import '../models/json.dart';
import 'providers.dart';

/// Container/sandbox transport, shared like web `shared/api/containers.ts`
/// — both chat (composer mentions/attachments) and workbench consume it.
class ContainersApi {
  ContainersApi(this._dio);

  final Dio _dio;

  Future<List<ContainerInfo>> list() async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/containers');
    return asList(resp.data?['containers'])
        .whereType<Map<String, dynamic>>()
        .map(ContainerInfo.fromJson)
        .toList();
  }

  Future<ContainerInfo> create() async {
    final resp = await _dio.post<Map<String, dynamic>>('/api/containers');
    return ContainerInfo.fromJson(resp.data ?? const {});
  }

  Future<SandboxStatus> sandboxStatus() async {
    final resp =
        await _dio.get<Map<String, dynamic>>('/api/agent/sandbox/status');
    return SandboxStatus.fromJson(resp.data ?? const {});
  }

  /// `@`-mention file search (web `useFileSearch`).
  Future<List<String>> searchFiles(String containerId, String query,
      {int limit = 20}) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/containers/$containerId/files/search',
      queryParameters: {'q': query, 'limit': limit},
    );
    return asList(resp.data?['files']).whereType<String>().toList();
  }
}

final containersApiProvider =
    Provider<ContainersApi>((ref) => ContainersApi(ref.watch(apiDioProvider)));

/// The user's running sandbox, if any (web `useRunningContainer`).
final runningContainerProvider = FutureProvider<ContainerInfo?>((ref) async {
  final containers = await ref.watch(containersApiProvider).list();
  for (final container in containers) {
    if (container.isRunning) return container;
  }
  return null;
});
