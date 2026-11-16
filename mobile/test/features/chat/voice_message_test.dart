import 'dart:async';

import 'package:bossip_mobile/features/chat/widgets/composer/voice_message_input.dart';
import 'package:bossip_mobile/features/chat/widgets/user_bubble.dart';
import 'package:bossip_mobile/features/chat/widgets/voice_message_bubble.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_controller.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_state.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../voice/voice_fakes.dart';
import 'assistant_fixture.dart';

class FakeRecorder implements MessageRecorder {
  Completer<bool>? permission;
  int starts = 0, stops = 0;
  @override
  Future<bool> permitted() async =>
      permission == null ? true : await permission!.future;
  @override
  Future<void> start(String path) async {
    starts++;
  }

  @override
  Future<void> stop() async {
    stops++;
  }

  @override
  Future<void> dispose() async {}
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late SharedPreferences prefs;
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    prefs = await SharedPreferences.getInstance();
  });

  Future<void> pumpInput(WidgetTester tester, FakeRecorder recorder) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          await zhI18n(tester),
          prefsProvider.overrideWithValue(prefs),
          messageRecorderFactoryProvider.overrideWithValue(() => recorder),
          voiceDraftDirectoryProvider.overrideWithValue(() async => '/tmp'),
          voiceCallControllerProvider.overrideWith(
            () => StubVoiceController(const VoiceCallState(), FakeClock()),
          ),
        ],
        child: MaterialApp(
          theme: testTheme(),
          home: const Scaffold(
            body: VoiceMessageInput(scope: scope, sessionId: 'main'),
          ),
        ),
      ),
    );
    await tester.pump();
  }

  testWidgets('leaving voice mode before recording disposes safely', (
    tester,
  ) async {
    final recorder = FakeRecorder();
    await pumpInput(tester, recorder);
    await tester.pumpWidget(const SizedBox());
    await tester.pump();
    expect(recorder.starts, 0);
    expect(tester.takeException(), isNull);
  });

  testWidgets('release while permission is pending never starts a microphone', (
    tester,
  ) async {
    final recorder = FakeRecorder()..permission = Completer<bool>();
    await pumpInput(tester, recorder);
    final gesture = await tester.startGesture(
      tester.getCenter(find.byKey(const Key('voice-message-hold'))),
    );
    await tester.pump(const Duration(milliseconds: 600));
    await gesture.up();
    recorder.permission!.complete(true);
    await tester.pump();
    expect(recorder.starts, 0);
    expect(
      prefs.getKeys().where((key) => key.startsWith('voice-message:')),
      isEmpty,
    );
  });

  testWidgets('slide up cancels a recording without persisting or sending', (
    tester,
  ) async {
    final recorder = FakeRecorder();
    await pumpInput(tester, recorder);
    final gesture = await tester.startGesture(
      tester.getCenter(find.byKey(const Key('voice-message-hold'))),
    );
    await tester.pump(const Duration(milliseconds: 600));
    await tester.pump();
    expect(recorder.starts, 1);
    await gesture.moveBy(const Offset(0, -100));
    await tester.pump();
    expect(find.textContaining('松开取消'), findsOneWidget);
    await gesture.up();
    await tester.pump();
    expect(recorder.stops, 1);
    expect(
      prefs.getKeys().where((key) => key.startsWith('voice-message:')),
      isEmpty,
    );
  });

  testWidgets('voice bubble survives JSON history and expands the transcript', (
    tester,
  ) async {
    final value = ChatMessage.fromJson({
      'id': 'voice-1',
      'session_id': 'main',
      'role': 'user',
      'parts': [
        {
          'id': 'text-1',
          'type': 'text',
          'text': '不要取消会议。',
          'origin': 'human',
          'origin_ref': {
            'voice_message': {'asset_id': 'audio-1', 'duration_ms': 4200},
          },
        },
      ],
    });
    await tester.pumpWidget(
      ProviderScope(
        overrides: [await zhI18n(tester)],
        child: MaterialApp(
          theme: testTheme(),
          home: Scaffold(body: UserBubble(message: value)),
        ),
      ),
    );
    expect(find.byType(VoiceMessageBubble), findsOneWidget);
    expect(find.text('5″'), findsOneWidget);
    expect(find.text('不要取消会议。'), findsNothing);
    await tester.tap(find.text('查看文字'));
    await tester.pump();
    expect(find.text('不要取消会议。'), findsOneWidget);
  });

  test(
    'voice retries reuse their identity even after an accepted response was lost by the view',
    () async {
      final api = TestApi();
      final f = Fixture(prefs, server: api);
      addTearDown(f.close);
      await f.ready();
      const voice = {'asset_id': 'audio-1', 'duration_ms': 4000};
      api.onSend = (_) async => throw ApiError(
        status: 503,
        code: 'NETWORK',
        message: 'lost response',
      );
      await expectLater(
        f.controller.send(
          '不要取消会议。',
          [],
          voiceMessage: voice,
          clientId: 'voice-fixed',
        ),
        throwsA(isA<ApiError>()),
      );
      api.onSend = null;
      await f.controller.send(
        '不要取消会议。',
        [],
        voiceMessage: voice,
        clientId: 'voice-fixed',
      );
      await f.controller.send(
        '不要取消会议。',
        [],
        voiceMessage: voice,
        clientId: 'voice-fixed',
      );
      expect(api.sends, hasLength(3));
      expect(
        api.sends.every(
          (body) =>
              body['client_id'] == 'voice-fixed' &&
              body['voice_asset_id'] == 'audio-1',
        ),
        isTrue,
      );
      expect(api.sends[0], api.sends[1]);
    },
  );
}
