import 'dart:convert';
import 'dart:typed_data';

import 'package:bossip_mobile/features/onboarding/state/onboarding_store.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
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
  Map<String, dynamic> serverOnboarding = {};
  bool serverDown = false;

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    prefs = await SharedPreferences.getInstance();
    requests = [];
    serverOnboarding = {};
    serverDown = false;
    dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
    dio.httpClientAdapter = _Adapter((o) async {
      requests.add(o);
      if (serverDown) throw DioException(requestOptions: o);
      if (o.method == 'PUT') {
        serverOnboarding = Map<String, dynamic>.from(
          (o.data as Map)['onboarding'] as Map,
        );
      }
      return {'onboarding': serverOnboarding};
    });
  });

  ProviderContainer container(String user) => ProviderContainer(
    overrides: [
      prefsProvider.overrideWithValue(prefs),
      apiDioProvider.overrideWithValue(dio),
      authProvider.overrideWith(() => _Auth(user)),
    ],
  );

  test('loads the server map, then guides only show for unseen keys', () async {
    serverOnboarding = {'welcome': true, 'industry': 'food'};
    final c = container('u1');
    addTearDown(c.dispose);
    final notifier = c.read(onboardingProvider.notifier);
    expect(notifier.shouldShow(Guides.drawer), isFalse, reason: 'not loaded');
    await notifier.whenLoaded();
    expect(c.read(onboardingProvider).loaded, isTrue);
    expect(notifier.shouldShow(Guides.welcome), isFalse);
    expect(notifier.shouldShow(Guides.drawer), isTrue);
    expect(c.read(onboardingProvider).industry, 'food');
  });

  test('markSeen sends the full map and caches it locally', () async {
    final c = container('u1');
    addTearDown(c.dispose);
    final notifier = c.read(onboardingProvider.notifier);
    await notifier.whenLoaded();
    await notifier.markSeen(Guides.welcome);
    await notifier.setIndustry('retail');
    await notifier.markSeen(Guides.welcome); // no-op, no extra request
    final puts = requests.where((r) => r.method == 'PUT').toList();
    expect(puts, hasLength(2));
    expect((puts.last.data as Map)['onboarding'], {
      'welcome': true,
      'industry': 'retail',
    });
    expect(jsonDecode(prefs.getString('bossip:onboarding:u1')!), {
      'welcome': true,
      'industry': 'retail',
    });
    expect(notifier.shouldShow(Guides.welcome), isFalse);
  });

  test('reset clears everything and replays', () async {
    serverOnboarding = {'welcome': true, 'drawer': true};
    final c = container('u1');
    addTearDown(c.dispose);
    final notifier = c.read(onboardingProvider.notifier);
    await notifier.whenLoaded();
    await notifier.reset();
    expect(notifier.shouldShow(Guides.welcome), isTrue);
    expect((requests.last.data as Map)['onboarding'], isEmpty);
  });

  test('a cached account survives an offline start; a fresh one waits', () async {
    await prefs.setString('bossip:onboarding:u1', jsonEncode({'welcome': true}));
    serverDown = true;
    final cached = container('u1');
    addTearDown(cached.dispose);
    await cached.read(onboardingProvider.notifier).whenLoaded();
    expect(cached.read(onboardingProvider).loaded, isTrue);
    expect(cached.read(onboardingProvider.notifier).shouldShow(Guides.welcome), isFalse);

    final fresh = container('u9');
    addTearDown(fresh.dispose);
    await fresh.read(onboardingProvider.notifier).whenLoaded();
    expect(fresh.read(onboardingProvider).loaded, isFalse);
    expect(fresh.read(onboardingProvider.notifier).shouldShow(Guides.welcome), isFalse);
  });

  test('switching accounts drops the previous progress', () async {
    serverOnboarding = {'welcome': true};
    final c = container('u1');
    addTearDown(c.dispose);
    await c.read(onboardingProvider.notifier).whenLoaded();
    expect(c.read(onboardingProvider.notifier).shouldShow(Guides.welcome), isFalse);
    serverOnboarding = {};
    (c.read(authProvider.notifier) as _Auth).switchTo('u2');
    await c.read(onboardingProvider.notifier).whenLoaded();
    expect(c.read(onboardingProvider).userId, 'u2');
    expect(c.read(onboardingProvider.notifier).shouldShow(Guides.welcome), isTrue);
  });

  test('guide queue admits one guide at a time and wakes waiters', () async {
    final c = ProviderContainer();
    addTearDown(c.dispose);
    final queue = c.read(guideQueueProvider.notifier);
    expect(queue.claim('welcome'), isTrue);
    expect(queue.claim('drawer'), isFalse);
    var idle = false;
    final waiting = queue.whenIdle().then((_) => idle = true);
    await Future<void>.delayed(Duration.zero);
    expect(idle, isFalse);
    queue.release('drawer'); // not the holder: ignored
    expect(queue.busy, isTrue);
    queue.release('welcome');
    await waiting;
    expect(idle, isTrue);
    expect(queue.claim('drawer'), isTrue);
  });
}
