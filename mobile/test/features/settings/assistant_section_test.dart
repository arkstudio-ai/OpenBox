import 'dart:async';

import 'package:bossip_mobile/features/settings/widgets/assistant_section.dart';
import 'package:bossip_mobile/shared/api/assistant_name.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import '../voice/voice_fakes.dart';

/// The name API, scripted: the server trims what it keeps.
class _FakeApi extends AssistantNameApi {
  _FakeApi() : super(Dio(BaseOptions(baseUrl: 'https://qa.example')));

  String name = '';
  final saved = <String>[];

  @override
  Future<String> get() async => name;

  @override
  Future<String> set(String value) async {
    saved.add(value);
    name = value.trim();
    return name;
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
  String name = '',
  _Ws? ws,
}) async {
  final api = _FakeApi()..name = name;
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        await zhI18n(tester),
        assistantNameApiProvider.overrideWithValue(api),
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

void main() {
  testWidgets(
    'a typed name is saved as typed and the default can be restored',
    (tester) async {
      final api = await _section(tester);
      expect(find.text('名字'), findsOneWidget);
      expect(find.byKey(const ValueKey('assistant-reset')), findsNothing);
      await tester.enterText(
        find.byKey(const ValueKey('assistant-name')),
        ' Mary ',
      );
      await tester.pump();
      await tester.tap(find.byKey(const ValueKey('assistant-save')));
      await tester.pumpAndSettle();
      expect(api.saved, [' Mary ']);
      expect(
        find.text('Mary'),
        findsOneWidget,
      ); // the field shows what the server kept
      expect(find.byKey(const ValueKey('assistant-reset')), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('assistant-reset')));
      await tester.pumpAndSettle();
      expect(api.saved, [' Mary ', '']);
      expect(find.byKey(const ValueKey('assistant-reset')), findsNothing);
    },
  );

  testWidgets(
    'a rename made elsewhere shows at once (the assistant in chat, another device)',
    (tester) async {
      final ws = _Ws();
      await _section(tester, name: 'Mary', ws: ws);
      expect(find.text('Mary'), findsOneWidget);
      ws.frames.add(
        const WsEvent('assistant.renamed', {'userId': 'u1', 'name': '小七'}),
      );
      await tester.pumpAndSettle();
      expect(find.text('小七'), findsOneWidget);
      expect(find.text('Mary'), findsNothing);
    },
  );

  testWidgets(
    'the stored name fills the field and saving it again is not offered',
    (tester) async {
      await _section(tester, name: '小七');
      expect(find.text('小七'), findsOneWidget);
      final save = tester.widget<FilledButton>(
        find.byKey(const ValueKey('assistant-save')),
      );
      expect(save.onPressed, isNull);
    },
  );
}
