import 'package:bossip_mobile/features/workspace/api/workspace_api.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('legacy servers use individual deletes, preserving failures', () async {
    final calls = <(String, String)>[];
    final dio = Dio()
      ..interceptors.add(
        InterceptorsWrapper(
          onRequest: (request, handler) {
            calls.add((request.method, request.path));
            final status = request.method == 'POST'
                ? 404
                : request.path.endsWith('/one')
                ? 200
                : 403;
            final response = Response<dynamic>(
              requestOptions: request,
              statusCode: status,
            );
            if (status == 200) {
              handler.resolve(response);
            } else {
              handler.reject(
                DioException.badResponse(
                  statusCode: status,
                  requestOptions: request,
                  response: response,
                ),
              );
            }
          },
        ),
      );

    final result = await WorkspaceApi(dio).deleteSessions(['one', 'two']);
    expect(result.deleted, ['one']);
    expect(result.failed, ['two']);
    expect(calls, [
      ('POST', '/api/agent/session/batch-delete'),
      ('DELETE', '/api/agent/session/one'),
      ('DELETE', '/api/agent/session/two'),
    ]);
  });
}
