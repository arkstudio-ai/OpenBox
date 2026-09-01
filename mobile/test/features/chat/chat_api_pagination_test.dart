import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('complete history prepends every newest-first API page', () async {
    final calls = <int>[];
    final dio = Dio(BaseOptions(baseUrl: 'https://openbox.invalid'));
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          final offset = options.queryParameters['offset'] as int;
          final limit = options.queryParameters['limit'] as int;
          calls.add(offset);
          final newestEnd = 405 - offset;
          final oldest = (newestEnd - limit + 1).clamp(1, 405);
          final page = newestEnd < 1
              ? <dynamic>[]
              : <dynamic>[
                  for (var value = oldest; value <= newestEnd; value++)
                    {
                      'id': 'm${value.toString().padLeft(3, '0')}',
                      'session_id': 'session-1',
                      'role': value.isOdd ? 'user' : 'assistant',
                      'parts': <dynamic>[],
                    },
                ];
          handler.resolve(
            Response<List<dynamic>>(
              requestOptions: options,
              statusCode: 200,
              data: page,
            ),
          );
        },
      ),
    );

    final messages = await ChatApi(
      dio,
    ).listAllMessages('session-1', pageSize: 200);

    expect(messages, hasLength(405));
    expect(messages.first.id, 'm001');
    expect(messages.last.id, 'm405');
    expect(calls, const [0, 200, 400]);
  });

  test('complete history rejects a non-positive page size', () async {
    final api = ChatApi(Dio());
    expect(
      () => api.listAllMessages('session-1', pageSize: 0),
      throwsArgumentError,
    );
  });
}
