import 'package:bossip_mobile/shared/api/containers_api.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test(
    'file search carries the current project scope and preserves Unicode',
    () async {
      RequestOptions? captured;
      final dio = Dio(BaseOptions(baseUrl: 'https://openbox.invalid'));
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) {
            captured = options;
            handler.resolve(
              Response<Map<String, dynamic>>(
                requestOptions: options,
                statusCode: 200,
                data: {
                  'files': ['资料/设计稿-你好😀.md'],
                },
              ),
            );
          },
        ),
      );

      final files = await ContainersApi(dio).searchFiles(
        'desktop-1',
        '你好😀',
        sessionId: 'session/一',
        projectId: 'project 二',
      );

      expect(files, ['资料/设计稿-你好😀.md']);
      expect(captured?.queryParameters, {
        'q': '你好😀',
        'limit': 20,
        'session_id': 'session/一',
        'project_id': 'project 二',
      });
    },
  );
}
