import 'package:bossip_mobile/features/voice/state/voice_call_controller.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_state.dart';
import 'package:bossip_mobile/features/voice/widgets/voice_call_host.dart';
import 'package:bossip_mobile/shared/widgets/toast.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'voice_fakes.dart';

const _content = Key('content');

/// What the screens under the host see as their top inset.
double _contentTopInset = -1;

class _Host {
  _Host(VoiceCallState initial) : stub = StubVoiceController(initial, clock);

  static final clock = FakeClock();
  final StubVoiceController stub;
  int opened = 0;

  /// [statusBar] stands in for the phone's status bar height.
  Future<void> pump(WidgetTester tester, {double statusBar = 0}) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          await zhI18n(tester),
          voiceCallControllerProvider.overrideWith(() => stub),
        ],
        child: MaterialApp(
          theme: testTheme(),
          // As in the app: the host wraps the Navigator and the toasts.
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(
              context,
            ).copyWith(padding: EdgeInsets.only(top: statusBar)),
            child: VoiceCallHost(
              onOpen: () => opened++,
              child: Stack(children: [child!, const ToastHost()]),
            ),
          ),
          home: Builder(
            builder: (context) {
              _contentTopInset = MediaQuery.paddingOf(context).top;
              return const Scaffold(body: SizedBox.expand(key: _content));
            },
          ),
        ),
      ),
    );
    await tester.pump(const Duration(milliseconds: 250));
  }
}

double _contentTop(WidgetTester tester) =>
    tester.getTopLeft(find.byKey(_content)).dy;

void main() {
  testWidgets('no call: no bar, nothing moves', (tester) async {
    final host = _Host(const VoiceCallState());
    await host.pump(tester);
    expect(find.byKey(const Key('voice-banner')), findsNothing);
    expect(_contentTop(tester), 0);
  });

  testWidgets('a collapsed call pushes every screen down by 44 pt', (
    tester,
  ) async {
    final host = _Host(connectedCall(at: _Host.clock.now));
    await host.pump(tester);
    expect(find.byKey(const Key('voice-banner')), findsOneWidget);
    expect(_contentTop(tester), 44);
    expect(_contentTopInset, 0);

    // The call page on top: the bar steps aside (after a short slide).
    host.stub.set(connectedCall(at: _Host.clock.now, expanded: true));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 250));
    expect(find.byKey(const Key('voice-banner')), findsNothing);
    expect(_contentTop(tester), 0);
  });

  testWidgets('under a status bar the bar takes it, the screens lose it', (
    tester,
  ) async {
    final host = _Host(connectedCall(at: _Host.clock.now));
    await host.pump(tester, statusBar: 20);
    expect(
      tester.getSize(find.byKey(const Key('voice-banner'))).height,
      20 + 44,
    );
    expect(_contentTop(tester), 64);
    // No second status-bar inset under the bar.
    expect(_contentTopInset, 0);
  });

  testWidgets('ending away from the page: toast, grey bar, then gone', (
    tester,
  ) async {
    final host = _Host(connectedCall(at: _Host.clock.now));
    await host.pump(tester);
    host.stub.set(
      const VoiceCallState(
        status: VoiceCallStatus.ended,
        end: VoiceCallEnd(
          reason: VoiceEndReason.hangup,
          durationSeconds: 75,
          pendingTurns: 2,
          cost: VoiceCost(totalYuan: 0.0123),
        ),
      ),
    );
    await tester.pump();
    expect(find.text('通话已结束'), findsNWidgets(2)); // bar + toast title
    expect(
      find.text('时长 01:15 · 费用约 ¥0.0123\n还有 2 件事在办，结果会写在对话里。'),
      findsOneWidget,
    );
    expect(_contentTop(tester), 44);

    await tester.pump(VoiceCallHost.endedBarTime);
    await tester.pump(const Duration(milliseconds: 250));
    expect(host.stub.state.status, VoiceCallStatus.idle);
    expect(find.byKey(const Key('voice-banner')), findsNothing);
    expect(_contentTop(tester), 0);
    // Let the toast run out.
    await tester.pump(const Duration(seconds: 12));
  });

  testWidgets('a call that ends on its own page gets no toast', (tester) async {
    final host = _Host(connectedCall(at: _Host.clock.now, expanded: true));
    await host.pump(tester);
    host.stub.set(
      const VoiceCallState(
        status: VoiceCallStatus.ended,
        expanded: true,
        end: VoiceCallEnd(reason: VoiceEndReason.network),
      ),
    );
    await tester.pump();
    expect(find.byType(ToastHost), findsOneWidget);
    expect(find.text('网络断开，通话结束。'), findsNothing);
    expect(find.byKey(const Key('voice-banner')), findsNothing);
  });

  testWidgets('coming back from an interruption says so', (tester) async {
    final host = _Host(
      connectedCall(at: _Host.clock.now).copyWith(
        status: VoiceCallStatus.paused,
        pauseCause: VoicePauseCause.interruption,
      ),
    );
    await host.pump(tester);
    host.stub.set(connectedCall(at: _Host.clock.now));
    await tester.pump();
    expect(find.text('通话已恢复'), findsOneWidget);
    await tester.pump(const Duration(seconds: 12));
  });

  testWidgets('tapping the bar asks to open the call page', (tester) async {
    final host = _Host(connectedCall(at: _Host.clock.now));
    await host.pump(tester);
    await tester.tap(find.byKey(const Key('voice-banner-open')));
    expect(host.opened, 1);
  });
}
