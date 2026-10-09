import 'package:bossip_mobile/features/voice/audio/mic_permission.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_controller.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_state.dart';
import 'package:bossip_mobile/features/voice/voice_call_page.dart';
import 'package:bossip_mobile/features/voice/voice_call_prepermission_page.dart';
import 'package:bossip_mobile/features/voice/widgets/voice_call_button.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

import 'voice_fakes.dart';

class _Entry {
  _Entry({
    VoiceCallState call = const VoiceCallState(),
    MicAccess access = MicAccess.granted,
  }) : rig = VoiceRig(access: access) {
    stub = StubVoiceController(call, rig.clock);
  }

  final VoiceRig rig;
  late final StubVoiceController stub;

  Future<void> pump(WidgetTester tester, {bool enabled = true}) async {
    final router = GoRouter(
      routes: [
        GoRoute(
          path: '/',
          builder: (context, state) => Scaffold(
            appBar: AppBar(actions: [VoiceCallButton(enabled: enabled)]),
          ),
        ),
        GoRoute(
          path: Paths.voice,
          builder: (context, state) => const VoiceCallPage(),
        ),
      ],
    );
    addTearDown(router.dispose);
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          await zhI18n(tester),
          ...rig.overrides,
          voiceCallControllerProvider.overrideWith(() => stub),
        ],
        child: MaterialApp.router(theme: testTheme(), routerConfig: router),
      ),
    );
    await tester.pump();
  }
}

Future<void> _settle(WidgetTester tester) async {
  for (var i = 0; i < 10; i++) {
    await tester.pump(const Duration(milliseconds: 100));
  }
}

final _button = find.byKey(const Key('voice-call-button'));

void main() {
  testWidgets('no entry where voice calls are off', (tester) async {
    await _Entry().pump(tester, enabled: false);
    expect(_button, findsNothing);
  });

  testWidgets('first call: the microphone is explained; later stays put', (
    tester,
  ) async {
    final entry = _Entry(access: MicAccess.undetermined);
    await entry.pump(tester);
    expect(find.byIcon(Icons.phone_outlined), findsOneWidget);
    await tester.tap(_button);
    await _settle(tester);
    expect(find.byType(VoicePrepermissionPage), findsOneWidget);
    expect(find.text('需要使用麦克风'), findsOneWidget);
    expect(find.text('和个人助理通话需要麦克风。只在通话时收音，不保存录音。'), findsOneWidget);

    await tester.tap(find.text('以后再说'));
    await _settle(tester);
    expect(find.byType(VoicePrepermissionPage), findsNothing);
    expect(find.byType(VoiceCallPage), findsNothing);
    expect(entry.stub.starts, 0);
    // Nothing was asked of the system yet.
    expect(entry.rig.permission.requests, 0);
  });

  testWidgets('allow opens the call page, which dials', (tester) async {
    final entry = _Entry(access: MicAccess.undetermined);
    await entry.pump(tester);
    await tester.tap(_button);
    await _settle(tester);
    await tester.tap(find.text('允许'));
    await _settle(tester);
    expect(find.byType(VoiceCallPage), findsOneWidget);
    expect(entry.stub.starts, 1);
  });

  testWidgets('with the microphone already granted, straight to the call', (
    tester,
  ) async {
    final entry = _Entry();
    await entry.pump(tester);
    await tester.tap(_button);
    await _settle(tester);
    expect(find.byType(VoicePrepermissionPage), findsNothing);
    expect(find.byType(VoiceCallPage), findsOneWidget);
    expect(entry.stub.starts, 1);
  });

  testWidgets('during a call it shows the call instead of dialling again', (
    tester,
  ) async {
    final entry = _Entry(
      call: connectedCall(at: DateTime(2026)),
      access: MicAccess.undetermined,
    );
    // Even with the switch off, a call in progress keeps its way back.
    await entry.pump(tester, enabled: false);
    expect(find.byIcon(Icons.phone_in_talk), findsOneWidget);
    await tester.tap(_button);
    await _settle(tester);
    expect(find.byType(VoicePrepermissionPage), findsNothing);
    expect(find.byType(VoiceCallPage), findsOneWidget);
    expect(entry.stub.starts, 0);
  });

  testWidgets('declining overlay permission still dials the call', (tester) async {
    final entry = _Entry();
    entry.rig.systemCall.needsOverlay = true;
    await entry.pump(tester);
    await tester.tap(_button);
    await _settle(tester);
    expect(find.text('开启通话悬浮窗'), findsOneWidget);
    await tester.tap(find.text('暂不开启，继续通话'));
    await _settle(tester);
    expect(find.byType(VoiceCallPage), findsOneWidget);
    expect(entry.stub.starts, 1);
    expect(entry.rig.systemCall.overlayRequests, 0);
  });

  testWidgets('returning from overlay settings without a grant still dials', (tester) async {
    final entry = _Entry();
    entry.rig.systemCall.needsOverlay = true;
    await entry.pump(tester);
    await tester.tap(_button);
    await _settle(tester);
    await tester.tap(find.text('去设置'));
    await _settle(tester);
    expect(entry.rig.systemCall.overlayRequests, 1);
    expect(find.byType(VoiceCallPage), findsOneWidget);
    expect(entry.stub.starts, 1);
  });
}
