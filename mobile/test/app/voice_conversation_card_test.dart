import 'package:bossip_mobile/app/voice_conversation_card.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_controller.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_state.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import '../features/voice/voice_fakes.dart';

const _scope = (userId: 'owner', workspaceId: 'workspace');

Future<StubVoiceController> _pump(
  WidgetTester tester,
  VoiceCallState state,
) async {
  final stub = StubVoiceController(state, FakeClock());
  await tester.pumpWidget(
    ProviderScope(
      key: UniqueKey(),
      overrides: [
        await zhI18n(tester),
        voiceCallControllerProvider.overrideWith(() => stub),
      ],
      child: MaterialApp(
        theme: testTheme(),
        home: const Scaffold(
          body: SizedBox(
            height: 400,
            child: VoiceConversationCard(scope: _scope),
          ),
        ),
      ),
    ),
  );
  await tester.pump();
  return stub;
}

VoiceLine _line(String role, String text, String item, {bool isFinal = true}) =>
    VoiceLine(role: role, text: text, itemId: item, isFinal: isFinal, at: 1);

void main() {
  testWidgets('shows the call as text, the reply in progress in italics', (
    tester,
  ) async {
    await _pump(
      tester,
      VoiceCallState(
        status: VoiceCallStatus.connected,
        phase: VoicePhase.speaking,
        transcript: [
          _line('user', '帮我看看艾薇兰那条', 'u1'),
          _line('assistant', '好的，正在', 'a1', isFinal: false),
        ],
      ),
    );
    expect(find.byKey(const Key('voice-conversation-card')), findsOneWidget);
    expect(find.textContaining('帮我看看艾薇兰那条'), findsOneWidget);
    final partial = tester.widget<Text>(find.textContaining('好的，正在'));
    expect(partial.style?.fontStyle, FontStyle.italic);
    // Nothing is the room's yet: one channel, no room tab.
    expect(
      find.byKey(const Key('voice-conversation-channel-room')),
      findsNothing,
    );
  });

  testWidgets('offers the focused task as a second channel', (tester) async {
    await _pump(
      tester,
      const VoiceCallState(
        status: VoiceCallStatus.connected,
        phase: VoicePhase.listening,
        hearing: true,
        focus: VoiceFocusTask(taskId: 't1', title: '艾薇兰调研', sessionId: 's1'),
      ),
    );
    expect(find.text('在听你说…'), findsOneWidget);
    expect(find.textContaining('「艾薇兰调研」现场'), findsOneWidget);
    expect(
      find.byKey(const Key('voice-conversation-channel-room')),
      findsOneWidget,
    );
  });
}
