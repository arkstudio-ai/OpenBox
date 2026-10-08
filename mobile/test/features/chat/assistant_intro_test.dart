import 'package:bossip_mobile/features/chat/assistant_screen.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_intro.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_intro_entry.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/composer.dart';
import 'package:bossip_mobile/shared/api/assistant_profile.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../voice/voice_fakes.dart';
import 'assistant_fixture.dart';

class _NamedUser extends AuthController {
  @override
  AuthState build() => const AuthState(
    user: AuthUser(id: 'owner', username: 'memoryqa_2026'),
    isLoading: false,
  );
}

/// [child] under the person's profile as [api] serves it, in Chinese.
Future<void> _pump(
  WidgetTester tester,
  TestProfileApi api,
  Widget child,
) async {
  tester.view.physicalSize = const Size(390, 1400);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final ws = TestWs();
  addTearDown(ws.close);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        await zhI18n(tester),
        authProvider.overrideWith(_NamedUser.new),
        assistantProfileApiProvider.overrideWithValue(api),
        wsClientProvider.overrideWithValue(ws),
      ],
      child: MaterialApp(
        theme: testTheme(),
        home: Scaffold(body: child),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

/// A profile the person shaped: [decided] chosen somewhere, the meeting at
/// [status].
AssistantProfile _profile({
  Set<String> decided = const {},
  String status = 'new',
  bool nudged = false,
  Map<String, Object> values = const {},
}) => AssistantProfile.fromJson({
  ...values,
  'decided': {
    for (final key in decided) key: {'via': 'settings'},
  },
  'intro': {'status': status, 'nudged': nudged},
});

void main() {
  testWidgets(
    'asks only what is undecided, one question at a time, and ends with one real thing to start',
    (tester) async {
      // The length was chosen in Settings before the meeting.
      final api = TestProfileApi()..profile = _profile(decided: {'length'});
      String? picked;
      var closed = 0;
      await _pump(
        tester,
        api,
        SingleChildScrollView(
          child: AssistantIntro(
            mode: 'auto',
            onPick: (prompt) => picked = prompt,
            onClose: () => closed++,
          ),
        ),
      );
      expect(find.text('先花 30 秒，让我知道怎么和你相处。'), findsOneWidget);
      expect(find.text('1/3'), findsOneWidget);
      // The sign-in name is offered, never filled in.
      expect(find.text('叫我 memoryqa_2026'), findsOneWidget);
      final field = find.byKey(const ValueKey('intro-words-address'));
      expect(tester.widget<TextField>(field).controller!.text, '');
      await tester.enterText(field, ' 老王 ');
      await tester.pump();
      await tester.tap(find.byKey(const ValueKey('intro-confirm-address')));
      await tester.pumpAndSettle();
      expect(api.introEvents.last, {
        'event': 'answer',
        'step': 'address',
        'value': '老王',
      });
      expect(find.text('要给我起个名字吗？'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('intro-skip')));
      await tester.pumpAndSettle();
      expect(api.introEvents.last, {'event': 'skip', 'step': 'name'});
      // Decided in Settings: not asked.
      expect(find.text('回答你喜欢哪种？'), findsNothing);
      expect(find.text('你主要做什么生意？'), findsOneWidget);
      expect(find.text('3/3'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('intro-business-beauty')));
      await tester.pumpAndSettle();
      expect(api.profile.business, 'beauty');
      expect(api.profile.intro.status, 'done');
      // Done: greeted by the new address, with things to start that fit the
      // business.
      expect(find.text('好了，老王。先从一件事开始？'), findsOneWidget);
      await tester.tap(find.text('写一段周末护理套餐的推广文案'));
      await tester.pumpAndSettle();
      expect(picked, '帮我写一段周末护理套餐的推广文案，给三个版本。');
      expect(closed, 1);
    },
  );

  testWidgets('a business in their own words is taken as they said it', (
    tester,
  ) async {
    final api = TestProfileApi()
      ..profile = _profile(decided: {'address', 'name', 'length'});
    await _pump(
      tester,
      api,
      AssistantIntro(mode: 'auto', onPick: (_) {}, onClose: () {}),
    );
    expect(find.text('1/1'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('intro-business-other')));
    await tester.pump();
    await tester.enterText(
      find.byKey(const ValueKey('intro-words-business')),
      '宠物店',
    );
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('intro-confirm-business')));
    await tester.pumpAndSettle();
    expect(api.introEvents.single, {
      'event': 'answer',
      'step': 'business',
      'value': '宠物店',
    });
    // Not one of the kinds: the general ideas, and no address to greet by.
    expect(find.text('好了。先从一件事开始？'), findsOneWidget);
    expect(find.text('交给我一件事'), findsOneWidget);
  });

  testWidgets('以后再说 puts the meeting off', (tester) async {
    final api = TestProfileApi()..profile = _profile();
    var closed = 0;
    await _pump(
      tester,
      api,
      AssistantIntro(mode: 'auto', onPick: (_) {}, onClose: () => closed++),
    );
    expect(find.text('以后再说'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('intro-later')));
    await tester.pumpAndSettle();
    expect(api.introEvents, [
      {'event': 'dismiss'},
    ]);
    expect(api.profile.intro.status, 'dismissed');
    expect(closed, 1);
  });

  testWidgets('a meeting the person opened just closes', (tester) async {
    final api = TestProfileApi()..profile = _profile(status: 'dismissed');
    var closed = 0;
    await _pump(
      tester,
      api,
      AssistantIntro(
        mode: 'undecided',
        onPick: (_) {},
        onClose: () => closed++,
      ),
    );
    expect(find.text('以后再说'), findsNothing);
    expect(find.text('关闭'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('intro-later')));
    await tester.pumpAndSettle();
    expect(closed, 1);
    expect(api.introEvents, isEmpty); // closing records nothing
  });

  testWidgets(
    'going through again asks every question with today’s values shown',
    (tester) async {
      final api = TestProfileApi()
        ..profile = _profile(
          decided: {'address', 'length'},
          status: 'done',
          values: {'address': '老王', 'length': 'brief'},
        );
      await _pump(
        tester,
        api,
        AssistantIntro(mode: 'all', onPick: (_) {}, onClose: () {}),
      );
      expect(find.text('1/4'), findsOneWidget);
      expect(
        tester
            .widget<TextField>(
              find.byKey(const ValueKey('intro-words-address')),
            )
            .controller!
            .text,
        '老王',
      );
      await tester.tap(find.byKey(const ValueKey('intro-skip')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('intro-skip')));
      await tester.pumpAndSettle();
      // Today's length is the chosen one.
      bool? chosen(String key) => tester
          .widget<Semantics>(
            find
                .descendant(
                  of: find.byKey(ValueKey(key)),
                  matching: find.byType(Semantics),
                )
                .first,
          )
          .properties
          .selected;
      expect(chosen('intro-length-brief'), isTrue);
      expect(chosen('intro-length-balanced'), isFalse);
    },
  );

  testWidgets('a question decided elsewhere while it was open is not asked', (
    tester,
  ) async {
    final api = TestProfileApi()..profile = _profile();
    final ws = TestWs();
    addTearDown(ws.close);
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          await zhI18n(tester),
          authProvider.overrideWith(_NamedUser.new),
          assistantProfileApiProvider.overrideWithValue(api),
          wsClientProvider.overrideWithValue(ws),
        ],
        child: MaterialApp(
          theme: testTheme(),
          home: Scaffold(
            body: AssistantIntro(mode: 'auto', onPick: (_) {}, onClose: () {}),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('我该怎么称呼你？'), findsOneWidget);
    // Said on the phone meanwhile.
    ws.frames.add(
      const WsEvent('assistant.profile.updated', {
        'profile': {
          'address': '老王',
          'decided': {
            'address': {'via': 'chat'},
          },
          'intro': {'status': 'new'},
        },
      }),
    );
    await tester.pumpAndSettle();
    expect(find.text('我该怎么称呼你？'), findsNothing);
    expect(find.text('要给我起个名字吗？'), findsOneWidget);
  });

  testWidgets(
    'after going straight to work: one reminder, recorded when it shows; 不用了 puts it off',
    (tester) async {
      final api = TestProfileApi()..profile = _profile(status: 'bypassed');
      await _pump(
        tester,
        api,
        Column(
          children: [
            const Spacer(),
            AssistantIntroEntry(quiet: true, onPick: (_) {}),
          ],
        ),
      );
      expect(find.text('想让我知道怎么称呼你、喜欢怎么回答吗？'), findsOneWidget);
      expect(api.introEvents, [
        {'event': 'nudged'},
      ]);
      // Still on screen though it is recorded, until answered.
      expect(api.profile.intro.nudged, isTrue);
      await tester.tap(find.byKey(const ValueKey('intro-nudge-no')));
      await tester.pumpAndSettle();
      expect(api.introEvents.last, {'event': 'dismiss'});
      expect(find.text('想让我知道怎么称呼你、喜欢怎么回答吗？'), findsNothing);
      // While something is undecided, the quiet way back stays.
      expect(find.text('让我更懂你'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('assistant-intro-entry')));
      await tester.pumpAndSettle();
      expect(find.text('我该怎么称呼你？'), findsOneWidget);
      expect(find.text('以后再说'), findsNothing); // opened by the person
    },
  );

  testWidgets('nothing above the composer once everything is decided', (
    tester,
  ) async {
    final api = TestProfileApi()
      ..profile = _profile(
        decided: {'address', 'name', 'length', 'business'},
        status: 'bypassed',
      );
    await _pump(
      tester,
      api,
      Column(
        children: [
          const Spacer(),
          AssistantIntroEntry(quiet: true, onPick: (_) {}),
        ],
      ),
    );
    expect(find.text('让我更懂你'), findsNothing);
    expect(find.text('想让我知道怎么称呼你、喜欢怎么回答吗？'), findsNothing);
    expect(api.introEvents, isEmpty);
  });

  testWidgets('Settings’ 重新认识一下 opens every question in a sheet', (
    tester,
  ) async {
    final api = TestProfileApi()
      ..profile = _profile(
        decided: {'address', 'name', 'length', 'business'},
        status: 'done',
      );
    var done = 0;
    await _pump(
      tester,
      api,
      Column(
        children: [
          const Spacer(),
          AssistantIntroEntry(
            quiet: false,
            requested: 'all',
            onPick: (_) {},
            onRequestDone: () => done++,
          ),
        ],
      ),
    );
    expect(find.text('1/4'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('intro-later')));
    await tester.pumpAndSettle();
    expect(find.text('1/4'), findsNothing);
    expect(done, 1);
    expect(api.introEvents, isEmpty);
  });

  testWidgets(
    'the first meeting stands where the ideas would; sending right away goes straight to work',
    (tester) async {
      tester.view.physicalSize = const Size(390, 1400);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final server = TestApi()
        ..newest = []
        ..stored.clear();
      final f = (await tester.runAsync(() async {
        SharedPreferences.setMockInitialValues({});
        final prefs = await SharedPreferences.getInstance();
        final bundle = await I18nBundle.load();
        final fixture = Fixture(
          prefs,
          server: server,
          i18n: I18nState(language: 'en-US', bundle: bundle),
        );
        fixture.profileApi.profile = _profile();
        await fixture.ready();
        return fixture;
      }))!;
      addTearDown(f.close);
      await tester.pumpWidget(
        UncontrolledProviderScope(
          container: f.container,
          child: MaterialApp(
            theme: testTheme(),
            home: const Scaffold(body: AssistantScreen(scope: scope)),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('assistant-intro')), findsOneWidget);
      expect(find.byKey(const ValueKey('intro-step-address')), findsOneWidget);
      final composer = find.descendant(
        of: find.byType(Composer),
        matching: find.byType(TextField),
      );
      await tester.enterText(composer, 'Make me a poster');
      await tester.pump();
      await tester.tap(find.byIcon(Icons.arrow_upward));
      await tester.pumpAndSettle();
      expect(server.sends, hasLength(1));
      expect(f.profileApi.introEvents, [
        {'event': 'bypass'},
      ]);
    },
  );
}
