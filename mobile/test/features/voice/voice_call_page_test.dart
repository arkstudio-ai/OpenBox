import 'package:bossip_mobile/features/voice/state/voice_call_controller.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_state.dart';
import 'package:bossip_mobile/features/voice/voice_call_page.dart';
import 'package:bossip_mobile/features/voice/widgets/voice_call_host.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:bossip_mobile/shared/widgets/toast.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

import 'voice_fakes.dart';

/// The page alone over a stub controller frozen in one state.
Future<StubVoiceController> _page(
  WidgetTester tester,
  VoiceCallState state, {
  FakeClock? clock,
}) async {
  final stub = StubVoiceController(state, clock ?? FakeClock());
  await tester.pumpWidget(
    ProviderScope(
      // A fresh scope each time: overrides are fixed once built.
      key: UniqueKey(),
      overrides: [
        await zhI18n(tester),
        voiceCallControllerProvider.overrideWith(() => stub),
      ],
      child: MaterialApp(theme: testTheme(), home: const VoiceCallPage()),
    ),
  );
  await tester.pump();
  return stub;
}

String _status(WidgetTester tester) =>
    tester.widget<Text>(find.byKey(const Key('voice-status'))).data!;

Finder _button(String key) => find.byKey(Key(key));

void main() {
  group('states and controls', () {
    testWidgets('opening on no call dials', (tester) async {
      final stub = await _page(tester, const VoiceCallState());
      expect(stub.starts, 1);
      expect(stub.state.expanded, isTrue);
    });

    testWidgets('dialling: only cancel', (tester) async {
      for (final (status, line) in [
        (VoiceCallStatus.requestingMic, '请允许使用麦克风'),
        (VoiceCallStatus.connecting, '正在接通…'),
      ]) {
        final stub = await _page(tester, VoiceCallState(status: status));
        expect(stub.starts, 0);
        expect(_status(tester), line);
        expect(find.text('取消'), findsOneWidget);
        expect(_button('voice-mute'), findsNothing);
        expect(_button('voice-speaker'), findsNothing);
        expect(_button('voice-timer'), findsNothing);
      }
    });

    testWidgets('connected: timer, hint for ten seconds, three buttons', (
      tester,
    ) async {
      final clock = FakeClock();
      final at = clock.now;
      clock.advance(const Duration(seconds: 3));
      final stub = await _page(
        tester,
        connectedCall(at: at, cost: const VoiceCost(totalYuan: 0.0035)),
        clock: clock,
      );
      expect(_status(tester), '我在听');
      expect(tester.widget<Text>(_button('voice-timer')).data, '00:03');
      expect(find.text('直接说话就好，随时可以打断。'), findsOneWidget);
      expect(find.text('本次消耗 0.0035 积分'), findsOneWidget);
      for (final label in ['静音', '挂断', '扬声器']) {
        expect(find.text(label), findsOneWidget);
      }
      await tester.tap(_button('voice-mute'));
      await tester.tap(_button('voice-speaker'));
      expect((stub.mutes, stub.speakers), (1, 1));

      clock.advance(const Duration(seconds: 7));
      await tester.pump(const Duration(seconds: 1));
      expect(find.text('直接说话就好，随时可以打断。'), findsNothing);
      expect(tester.widget<Text>(_button('voice-timer')).data, '00:10');
    });

    testWidgets('the request in progress shows beside the phase', (
      tester,
    ) async {
      final clock = FakeClock();
      final stub = await _page(
        tester,
        connectedCall(
          at: clock.now,
          phase: VoicePhase.speaking,
        ).copyWith(working: true),
        clock: clock,
      );
      expect(_status(tester), '在说话');
      expect(find.text('在办，稍等…'), findsOneWidget);
      stub.set(
        connectedCall(
          at: clock.now,
          phase: VoicePhase.working,
        ).copyWith(working: true, late: true),
      );
      await tester.pump();
      expect(_status(tester), '还在办…');
      expect(_button('voice-detail'), findsNothing);
    });

    testWidgets('the last five minutes are counted down', (tester) async {
      final clock = FakeClock();
      final at = clock.now;
      clock.advance(const Duration(minutes: 26));
      await _page(tester, connectedCall(at: at), clock: clock);
      expect(
        tester.widget<Text>(_button('voice-timer')).data,
        '26:00 · 剩余 4 分钟',
      );
    });

    testWidgets('paused: why, and only hang-up', (tester) async {
      final clock = FakeClock();
      await _page(
        tester,
        connectedCall(at: clock.now).copyWith(
          status: VoiceCallStatus.paused,
          pauseCause: VoicePauseCause.interruption,
        ),
        clock: clock,
      );
      expect(_status(tester), '通话已暂停');
      expect(find.text('有来电，通话已暂停'), findsOneWidget);
      expect(find.text('挂断'), findsOneWidget);
      expect(_button('voice-mute'), findsNothing);
    });

    testWidgets('hanging up: nothing to press', (tester) async {
      final clock = FakeClock();
      final stub = await _page(
        tester,
        connectedCall(at: clock.now).copyWith(status: VoiceCallStatus.ending),
        clock: clock,
      );
      expect(_status(tester), '正在挂断…');
      await tester.tap(_button('voice-hang-up'), warnIfMissed: false);
      expect(stub.hangUps, 0);
    });

    testWidgets('ended: why, numbers, pending work, what next', (tester) async {
      await _page(
        tester,
        const VoiceCallState(
          status: VoiceCallStatus.ended,
          end: VoiceCallEnd(
            reason: VoiceEndReason.network,
            durationSeconds: 134,
            pendingTurns: 1,
            cost: VoiceCost(totalYuan: 0.0123),
          ),
        ),
      );
      expect(_status(tester), '网络断开，通话结束。');
      expect(find.text('时长 02:14 · 消耗约 0.0123 积分'), findsOneWidget);
      expect(find.text('还有 1 件事在办，结果会写在对话里。'), findsOneWidget);
      expect(find.text('重新拨打'), findsOneWidget);
      expect(find.text('关闭'), findsOneWidget);
      expect(_button('voice-hang-up'), findsNothing);
    });

    testWidgets('quota and a call elsewhere cannot redial', (tester) async {
      await _page(
        tester,
        const VoiceCallState(
          status: VoiceCallStatus.ended,
          end: VoiceCallEnd(reason: VoiceEndReason.concurrent),
        ),
      );
      expect(_status(tester), '你在另一台设备上正在通话。');
      expect(_button('voice-redial'), findsNothing);
      expect(find.text('关闭'), findsOneWidget);
    });

    testWidgets('out of credits: no redial, a way to top up', (tester) async {
      await _page(
        tester,
        const VoiceCallState(
          status: VoiceCallStatus.ended,
          end: VoiceCallEnd(reason: VoiceEndReason.quota),
        ),
      );
      expect(_status(tester), '积分不足，充值后再打吧。');
      expect(_button('voice-redial'), findsNothing);
      expect(_button('voice-top-up'), findsOneWidget);
      expect(find.text('去充值'), findsOneWidget);
    });

    testWidgets('microphone refused: settings and retry', (tester) async {
      final rig = VoiceRig();
      final stub = StubVoiceController(
        const VoiceCallState(
          status: VoiceCallStatus.ended,
          end: VoiceCallEnd(reason: VoiceEndReason.micDenied),
        ),
        rig.clock,
      );
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            await zhI18n(tester),
            ...rig.overrides,
            voiceCallControllerProvider.overrideWith(() => stub),
          ],
          child: MaterialApp(theme: testTheme(), home: const VoiceCallPage()),
        ),
      );
      await tester.pump();
      expect(_status(tester), '麦克风权限未开启，请在设置里允许后重试。');
      await tester.tap(find.text('去设置'));
      expect(rig.permission.settings, 1);
      expect(find.text('重试'), findsOneWidget);
    });

    testWidgets('server words explain an error', (tester) async {
      await _page(
        tester,
        const VoiceCallState(
          status: VoiceCallStatus.ended,
          end: VoiceCallEnd(
            reason: VoiceEndReason.error,
            message: '语音服务暂时不可用。',
          ),
        ),
      );
      expect(_status(tester), '通话中断了');
      expect(find.text('语音服务暂时不可用。'), findsOneWidget);
    });

    testWidgets('the cost line opens the breakdown', (tester) async {
      final clock = FakeClock();
      await _page(
        tester,
        connectedCall(
          at: clock.now,
          cost: const VoiceCost(
            totalYuan: 0.01,
            costsYuan: {'input_audio': 0.006, 'output_audio': 0.004},
            settledRounds: 3,
            unreportedRounds: 1,
          ),
        ),
        clock: clock,
      );
      await tester.tap(_button('voice-cost'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      for (final label in ['文字输入', '语音输入', '文字输出', '语音输出']) {
        expect(find.text(label), findsOneWidget);
      }
      expect(find.text('0.0060 积分'), findsOneWidget);
      expect(find.text('已核算 3 轮 · 部分用量未返回，金额可能偏低'), findsOneWidget);
    });
  });

  group('with the real controller', () {
    /// The app's shape: a router with the call page, the host above it.
    Future<GoRouter> app(WidgetTester tester, VoiceRig rig) async {
      final router = GoRouter(
        routes: [
          GoRoute(
            path: '/',
            builder: (context, state) => Scaffold(
              body: Center(
                child: TextButton(
                  onPressed: () => context.push(Paths.voice),
                  child: const Text('call'),
                ),
              ),
            ),
          ),
          GoRoute(
            path: Paths.voice,
            pageBuilder: (context, state) => MaterialPage<void>(
              key: state.pageKey,
              fullscreenDialog: true,
              child: const VoiceCallPage(),
            ),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(
        ProviderScope(
          overrides: [await zhI18n(tester), ...rig.overrides],
          child: MaterialApp.router(
            theme: testTheme(),
            routerConfig: router,
            builder: (context, child) => VoiceCallHost(
              onOpen: () => router.push(Paths.voice),
              child: Stack(children: [child!, const ToastHost()]),
            ),
          ),
        ),
      );
      return router;
    }

    /// A page transition, plus the frame after it in which the call bar
    /// takes over from a page that has just gone.
    Future<void> transition(WidgetTester tester) async {
      for (var i = 0; i < 12; i++) {
        await tester.pump(const Duration(milliseconds: 100));
      }
    }

    Future<void> openAndConnect(WidgetTester tester, VoiceRig rig) async {
      await tester.tap(find.text('call'));
      await transition(tester);
      expect(find.byType(VoiceCallPage), findsOneWidget);
      expect(rig.connector.channels, hasLength(1));
      rig.connector.last.sendReady();
      await tester.pump();
      expect(_status(tester), '已接通');
      expect(rig.screenOn, [true]);
    }

    testWidgets('collapsing keeps the call and shows the bar', (tester) async {
      final rig = VoiceRig();
      final router = await app(tester, rig);
      await openAndConnect(tester, rig);
      expect(find.byKey(const Key('voice-banner')), findsNothing);

      await tester.tap(_button('voice-collapse'));
      await transition(tester);
      expect(find.byType(VoiceCallPage), findsNothing);
      expect(router.routerDelegate.currentConfiguration.uri.path, '/');
      expect(find.byKey(const Key('voice-banner')), findsOneWidget);
      expect(rig.connector.last.jsonSent, isEmpty);
      expect(rig.screenOn, [true, false]);
      expect(rig.audio.closed, isFalse);

      // The bar brings the page back to the same call.
      await tester.tap(_button('voice-banner-open'));
      await transition(tester);
      expect(find.byType(VoiceCallPage), findsOneWidget);
      expect(rig.connector.channels, hasLength(1));
      expect(find.byKey(const Key('voice-banner')), findsNothing);
    });

    testWidgets('leaving twice pops only the call page', (tester) async {
      final rig = VoiceRig();
      final router = await app(tester, rig);
      await openAndConnect(tester, rig);
      await tester.tap(_button('voice-collapse'));
      await tester.pump();
      await tester.tap(_button('voice-collapse'), warnIfMissed: false);
      await transition(tester);
      expect(find.byType(VoiceCallPage), findsNothing);
      expect(find.text('call'), findsOneWidget);
      expect(router.routerDelegate.currentConfiguration.uri.path, '/');
    });

    testWidgets('a firm swipe down collapses too', (tester) async {
      final rig = VoiceRig();
      await app(tester, rig);
      await openAndConnect(tester, rig);
      await tester.fling(
        find.byKey(const Key('voice-status')),
        const Offset(0, 400),
        1500,
      );
      await transition(tester);
      expect(find.byType(VoiceCallPage), findsNothing);
      expect(find.byKey(const Key('voice-banner')), findsOneWidget);
    });

    testWidgets('hanging up leaves the page and ends the call', (tester) async {
      final rig = VoiceRig();
      await app(tester, rig);
      await openAndConnect(tester, rig);
      await tester.tap(_button('voice-hang-up'));
      await transition(tester);
      expect(find.byType(VoiceCallPage), findsNothing);
      expect(rig.connector.last.jsonSent, [
        {'type': 'stop'},
      ]);
      expect(find.text('通话中 · 00:00 · 正在挂断…'), findsOneWidget);

      rig.connector.last.event({
        'type': 'ended',
        'reason': 'hangup',
        'duration_seconds': 7,
      });
      await tester.pump();
      // Grey bar plus the toast's title.
      expect(find.text('通话已结束'), findsNWidgets(2));
      expect(find.text('时长 00:07'), findsOneWidget);

      await tester.pump(VoiceCallHost.endedBarTime);
      await transition(tester);
      expect(find.byKey(const Key('voice-banner')), findsNothing);
      await tester.pump(const Duration(seconds: 12));
    });

    testWidgets('an ended that arrives mid-slide still gets its summary', (
      tester,
    ) async {
      final rig = VoiceRig();
      await app(tester, rig);
      await openAndConnect(tester, rig);
      await tester.tap(_button('voice-hang-up'));
      // The server answers before the page has finished sliding away.
      rig.connector.last.event({
        'type': 'ended',
        'reason': 'hangup',
        'duration_seconds': 3,
        'pending_turns': 1,
      });
      await tester.pump();
      await transition(tester);
      expect(find.byType(VoiceCallPage), findsNothing);
      expect(find.text('通话已结束'), findsNWidgets(2));
      expect(find.text('时长 00:03\n还有 1 件事在办，结果会写在对话里。'), findsOneWidget);
      await tester.pump(const Duration(seconds: 12));
    });

    testWidgets('reopening during the slide keeps the bar away', (
      tester,
    ) async {
      final rig = VoiceRig();
      await app(tester, rig);
      await openAndConnect(tester, rig);
      await tester.tap(_button('voice-collapse'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 250));
      // The bar is up at once; tap it while the old page still slides.
      expect(find.byType(VoiceCallPage), findsOneWidget);
      await tester.tap(_button('voice-banner-open'));
      await transition(tester);
      expect(find.byType(VoiceCallPage), findsOneWidget);
      expect(find.byKey(const Key('voice-banner')), findsNothing);
      expect(rig.connector.channels, hasLength(1));
    });

    testWidgets('cancelling while connecting leaves no trace', (tester) async {
      final rig = VoiceRig();
      await app(tester, rig);
      await tester.tap(find.text('call'));
      await transition(tester);
      expect(_status(tester), '正在接通…');
      await tester.tap(find.text('取消'));
      await transition(tester);
      expect(find.byType(VoiceCallPage), findsNothing);
      expect(find.byKey(const Key('voice-banner')), findsNothing);
      expect(rig.connector.last.sinkClosed, isTrue);
    });
  });
}
