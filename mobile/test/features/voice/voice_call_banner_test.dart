import 'package:bossip_mobile/features/voice/state/voice_call_controller.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_state.dart';
import 'package:bossip_mobile/features/voice/widgets/voice_call_banner.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'voice_fakes.dart';

class _Bar {
  _Bar(this.stub);

  final StubVoiceController stub;
  int opened = 0;

  Future<void> pump(WidgetTester tester) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          await zhI18n(tester),
          voiceCallControllerProvider.overrideWith(() => stub),
        ],
        child: MaterialApp(
          theme: testTheme(),
          home: Scaffold(
            body: Column(children: [VoiceCallBanner(onOpen: () => opened++)]),
          ),
        ),
      ),
    );
    await tester.pump();
  }
}

String _text(WidgetTester tester) =>
    tester.widget<Text>(find.byKey(const Key('voice-banner-text'))).data!;

void main() {
  testWidgets('shows the call, its time and its state, and ticks', (
    tester,
  ) async {
    final clock = FakeClock();
    final at = clock.now;
    clock.advance(const Duration(seconds: 5));
    final bar = _Bar(StubVoiceController(connectedCall(at: at), clock));
    await bar.pump(tester);
    expect(_text(tester), '通话中 · 00:05 · 我在听');
    expect(tester.getSize(find.byKey(const Key('voice-banner'))).height, 44);

    clock.advance(const Duration(seconds: 2));
    await tester.pump(const Duration(seconds: 1));
    expect(_text(tester), '通话中 · 00:07 · 我在听');

    bar.stub.set(
      connectedCall(at: at).copyWith(
        status: VoiceCallStatus.paused,
        pauseCause: VoicePauseCause.interruption,
      ),
    );
    await tester.pump();
    expect(_text(tester), '通话中 · 00:07 · 通话已暂停');
  });

  testWidgets('tap returns to the call; the red button hangs up', (
    tester,
  ) async {
    final clock = FakeClock();
    final bar = _Bar(StubVoiceController(connectedCall(at: clock.now), clock));
    final semantics = tester.ensureSemantics();
    await bar.pump(tester);
    // The open area reads its own status too.
    expect(find.bySemanticsLabel(RegExp('^点按返回通话')), findsOneWidget);
    expect(find.bySemanticsLabel('挂断'), findsOneWidget);
    semantics.dispose();

    await tester.tap(find.byKey(const Key('voice-banner-open')));
    expect(bar.opened, 1);
    expect(bar.stub.hangUps, 0);

    await tester.tap(find.byKey(const Key('voice-banner-hang-up')));
    expect(bar.stub.hangUps, 1);
    expect(bar.opened, 1);
  });

  testWidgets('while dialling it says so and can still cancel', (tester) async {
    final clock = FakeClock();
    final bar = _Bar(
      StubVoiceController(
        const VoiceCallState(status: VoiceCallStatus.connecting),
        clock,
      ),
    );
    await bar.pump(tester);
    expect(_text(tester), '通话中 · 正在接通…');
    await tester.tap(find.byKey(const Key('voice-banner-hang-up')));
    expect(bar.stub.hangUps, 1);
  });

  testWidgets('once ended it turns grey with no hang-up', (tester) async {
    final clock = FakeClock();
    final bar = _Bar(
      StubVoiceController(
        const VoiceCallState(
          status: VoiceCallStatus.ended,
          end: VoiceCallEnd(reason: VoiceEndReason.hangup),
        ),
        clock,
      ),
    );
    await bar.pump(tester);
    expect(_text(tester), '通话已结束');
    expect(find.byKey(const Key('voice-banner-hang-up')), findsNothing);
  });
}
