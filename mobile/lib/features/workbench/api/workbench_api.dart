import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/container.dart';
import '../../../shared/models/diff.dart';
import '../../../shared/models/json.dart';

/// Review/files/containers REST calls (web `features/workbench/api/*`).
class WorkbenchApi {
  WorkbenchApi(this._dio);

  final Dio _dio;

  Future<List<DiffEntry>> sessionDiff(String sessionId) async {
    final resp = await _dio.get<List<dynamic>>(
      '/api/agent/session/$sessionId/diff',
      queryParameters: {'full': true},
    );
    return (resp.data ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(DiffEntry.fromJson)
        .toList();
  }

  /// The session's project workdir (`directory` on `GET /session/{id}`),
  /// used as the files-tab root (web D.4.7 — never climb above it).
  Future<String?> sessionDirectory(String sessionId) async {
    final resp = await _dio
        .get<Map<String, dynamic>>('/api/agent/session/$sessionId');
    return asString(resp.data?['directory']);
  }

  Future<List<FileEntry>> listFiles(String containerId, String path) async {
    final resp = await _dio.post<dynamic>(
      '/api/containers/$containerId/files/list',
      data: {'path': path},
    );
    return FileEntry.listFrom(resp.data);
  }

  Future<FileContent> fileContent(String containerId, String path) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/containers/$containerId/files/content',
      queryParameters: {'path': path},
    );
    return FileContent.fromJson(resp.data ?? const {});
  }
}

final workbenchApiProvider =
    Provider<WorkbenchApi>((ref) => WorkbenchApi(ref.watch(apiDioProvider)));
