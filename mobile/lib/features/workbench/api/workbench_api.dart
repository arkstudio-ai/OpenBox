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

  /// The session's project identity and physical workdir.
  ///
  /// Only [SessionWorkspace.directory] is sent back to file APIs. The UI uses
  /// [SessionWorkspace.projectName], never the physical namespace basename.
  /// The project-list fallback keeps this correct during a rolling backend
  /// deployment where `project_name` is not present on the session response.
  Future<SessionWorkspace> sessionWorkspace(String sessionId) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/agent/session/$sessionId',
    );
    final data = resp.data ?? const <String, dynamic>{};
    final projectId = asString(data['project_id']);
    var projectName = asString(data['project_name']);
    if ((projectName == null || projectName.isEmpty) &&
        projectId != null &&
        projectId.isNotEmpty) {
      final projects = await _dio.get<List<dynamic>>('/api/agent/project');
      for (final raw in projects.data ?? const <dynamic>[]) {
        if (raw is! Map<String, dynamic>) continue;
        if (asString(raw['id']) == projectId) {
          projectName = asString(raw['name']);
          break;
        }
      }
    }
    return SessionWorkspace(
      directory: asString(data['directory']),
      projectId: projectId,
      projectName: projectName,
    );
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

class SessionWorkspace {
  const SessionWorkspace({
    required this.directory,
    required this.projectId,
    required this.projectName,
  });

  final String? directory;
  final String? projectId;
  final String? projectName;
}

final workbenchApiProvider = Provider<WorkbenchApi>(
  (ref) => WorkbenchApi(ref.watch(apiDioProvider)),
);
