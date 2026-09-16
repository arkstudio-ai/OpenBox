import 'dart:convert';
import 'dart:typed_data';

import 'package:bossip_mobile/features/onboarding/state/onboarding_store.dart';
import 'package:bossip_mobile/features/onboarding/widgets/coach_mark.dart';
import 'package:bossip_mobile/features/onboarding/widgets/first_seen_hint.dart';
import 'package:bossip_mobile/features/onboarding/widgets/starter_cards.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
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

class _Loaded extends OnboardingController {
  @override
  OnboardingState build() {
    ref.watch(authProvider);
    return const OnboardingState(userId: 'u1', values: {}, loaded: true);
  }
}

class _Auth extends AuthController {
  @override
  AuthState build() => const AuthState(
    isLoading: false,
    user: AuthUser(id: 'u1', username: 'wang', role: 'user'),
  );
}

void main() {
  late SharedPreferences prefs;
  late I18nBundle bundle;
  late Dio dio;
  late Map<String, dynamic> server;

  setUp(() async {
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    prefs = await SharedPreferences.getInstance();
    server = {};
    dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
    dio.httpClientAdapter = _Adapter((o) async {
      if (o.method == 'PUT') {
        server = Map<String, dynamic>.from((o.data as Map)['onboarding'] as Map);
      }
      return {'onboarding': server};
    });
  });

  Widget app(Widget home) => ProviderScope(
    overrides: [
      prefsProvider.overrideWithValue(prefs),
      apiDioProvider.overrideWithValue(dio),
      authProvider.overrideWith(_Auth.new),
      onboardingProvider.overrideWith(_Loaded.new),
      i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
    ],
    child: MaterialApp(
      theme: ThemeData(
        extensions: [
          BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
        ],
      ),
      home: home,
    ),
  );

  testWidgets('starter cards follow the picked industry and persist it', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    final picked = <String>[];
    await tester.pumpWidget(
      app(Scaffold(body: StarterCards(onPick: picked.add))),
    );
    await tester.pumpAndSettle();
    expect(find.textContaining('新客体验项目'), findsOneWidget);
    await tester.runAsync(() async {
      await tester.tap(find.byKey(const Key('industry-food')));
      await Future<void>.delayed(const Duration(milliseconds: 100));
    });
    await tester.pumpAndSettle();
    expect(find.textContaining('新品上市'), findsOneWidget);
    expect(server['industry'], 'food');
    await tester.tap(find.textContaining('新品上市'));
    expect(picked.single, contains('新品上市'));
  });

  testWidgets('coach marks mask the anchor, step through, and mark seen once', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    late BuildContext ctx;
    late WidgetRef widgetRef;
    await tester.pumpWidget(
      app(
        Scaffold(
          body: Consumer(
            builder: (context, ref, _) {
              ctx = context;
              widgetRef = ref;
              return Column(
                children: const [
                  CoachAnchor(name: 'a', child: SizedBox(width: 80, height: 40)),
                  CoachAnchor(name: 'b', child: SizedBox(width: 80, height: 40)),
                ],
              );
            },
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    final steps = [
      const CoachStep(anchor: 'a', title: '第一步', body: 'A'),
      const CoachStep(anchor: 'b', title: '第二步', body: 'B'),
    ];
    final shown = showCoachMarks(ctx, widgetRef, guideKey: 'drawer', steps: steps);
    await tester.pumpAndSettle();
    expect(find.text('第一步'), findsOneWidget);
    expect(find.text('1 / 2'), findsOneWidget);
    expect(widgetRef.read(guideQueueProvider), 'drawer');
    await tester.tap(find.text('下一步'));
    await tester.pumpAndSettle();
    expect(find.text('第二步'), findsOneWidget);
    await tester.tap(find.text('知道了'));
    await tester.pumpAndSettle();
    expect(await shown, isTrue);
    expect(find.text('第二步'), findsNothing);
    expect(widgetRef.read(guideQueueProvider), isNull);
    expect(widgetRef.read(onboardingProvider).seen('drawer'), isTrue);
    // Seen: never again.
    expect(
      await showCoachMarks(ctx, widgetRef, guideKey: 'drawer', steps: steps),
      isFalse,
    );
  });

  testWidgets('first-seen hint shows once and disappears on dismiss', (
    tester,
  ) async {
    bundle = (await tester.runAsync(I18nBundle.load))!;
    await tester.pumpWidget(
      app(
        Scaffold(
          body: FirstSeenHint(
            guide: Guides.inbox,
            title: '标题',
            body: '正文',
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('hint-inbox')), findsOneWidget);
    expect(find.text('标题'), findsOneWidget);
    await tester.runAsync(() async {
      await tester.tap(find.text('知道了'));
      await Future<void>.delayed(const Duration(milliseconds: 100));
    });
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('hint-inbox')), findsNothing);
    expect(server['inbox'], isTrue);
  });
}
