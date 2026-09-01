import 'package:bossip_mobile/features/workbench/api/workbench_api.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

Dio _dioFor(
  Map<String, dynamic> session,
  List<Map<String, dynamic>> projects,
  List<String> requests,
) {
  final dio = Dio(BaseOptions(baseUrl: 'https://openbox.invalid'));
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        requests.add(options.path);
        final Object body = options.path == '/api/agent/project'
            ? projects
            : session;
        handler.resolve(
          Response<dynamic>(
            requestOptions: options,
            statusCode: 200,
            data: body,
          ),
        );
      },
    ),
  );
  return dio;
}

void main() {
  test('session workspace uses the user-facing project name', () async {
    final requests = <String>[];
    final api = WorkbenchApi(
      _dioFor(
        {
          'directory': '/workspace/openbox/users/u-a/projects/p-hash-name',
          'project_id': 'project-1',
          'project_name': '中文项目',
        },
        const [],
        requests,
      ),
    );

    final workspace = await api.sessionWorkspace('session-1');

    expect(
      workspace.directory,
      '/workspace/openbox/users/u-a/projects/p-hash-name',
    );
    expect(workspace.projectId, 'project-1');
    expect(workspace.projectName, '中文项目');
    expect(requests, ['/api/agent/session/session-1']);
  });

  test(
    'rolling-deploy fallback resolves project name from project list',
    () async {
      final requests = <String>[];
      final api = WorkbenchApi(
        _dioFor(
          {
            'directory': '/workspace/openbox/users/u-a/projects/p-hash-name',
            'project_id': 'project-1',
          },
          const [
            {'id': 'project-1', 'name': '验收项目'},
          ],
          requests,
        ),
      );

      final workspace = await api.sessionWorkspace('session-1');

      expect(workspace.projectName, '验收项目');
      expect(requests, ['/api/agent/session/session-1', '/api/agent/project']);
    },
  );
}
