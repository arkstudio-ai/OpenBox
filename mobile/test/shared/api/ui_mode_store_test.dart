import 'dart:convert';
import 'dart:typed_data';

import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/api/ui_mode_store.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/models/ui_mode.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _Adapter implements HttpClientAdapter {
  _Adapter(this.handle);
  final Future<Map<String, dynamic>> Function(RequestOptions) handle;
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async => ResponseBody.fromString(
    jsonEncode(await handle(options)),
    200,
    headers: {
      Headers.contentTypeHeader: ['application/json'],
    },
  );

  @override
  void close({bool force = false}) {}
}

class _Auth extends AuthController {
  _Auth(this.id);
  final String id;
  @override
  AuthState build() => AuthState(
    isLoading: false,
    user: AuthUser(id: id, username: id, role: 'user'),
  );
  void switchTo(String next) => state = AuthState(
    isLoading: false,
    user: AuthUser(id: next, username: next, role: 'user'),
  );
}

void main() {
  late SharedPreferences prefs;
  late Dio dio;
  late List<RequestOptions> requests;
  var serverMode = 'pro';
  var serverDown = false;

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    prefs = await SharedPreferences.getInstance();
    requests = [];
    serverMode = 'pro';
    serverDown = false;
    dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
    dio.httpClientAdapter = _Adapter((o) async {
      requests.add(o);
      if (serverDown) throw DioException(requestOptions: o);
      if (o.method == 'PUT') serverMode = (o.data as Map)['ui_mode'] as String;
      return {'ui_mode': serverMode};
    });
  });

  ProviderContainer container({String user = 'u1'}) {
    final c = ProviderContainer(
      overrides: [
        prefsProvider.overrideWithValue(prefs),
        apiDioProvider.overrideWithValue(dio),
        authProvider.overrideWith(() => _Auth(user)),
      ],
    );
    addTearDown(c.dispose);
    return c;
  }

  test('the wire value: only an explicit light is light', () {
    expect(uiModeFromWire('light'), UiMode.light);
    expect(uiModeFromWire('pro'), UiMode.pro);
    expect(uiModeFromWire(null), UiMode.pro);
    expect(uiModeFromWire('dark'), UiMode.pro);
    expect(
      uiModeFromPreferences({
        'extra': {'ui_mode': 'light'},
      }),
      UiMode.light,
    );
    expect(uiModeFromPreferences({'extra': <String, dynamic>{}}), UiMode.pro);
    expect(uiModeFromPreferences({}), UiMode.pro);
    expect(UiMode.light.wire, 'light');
  });

  test('an account without a cache is pro until the server says light', () async {
    serverMode = 'light';
    final c = container();
    expect(c.read(uiModeProvider).mode, UiMode.pro);
    expect(c.read(uiModeProvider).loaded, isFalse);
    await c.read(uiModeProvider.notifier).whenLoaded();
    expect(c.read(uiModeProvider).mode, UiMode.light);
    expect(c.read(uiModeProvider).loaded, isTrue);
    expect(requests.single.path, '/api/light/mode');
    expect(requests.single.method, 'GET');
    expect(prefs.getString(UiModeController.cacheKey('u1')), 'light');
  });

  test('the cache decides the first paint, the server the rest', () async {
    await prefs.setString(UiModeController.cacheKey('u1'), 'light');
    serverMode = 'pro';
    final c = container();
    expect(c.read(uiModeProvider).mode, UiMode.light);
    await c.read(uiModeProvider.notifier).whenLoaded();
    expect(c.read(uiModeProvider).mode, UiMode.pro);
    expect(prefs.getString(UiModeController.cacheKey('u1')), 'pro');
  });

  test('offline keeps the cache and never flips an account', () async {
    await prefs.setString(UiModeController.cacheKey('u1'), 'light');
    serverDown = true;
    final c = container();
    await c.read(uiModeProvider.notifier).whenLoaded();
    expect(c.read(uiModeProvider).mode, UiMode.light);
    expect(c.read(uiModeProvider).loaded, isFalse);
  });

  test('the switch writes through and comes back when refused', () async {
    final c = container();
    await c.read(uiModeProvider.notifier).setMode(UiMode.light);
    expect(c.read(uiModeProvider).mode, UiMode.light);
    expect(requests.single.method, 'PUT');
    expect(requests.single.path, '/api/light/mode');
    expect(requests.single.data, {'ui_mode': 'light'});
    expect(prefs.getString(UiModeController.cacheKey('u1')), 'light');

    serverDown = true;
    await expectLater(
      c.read(uiModeProvider.notifier).setMode(UiMode.pro),
      throwsA(isA<DioException>()),
    );
    expect(c.read(uiModeProvider).mode, UiMode.light);
    expect(prefs.getString(UiModeController.cacheKey('u1')), 'light');
  });

  test('a sign-in primes the next account before it is published', () async {
    final c = container();
    c.read(uiModeProvider.notifier).prime('u2', UiMode.light);
    expect(c.read(uiModeProvider).mode, UiMode.pro, reason: 'still u1');
    (c.read(authProvider.notifier) as _Auth).switchTo('u2');
    expect(c.read(uiModeProvider).userId, 'u2');
    expect(c.read(uiModeProvider).mode, UiMode.light);
    c.read(uiModeProvider.notifier).adopt(UiMode.light);
    expect(c.read(uiModeProvider).loaded, isTrue);
    await c.read(uiModeProvider.notifier).whenLoaded();
    expect(requests, isEmpty, reason: 'adopted: no cold-start fetch');
  });

  test('a widget test without preferences reads as pro', () {
    final c = ProviderContainer(
      overrides: [authProvider.overrideWith(() => _Auth('u1'))],
    );
    addTearDown(c.dispose);
    expect(c.read(uiModeProvider).mode, UiMode.pro);
    expect(c.read(uiModeProvider).isLight, isFalse);
  });
}
