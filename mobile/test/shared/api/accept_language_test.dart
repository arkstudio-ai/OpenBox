import 'dart:convert';
import 'dart:typed_data';

import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/http_client.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:cookie_jar/cookie_jar.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

class _Adapter implements HttpClientAdapter {
  _Adapter(this.handle);
  final Future<ResponseBody> Function(RequestOptions) handle;
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) => handle(options);
  @override
  void close({bool force = false}) {}
}

ResponseBody _ok() => ResponseBody.fromString(
  jsonEncode({}),
  200,
  headers: {
    Headers.contentTypeHeader: ['application/json'],
  },
);

void main() {
  // docs/LIGHT_MODE_I18N_PLAN.md §2.1: the server renders Light copy in the
  // language the App is in, read from this header on every request.
  test('API requests carry the active device language and time zone', () async {
    var lang = 'zh-CN';
    var zone = 'Asia/Shanghai';
    final dio = buildApiDio(
      auth: AuthSession()..accessToken = 'token',
      cookieJar: CookieJar(),
      workspace: WorkspaceScope(),
      language: () => lang,
      timezone: () async => zone,
    );
    final seen = <(String?, String?)>[];
    dio.httpClientAdapter = _Adapter((options) async {
      seen.add((
        options.headers['Accept-Language'] as String?,
        options.headers['X-Time-Zone'] as String?,
      ));
      return _ok();
    });
    await dio.get<dynamic>('/api/light/entries');
    lang = 'vi-VN'; // the person switched language: the next request says so
    zone = 'Asia/Ho_Chi_Minh';
    await dio.get<dynamic>('/api/light/entries');
    await dio.get<dynamic>(
      '/api/light/entries',
      options: Options(
        headers: {
          'Accept-Language': 'en-US',
          'X-Time-Zone': 'America/Los_Angeles',
        },
      ),
    );
    expect(seen, [
      ('zh-CN', 'Asia/Shanghai'),
      ('vi-VN', 'Asia/Ho_Chi_Minh'),
      ('en-US', 'America/Los_Angeles'),
    ]);
    dio.close();
  });

  test('a client built without a language source sends no header', () async {
    final dio = buildApiDio(
      auth: AuthSession(),
      cookieJar: CookieJar(),
      workspace: WorkspaceScope(),
    );
    String? seen = 'unset';
    dio.httpClientAdapter = _Adapter((options) async {
      seen = options.headers['Accept-Language'] as String?;
      return _ok();
    });
    await dio.get<dynamic>('/api/environment');
    expect(seen, isNull);
    dio.close();
  });
}
