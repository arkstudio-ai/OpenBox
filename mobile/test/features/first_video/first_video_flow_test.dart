import 'package:bossip_mobile/features/first_video/api/first_video_api.dart';
import 'package:bossip_mobile/features/first_video/api/photo_picker.dart';
import 'package:bossip_mobile/features/first_video/making_page.dart';
import 'package:bossip_mobile/features/first_video/photo_page.dart';
import 'package:bossip_mobile/features/first_video/result_page.dart';
import 'package:bossip_mobile/features/first_video/shop_page.dart';
import 'package:bossip_mobile/features/first_video/state/first_video_store.dart';
import 'package:bossip_mobile/features/first_video/template_page.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:bossip_mobile/shared/widgets/toast.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// The backend as the six screens see it: one job per workspace, flipped
/// by the test between polls.
class _Server {
  Map<String, dynamic> job = {'status': 'none'};
  Map<String, dynamic> catalog = {
    'eta_seconds': 90,
    'industries': <dynamic>[],
    'templates': <dynamic>[],
  };
  final requests = <RequestOptions>[];
  final writes = <RequestOptions>[];

  Map<String, dynamic> handle(RequestOptions o) {
    requests.add(o);
    if (o.method != 'GET') writes.add(o);
    final path = o.uri.path;
    if (path == '/api/first-video/templates') return catalog;
    if (path == '/api/first-video') {
      if (o.method == 'POST' && job['status'] == 'none') {
        job = {'status': 'submitted', 'session_id': 's1', 'eta_seconds': 90};
      }
      return job;
    }
    if (path == '/api/assets') {
      return {
        'id': 'asset_1',
        'putUrl': 'https://oss.invalid/put/asset_1',
        'headers': {'Content-Type': 'image/jpeg'},
      };
    }
    if (path.endsWith('/complete')) return {'id': 'asset_1'};
    // No playable URL in tests: the player shows its fallback, no platform.
    if (path.startsWith('/api/assets/')) return <String, dynamic>{};
    if (path == '/api/auth/me/preferences') {
      return {'extra': <String, dynamic>{}};
    }
    return <String, dynamic>{};
  }
}

class _Auth extends AuthController {
  @override
  AuthState build() => const AuthState(
    isLoading: false,
    user: AuthUser(id: 'u1', username: 'wang', role: 'user'),
  );
}

class _Picker implements FirstVideoPhotoPicker {
  final picked = <PhotoSource>[];
  @override
  Future<PickedPhoto?> pick(PhotoSource source) async {
    picked.add(source);
    return PickedPhoto(
      name: 'shop.jpg',
      mime: 'image/jpeg',
      bytes: List<int>.filled(64, 7),
    );
  }
}

({_Server server, GoRouter router, _Picker picker, ProviderContainer container})
_mount(
  WidgetTester tester,
  I18nBundle bundle,
  SharedPreferences prefs, {
  String initial = Paths.firstVideoShop,
  Future<bool> Function()? canPublish,
}) {
  final server = _Server();
  final dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (o, h) => h.resolve(
        Response<dynamic>(
          requestOptions: o,
          statusCode: 200,
          data: server.handle(o),
        ),
      ),
    ),
  );
  final picker = _Picker();
  final router = GoRouter(
    initialLocation: initial,
    routes: [
      GoRoute(
        path: Paths.firstVideoShop,
        builder: (_, _) => const FirstVideoShopPage(),
      ),
      GoRoute(
        path: Paths.firstVideoTemplate,
        builder: (_, _) => const FirstVideoTemplatePage(),
      ),
      GoRoute(
        path: Paths.firstVideoPhoto,
        builder: (_, _) => const FirstVideoPhotoPage(),
      ),
      GoRoute(
        path: Paths.firstVideoMaking,
        builder: (_, _) => const FirstVideoMakingPage(),
      ),
      GoRoute(
        path: Paths.firstVideoResult,
        builder: (_, _) => FirstVideoResultPage(canPublish: canPublish),
      ),
      for (final path in [Paths.app, '/app/billing', '/app/s/:sessionId'])
        GoRoute(
          path: path,
          builder: (_, state) =>
              Scaffold(body: Text('at ${state.uri.toString()}')),
        ),
    ],
  );
  addTearDown(router.dispose);
  final container = ProviderContainer(
    overrides: [
      prefsProvider.overrideWithValue(prefs),
      apiDioProvider.overrideWithValue(dio),
      authProvider.overrideWith(_Auth.new),
      i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
      firstVideoApiProvider.overrideWithValue(
        FirstVideoApi(dio, uploader: dio),
      ),
      firstVideoPhotoPickerProvider.overrideWithValue(picker),
    ],
  );
  addTearDown(container.dispose);
  return (server: server, router: router, picker: picker, container: container);
}

Future<void> _pumpApp(
  WidgetTester tester,
  ProviderContainer container,
  GoRouter router,
) async {
  await tester.pumpWidget(
    UncontrolledProviderScope(
      container: container,
      child: MaterialApp.router(
        routerConfig: router,
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
      ),
    ),
  );
  await _settle(tester);
}

/// Screens here carry spinners and periodic timers, so settle by hand.
Future<void> _settle(WidgetTester tester, [int frames = 4]) async {
  for (var i = 0; i < frames; i++) {
    await tester.pump(const Duration(milliseconds: 100));
  }
}

Future<void> _tap(WidgetTester tester, Key key) async {
  await tester.ensureVisible(find.byKey(key));
  await tester.tap(find.byKey(key));
  await _settle(tester);
}

/// Where the router is: the top of the page stack. An imperative `push`
/// keeps the base `uri`, so read the pushed match itself when there is one.
String _location(GoRouter router) {
  final config = router.routerDelegate.currentConfiguration;
  final last = config.last;
  return last is ImperativeRouteMatch
      ? last.matches.uri.toString()
      : config.uri.toString();
}

void main() {
  late I18nBundle bundle;
  late SharedPreferences prefs;

  setUp(() async {
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    prefs = await SharedPreferences.getInstance();
  });

  testWidgets('six screens without typing a word: skip, skip, ready', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    final h = _mount(tester, bundle, prefs);
    await _pumpApp(tester, h.container, h.router);

    // ① 你的店 — pick an industry, leave the rest empty.
    expect(find.text('先说说你的店'), findsOneWidget);
    await _tap(tester, const Key('fv-industry-food'));
    await _tap(tester, const Key('fv-next'));

    // ② 选模板 — bundled labels while the server sends none.
    expect(_location(h.router), Paths.firstVideoTemplate);
    expect(find.text('招牌推荐'), findsOneWidget);
    expect(find.text('门店氛围'), findsOneWidget);
    expect(find.text('活动促销'), findsOneWidget);
    await _tap(tester, const Key('fv-template-ambience'));
    await _tap(tester, const Key('fv-confirm'));

    // ③ 拍一张 — skipped: text-only submission.
    expect(_location(h.router), Paths.firstVideoPhoto);
    expect(find.text('拍一张你的店'), findsOneWidget);
    await _tap(tester, const Key('fv-skip'));

    final submit = h.server.writes.singleWhere(
      (r) => r.uri.path == '/api/first-video',
    );
    expect(submit.data, {
      'industry': 'food',
      'shop_name': null,
      'headline': null,
      'template': 'ambience',
      'asset_id': null,
    });
    expect(h.picker.picked, isEmpty);

    // ④ 制作中 — the job is mirrored locally, polling has begun.
    expect(_location(h.router), Paths.firstVideoMaking);
    expect(find.text('正在做你的第一条视频'), findsOneWidget);
    expect(find.text('做好后一键发到抖音'), findsOneWidget);
    expect(h.container.read(firstVideoProvider).inFlight, isTrue);
    final pollsBefore = h.server.requests
        .where((r) => r.method == 'GET' && r.uri.path == '/api/first-video')
        .length;
    await tester.pump(FirstVideoMakingPage.pollInterval);
    await _settle(tester);
    expect(
      h.server.requests
          .where((r) => r.method == 'GET' && r.uri.path == '/api/first-video')
          .length,
      greaterThan(pollsBefore),
    );
    expect(_location(h.router), Paths.firstVideoMaking);

    // The server finishes; the next poll moves on.
    h.server.job = {
      'status': 'ready',
      'session_id': 's1',
      'job_id': 'j1',
      'asset_id': 'asset_9',
      'eta_seconds': 90,
    };
    await tester.pump(FirstVideoMakingPage.pollInterval);
    await _settle(tester);

    // ⑤ 成片
    expect(_location(h.router), Paths.firstVideoResult);
    expect(find.text('你的第一条视频'), findsOneWidget);
    expect(find.byKey(const Key('fv-free-note')), findsOneWidget);
    expect(find.text('本条免费 · 之后约 4 积分/条'), findsOneWidget);
    expect(h.container.read(firstVideoProvider).prefs?.status, 'ready');
    expect(h.container.read(firstVideoProvider).prefs?.assetId, 'asset_9');
    await _tap(tester, const Key('fv-home'));
    expect(_location(h.router), Paths.app);
  });

  testWidgets('with a photo: album → upload → submit carries the asset', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    final h = _mount(tester, bundle, prefs, initial: Paths.firstVideoPhoto);
    await _pumpApp(tester, h.container, h.router);
    expect(find.byKey(const Key('fv-photo-preview')), findsNothing);
    await _tap(tester, const Key('fv-album'));
    expect(h.picker.picked, [PhotoSource.album]);
    expect(find.byKey(const Key('fv-photo-preview')), findsOneWidget);
    await _tap(tester, const Key('fv-use-photo'));
    await _settle(tester, 8);
    expect(h.server.writes.map((r) => '${r.method} ${r.uri.path}').toList(), [
      'POST /api/assets',
      'PUT /put/asset_1',
      'POST /api/assets/asset_1/complete',
      'POST /api/first-video',
    ]);
    expect(h.server.writes.last.data, containsPair('asset_id', 'asset_1'));
    expect(h.server.writes.last.data, containsPair('template', 'signature'));
    expect(_location(h.router), Paths.firstVideoMaking);
  });

  testWidgets('server labels win over the bundled template copy', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    final h = _mount(tester, bundle, prefs, initial: Paths.firstVideoTemplate);
    h.server.catalog = {
      'eta_seconds': 60,
      'industries': [
        {'id': 'food', 'label': '餐饮'},
      ],
      'templates': [
        {'id': 'signature', 'label': '招牌特写', 'description': '来自服务端'},
        {'id': 'ambience', 'label': '店里坐坐', 'description': '来自服务端'},
        {'id': 'promo', 'label': '今日优惠', 'description': '来自服务端'},
      ],
    };
    await _pumpApp(tester, h.container, h.router);
    expect(find.text('招牌特写'), findsOneWidget);
    expect(find.text('招牌推荐'), findsNothing);
  });

  testWidgets('closing the first screen records skipped and goes home', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    final h = _mount(tester, bundle, prefs);
    await _pumpApp(tester, h.container, h.router);
    await _tap(tester, const Key('fv-close'));
    expect(find.text('先不做了？'), findsOneWidget);
    await _tap(tester, const Key('fv-leave-stay'));
    expect(h.server.writes, isEmpty);
    expect(_location(h.router), Paths.firstVideoShop);
    await _tap(tester, const Key('fv-close'));
    await _tap(tester, const Key('fv-leave-confirm'));
    final put = h.server.writes.singleWhere((r) => r.method == 'PUT');
    expect(put.uri.path, '/api/auth/me/preferences');
    final record = ((put.data as Map)['extra'] as Map)['first_video'] as Map;
    expect(record['status'], 'skipped');
    expect(h.container.read(firstVideoProvider).skipped, isTrue);
    expect(_location(h.router), Paths.app);
  });

  testWidgets('the making page can be left; a stale link restarts', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    final h = _mount(tester, bundle, prefs, initial: Paths.firstVideoMaking);
    h.server.job = {'status': 'running', 'session_id': 's1', 'eta_seconds': 30};
    await _pumpApp(tester, h.container, h.router);
    expect(find.text('正在做你的第一条视频'), findsOneWidget);
    await _tap(tester, const Key('fv-leave'));
    expect(_location(h.router), Paths.app);

    // Nothing in flight any more: the making page sends people to the start.
    final h2 = _mount(tester, bundle, prefs, initial: Paths.firstVideoMaking);
    await _pumpApp(tester, h2.container, h2.router);
    expect(_location(h2.router), Paths.firstVideoShop);
  });

  testWidgets('a failed job shows the friendly page with both exits', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    final h = _mount(tester, bundle, prefs, initial: Paths.firstVideoResult);
    h.server.job = {
      'status': 'failed',
      'session_id': 's1',
      'error': {'code': 'UPSTREAM', 'message': 'boom'},
    };
    await _pumpApp(tester, h.container, h.router);
    expect(find.text('这条没做成'), findsOneWidget);
    expect(find.text('boom'), findsNothing);
    await _tap(tester, const Key('fv-failed-chat'));
    expect(_location(h.router), '/app/s/s1');
  });

  testWidgets('publishing needs a plan: free goes to billing, paid to chat', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    final ready = {
      'status': 'ready',
      'session_id': 's1',
      'asset_id': 'asset_9',
    };
    final free = _mount(
      tester,
      bundle,
      prefs,
      initial: Paths.firstVideoResult,
      canPublish: () async => false,
    );
    free.server.job = ready;
    await _pumpApp(tester, free.container, free.router);
    await _tap(tester, const Key('fv-publish'));
    expect(_location(free.router), '/app/billing');
    expect(
      free.container.read(toastProvider).map((t) => t.text),
      contains('发布需要云电脑'),
    );

    final paid = _mount(
      tester,
      bundle,
      prefs,
      initial: Paths.firstVideoResult,
      canPublish: () async => true,
    );
    paid.server.job = ready;
    await _pumpApp(tester, paid.container, paid.router);
    await _tap(tester, const Key('fv-again'));
    expect(_location(paid.router), '/app/s/s1');
    paid.router.go(Paths.firstVideoResult);
    await _settle(tester);
    await _tap(tester, const Key('fv-publish'));
    expect(_location(paid.router), '/app/s/s1');
    // Let the hint toast expire so no timer outlives the test.
    await tester.pump(const Duration(seconds: 4));
  });

  testWidgets('a result opened while the job still runs goes to making', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    final h = _mount(tester, bundle, prefs, initial: Paths.firstVideoResult);
    h.server.job = {'status': 'submitted', 'session_id': 's1'};
    await _pumpApp(tester, h.container, h.router);
    expect(_location(h.router), Paths.firstVideoMaking);
  });

  test('progress never completes on its own and creeps without an eta', () {
    expect(firstVideoProgressFraction(0, 90), 0);
    expect(firstVideoProgressFraction(45, 90), 0.5);
    expect(firstVideoProgressFraction(900, 90), 0.96);
    expect(firstVideoProgressFraction(0, 0), closeTo(0.08, 1e-9));
    expect(firstVideoProgressFraction(10000, 0), 0.9);
  });
}
