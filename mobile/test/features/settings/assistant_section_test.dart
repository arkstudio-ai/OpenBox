import 'dart:async';

import 'package:bossip_mobile/features/settings/widgets/assistant_section.dart';
import 'package:bossip_mobile/shared/api/assistant_profile.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import '../voice/voice_fakes.dart';

/// The profile API, scripted: the server trims what it keeps.
class _FakeApi extends AssistantProfileApi {
  _FakeApi() : super(Dio(BaseOptions(baseUrl: 'https://qa.example')));

  AssistantProfile profile = const AssistantProfile();
  final saved = <Map<String, Object>>[];
  LearnedStyle style = const LearnedStyle();
  final forgotten = <String>[];
  bool refuse = false;

  @override
  Future<AssistantProfile> get() async => profile;

  @override
  Future<AssistantProfile> save(Map<String, Object> patch) async {
    if (refuse) {
      throw DioException(
        requestOptions: RequestOptions(path: '/api/assistant/profile'),
        response: Response(
          requestOptions: RequestOptions(path: '/api/assistant/profile'),
          statusCode: 422,
        ),
      );
    }
    saved.add(patch);
    profile = AssistantProfile.fromJson({...profile.toJson(), ...patch});
    return profile;
  }

  @override
  Future<LearnedStyle> learned() async => style;

  @override
  Future<void> forget(LearnedItem item, {required String requestId}) async {
    forgotten.add(item.id);
    style = const LearnedStyle();
  }
}

/// The socket, scripted: a test pushes the events the server would send.
class _Ws extends AgentWsClient {
  _Ws() : super(Dio());
  final frames = StreamController<WsEvent>.broadcast(sync: true);
  @override
  Stream<WsEvent> get events => frames.stream;
  @override
  Future<void> connect() async {}
  Future<void> close() => frames.close();
}

Future<_FakeApi> _section(
  WidgetTester tester, {
  AssistantProfile profile = const AssistantProfile(),
  LearnedStyle style = const LearnedStyle(),
  _Ws? ws,
}) async {
  tester.view.physicalSize = const Size(390, 2000);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final api = _FakeApi()
    ..profile = profile
    ..style = style;
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        await zhI18n(tester),
        assistantProfileApiProvider.overrideWithValue(api),
        wsClientProvider.overrideWithValue(ws ?? _Ws()),
      ],
      child: MaterialApp(
        theme: testTheme(),
        home: const Scaffold(body: AssistantSection()),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return api;
}

FilledButton _saveButton(WidgetTester tester) =>
    tester.widget<FilledButton>(find.byKey(const ValueKey('assistant-save')));

void main() {
  testWidgets('only what changed is saved, as the server keeps it', (
    tester,
  ) async {
    final api = await _section(tester);
    expect(find.text('名字'), findsOneWidget);
    expect(_saveButton(tester).onPressed, isNull); // nothing changed yet
    await tester.enterText(
      find.byKey(const ValueKey('assistant-name')),
      ' Mary ',
    );
    await tester.enterText(
      find.byKey(const ValueKey('assistant-address')),
      '老王',
    );
    await tester.tap(find.byKey(const ValueKey('tone-lively')));
    await tester.tap(find.byKey(const ValueKey('length-brief')));
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('assistant-save')));
    await tester.pumpAndSettle();
    expect(api.saved, [
      {'name': 'Mary', 'address': '老王', 'tone': 'lively', 'length': 'brief'},
    ]);
    expect(find.text('Mary'), findsOneWidget); // the field shows what was kept
    expect(_saveButton(tester).onPressed, isNull);
    // Clearing the name brings back the default.
    await tester.enterText(find.byKey(const ValueKey('assistant-name')), '');
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('assistant-save')));
    await tester.pumpAndSettle();
    expect(api.saved.last, {'name': ''});
  });

  testWidgets(
    'a change made elsewhere shows at once (the assistant in chat, another device)',
    (tester) async {
      final ws = _Ws();
      await _section(
        tester,
        profile: const AssistantProfile(name: 'Mary'),
        ws: ws,
      );
      expect(find.text('Mary'), findsOneWidget);
      ws.frames.add(
        const WsEvent('assistant.profile.updated', {
          'userId': 'u1',
          'profile': {'name': '小七', 'address': '老王'},
        }),
      );
      await tester.pumpAndSettle();
      expect(find.text('小七'), findsOneWidget);
      expect(find.text('老王'), findsOneWidget);
      expect(find.text('Mary'), findsNothing);
    },
  );

  testWidgets('a refused profile is explained and nothing changes', (
    tester,
  ) async {
    final api = await _section(tester);
    api.refuse = true;
    await tester.enterText(
      find.byKey(const ValueKey('assistant-persona')),
      '像个老朋友',
    );
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('assistant-save')));
    await tester.pumpAndSettle();
    expect(api.saved, isEmpty);
    expect(api.profile.persona, '');
  });

  testWidgets(
    'what it learned on its own is listed, each removable, with the reasons given',
    (tester) async {
      final api = await _section(
        tester,
        style: const LearnedStyle(
          learned: [
            LearnedItem(id: 'm1', revision: 3, summary: '用户嫌回答太长，希望先说结论'),
          ],
          reactions: [('too_long', 2)],
        ),
      );
      expect(find.text('它从你身上学到的'), findsOneWidget);
      expect(find.text('用户嫌回答太长，希望先说结论'), findsOneWidget);
      expect(find.text('太长了 2 次'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('learned-remove-m1')));
      await tester.pumpAndSettle();
      expect(api.forgotten, ['m1']);
      expect(find.text('用户嫌回答太长，希望先说结论'), findsNothing);
    },
  );
}
