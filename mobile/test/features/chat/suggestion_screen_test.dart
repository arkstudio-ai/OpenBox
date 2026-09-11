import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/features/chat/state/config_providers.dart';
import 'package:bossip_mobile/features/chat/state/stream_store.dart';
import 'package:bossip_mobile/features/chat/utils/reasoning.dart';
import 'package:bossip_mobile/features/chat/widgets/chat_flow.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/suggestion_chips.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:bossip_mobile/shared/models/session.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'suggestion_fixtures.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late SuggestionFixture fixture;
  setUp(() async => fixture = await SuggestionFixture.create());
  tearDown(() => fixture.dispose());

  testWidgets(
    'screen sends full prompt through REST with current picker settings',
    (tester) async {
      await tester.pumpWidget(fixture.app(const ChatScreen(sessionId: 's1')));
      await tester.pumpAndSettle();
      fixture.container.read(pickedModelProvider('s1').notifier).state =
          'test/chat';
      fixture.container
          .read(pickedVariantProvider(reasoningKey('s1', 'test/chat')).notifier)
          .state = const Variant(
        'high',
      );
      fixture.container.read(pickedVideoProvider('s1').notifier).state =
          const VideoPick('video-test', '720p');
      fixture.container.read(pickedAgentProvider('s1').notifier).state = 'plan';
      await tester.pump();
      await tester.tap(find.text(sendSuggestion.label));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      final request = fixture.api.sends.single;
      expect(request.path, '/api/agent/session/s1/prompt_async');
      expect(request.data, containsPair('text', sendSuggestion.prompt));
      expect(request.data, containsPair('model', 'test/chat'));
      expect(request.data, containsPair('variant', 'high'));
      expect(request.data, containsPair('video_model', 'video-test'));
      expect(request.data, containsPair('video_resolution', '720p'));
      expect(request.data, containsPair('agent', 'plan'));
      expect(find.byType(SuggestionChips), findsNothing);
      await tester.pumpWidget(const SizedBox());
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'pinned chips allow history and dock drags without sending; keyboard stays pinned',
    (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetViewInsets);
      fixture.api.messages = [
        answer(
          parts: [
            TextPart(
              id: 'text',
              text: List.generate(35, (i) => 'Paragraph $i.\n\n').join(),
            ),
            testSuggestions,
          ],
        ),
      ];
      await tester.pumpWidget(fixture.app(const ChatScreen(sessionId: 's1')));
      await tester.pumpAndSettle();
      expect(find.byType(SuggestionChips), findsOneWidget);
      final list = find.descendant(
        of: find.byType(ChatFlow),
        matching: find.byType(ListView),
      );
      final viewport = tester.getRect(list);
      final dock = tester.getRect(find.byType(SuggestionChips));
      final controller = tester.widget<ListView>(list).controller!;
      final bottom = controller.offset;
      for (var i = 0; i < 5; i++) {
        final previous = controller.offset;
        await tester.drag(list, const Offset(0, 35));
        await tester.pumpAndSettle();
        expect(controller.offset, lessThan(previous));
      }
      expect(controller.offset, lessThan(bottom - 60));
      expect(tester.getRect(list), viewport);
      expect(tester.getRect(find.byType(SuggestionChips)), dock);
      await tester.drag(list, const Offset(0, 500));
      await tester.pumpAndSettle();
      expect(find.byType(SuggestionChips), findsOneWidget);
      final before = controller.offset;
      await tester.drag(find.text(sendSuggestion.label), const Offset(0, -180));
      await tester.pumpAndSettle();
      expect(controller.offset, greaterThan(before));
      expect(fixture.api.sends, isEmpty);
      await tester.tap(find.byIcon(Icons.arrow_downward));
      await tester.pumpAndSettle();
      expect(find.byType(SuggestionChips), findsOneWidget);
      await tester.showKeyboard(find.byType(TextField));
      tester.view.viewInsets = const FakeViewPadding(bottom: 300);
      await tester.pumpAndSettle();
      expect(find.byType(SuggestionChips), findsOneWidget);
      expect(find.byIcon(Icons.arrow_downward), findsNothing);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('late WS suggestions appear, idle errors and waiting hide them', (
    tester,
  ) async {
    fixture.api.messages = [answer(parts: [])];
    await tester.pumpWidget(fixture.app(const ChatScreen(sessionId: 's1')));
    await tester.pumpAndSettle();
    expect(find.byType(SuggestionChips), findsNothing);
    fixture.ws.frames.add(
      const WsEvent('part.created', {
        'sessionId': 's1',
        'messageId': 'm001',
        'part': {
          'id': 'late',
          'type': 'suggestions',
          'items': [
            {'label': '测试', 'prompt': '请添加测试。', 'mode': 'send'},
          ],
        },
      }),
    );
    await tester.pumpAndSettle();
    expect(find.text('测试'), findsOneWidget);
    final store = fixture.container.read(chatStreamProvider.notifier);
    store.setStatus('s1', SessionStatus.waitingInput);
    await tester.pumpAndSettle();
    expect(find.byType(SuggestionChips), findsNothing);
    store.setStatus('s1', SessionStatus.idle);
    store.setRunError('s1', 'Failed');
    await tester.pumpAndSettle();
    expect(find.byType(SuggestionChips), findsNothing);
    expect(fixture.api.sends, isEmpty);
  });

  testWidgets('another owner has no suggestion controls', (tester) async {
    fixture.api.owner = 'other-user';
    await tester.pumpWidget(fixture.app(const ChatScreen(sessionId: 's1')));
    await tester.pumpAndSettle();
    expect(find.byType(SuggestionChips), findsNothing);
    expect(find.byType(TextField), findsNothing);
    expect(fixture.api.sends, isEmpty);
  });
}
