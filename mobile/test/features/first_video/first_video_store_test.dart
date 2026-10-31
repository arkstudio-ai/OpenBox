import 'dart:convert';
import 'dart:typed_data';

import 'package:bossip_mobile/features/first_video/state/first_video_store.dart';
import 'package:bossip_mobile/features/onboarding/state/onboarding_store.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/first_video_entry.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/models/first_video.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
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
  Map<String, dynamic> serverExtra = {};
  bool serverDown = false;

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    prefs = await SharedPreferences.getInstance();
    requests = [];
    serverExtra = {};
    serverDown = false;
    dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
    dio.httpClientAdapter = _Adapter((o) async {
      requests.add(o);
      if (serverDown) throw DioException(requestOptions: o);
      if (o.method == 'PUT') {
        final extra = (o.data as Map)['extra'];
        if (extra is Map) {
          serverExtra = {...serverExtra, ...Map<String, dynamic>.from(extra)};
        }
      }
      return {'extra': serverExtra};
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

  test('the server copy wins over the local cache', () async {
    await prefs.setString(
      'bossip:first_video:u1',
      jsonEncode({'status': 'submitted', 'session_id': 'cached'}),
    );
    serverExtra = {
      'first_video': {
        'status': 'ready',
        'session_id': 's1',
        'asset_id': 'asset_9',
        'template': 'promo',
      },
    };
    final c = container();
    // Cache bridges the first paint …
    expect(c.read(firstVideoProvider).prefs?.sessionId, 'cached');
    expect(c.read(firstVideoProvider).loaded, isFalse);
    // … then the server answers.
    await c.read(firstVideoProvider.notifier).whenLoaded();
    final state = c.read(firstVideoProvider);
    expect(state.loaded, isTrue);
    expect(state.prefs?.status, 'ready');
    expect(state.prefs?.assetId, 'asset_9');
    expect(state.prefs?.template, 'promo');
    expect(
      jsonDecode(prefs.getString('bossip:first_video:u1')!),
      containsPair('status', 'ready'),
    );
  });

  test('an account that never started has no record', () async {
    final c = container();
    await c.read(firstVideoProvider.notifier).whenLoaded();
    expect(c.read(firstVideoProvider).started, isFalse);
    expect(c.read(firstVideoProvider).loaded, isTrue);
    expect(prefs.getString('bossip:first_video:u1'), isNull);
  });

  test('offline keeps the cache and still unblocks', () async {
    await prefs.setString(
      'bossip:first_video:u1',
      jsonEncode({'status': 'skipped'}),
    );
    serverDown = true;
    final c = container();
    await c.read(firstVideoProvider.notifier).whenLoaded();
    expect(c.read(firstVideoProvider).skipped, isTrue);
    expect(c.read(firstVideoProvider).loaded, isTrue);
  });

  test('skipping writes only extra.first_video, as a whole object', () async {
    final c = container();
    c.read(firstVideoEntryProvider.notifier).state = FirstVideoEntry.fresh;
    await c.read(firstVideoProvider.notifier).markSkipped();
    final put = requests.singleWhere((r) => r.method == 'PUT');
    final body = put.data as Map;
    expect(body.keys, ['extra']);
    final record = (body['extra'] as Map)['first_video'] as Map;
    expect(record['status'], 'skipped');
    expect(DateTime.tryParse(record['updated_at'] as String), isNotNull);
    expect(body.containsKey('onboarding'), isFalse);
    expect(c.read(firstVideoProvider).skipped, isTrue);
    expect(c.read(firstVideoEntryProvider), FirstVideoEntry.settled);
  });

  test('a job seen by the flow is mirrored locally without a write', () async {
    final c = container();
    c
        .read(firstVideoProvider.notifier)
        .noteJob(
          const FirstVideoJob(
            status: 'submitted',
            sessionId: 's1',
            jobId: 'j1',
            etaSeconds: 90,
          ),
          template: 'signature',
          shopName: '巷口小馆',
        );
    final state = c.read(firstVideoProvider);
    expect(state.inFlight, isTrue);
    expect(state.prefs?.sessionId, 's1');
    expect(state.prefs?.shopName, '巷口小馆');
    expect(requests.where((r) => r.method == 'PUT'), isEmpty);
  });

  test('a user switch drops the other account\'s record', () async {
    serverExtra = {
      'first_video': {'status': 'ready', 'session_id': 's1'},
    };
    final c = container();
    await c.read(firstVideoProvider.notifier).whenLoaded();
    expect(c.read(firstVideoProvider).prefs?.sessionId, 's1');
    (c.read(authProvider.notifier) as _Auth).switchTo('u2');
    expect(c.read(firstVideoProvider).userId, 'u2');
    expect(c.read(firstVideoProvider).prefs, isNull);
    expect(c.read(firstVideoProvider).loaded, isFalse);
  });

  test('the onboarding replay never touches extra.first_video', () async {
    final c = container();
    await c.read(onboardingProvider.notifier).reset();
    final put = requests.singleWhere((r) => r.method == 'PUT');
    expect((put.data as Map).keys, ['onboarding']);
  });

  group('sign-in entry', () {
    test('only "offered" opens the flow; in flight → making; else settled', () {
      expect(
        firstVideoEntryFromPreferences({
          'extra': {
            'first_video': {'status': 'offered'},
          },
        }),
        FirstVideoEntry.fresh,
      );
      // No record: an account from before the flow existed, never replayed.
      expect(firstVideoEntryFromPreferences({}), FirstVideoEntry.settled);
      expect(
        firstVideoEntryFromPreferences({'extra': <String, dynamic>{}}),
        FirstVideoEntry.settled,
      );
      expect(
        firstVideoEntryFromPreferences({
          'extra': {'first_video': 'junk'},
        }),
        FirstVideoEntry.settled,
      );
      for (final status in ['submitted', 'running']) {
        expect(
          firstVideoEntryFromPreferences({
            'extra': {
              'first_video': {'status': status},
            },
          }),
          FirstVideoEntry.making,
          reason: status,
        );
      }
      for (final status in ['ready', 'failed', 'skipped']) {
        expect(
          firstVideoEntryFromPreferences({
            'extra': {
              'first_video': {'status': status},
            },
          }),
          FirstVideoEntry.settled,
          reason: status,
        );
      }
    });

    test('the destination follows the entry; a deep link keeps its own', () {
      final plain = Uri.parse('/login');
      expect(
        Paths.postAuthDestination(plain, entry: FirstVideoEntry.fresh),
        Paths.firstVideo,
      );
      expect(
        Paths.postAuthDestination(plain, entry: FirstVideoEntry.making),
        Paths.firstVideoMaking,
      );
      expect(
        Paths.postAuthDestination(plain, entry: FirstVideoEntry.settled),
        Paths.app,
      );
      expect(Paths.postAuthDestination(plain), Paths.app);
      expect(
        Paths.postAuthDestination(
          Uri.parse('/login?redirect=%2Finvite%2Ftoken'),
          entry: FirstVideoEntry.fresh,
        ),
        '/invite/token',
      );
    });
  });
}
