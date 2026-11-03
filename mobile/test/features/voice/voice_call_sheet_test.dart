import 'package:bossip_mobile/features/voice/state/voice_call_controller.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_state.dart';
import 'package:bossip_mobile/features/voice/widgets/voice_call_button.dart';
import 'package:bossip_mobile/features/voice/widgets/voice_call_host.dart';
import 'package:bossip_mobile/features/voice/widgets/voice_call_sheet.dart';
import 'package:bossip_mobile/shared/widgets/toast.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'voice_fakes.dart';

const _page = Key('page');

/// The app's shape (VOICE_SEAM_PLAN §13): one page with the call button,
/// the host above it; no call page.
Future<void> _app(WidgetTester tester, VoiceRig rig) async {
  await tester.pumpWidget(
    ProviderScope(
      overrides: [await zhI18n(tester), ...rig.overrides],
      child: MaterialApp(
        theme: testTheme(),
        builder: (context, child) =>
            VoiceCallHost(child: Stack(children: [child!, const ToastHost()])),
        home: Scaffold(
          appBar: AppBar(actions: const [VoiceCallButton(enabled: true)]),
          body: const SizedBox.expand(key: _page),
        ),
      ),
    ),
  );
  await tester.pump();
}

VoiceCallState _call(WidgetTester tester) => ProviderScope.containerOf(
  tester.element(find.byType(VoiceCallHost)),
).read(voiceCallControllerProvider);

Future<void> _settle(WidgetTester tester) async {
  for (var i = 0; i < 6; i++) {
    await tester.pump(const Duration(milliseconds: 100));
  }
}

Future<void> _dialAndConnect(WidgetTester tester, VoiceRig rig) async {
  await tester.tap(find.byKey(const Key('voice-call-button')));
  await _settle(tester);
  expect(rig.connector.channels, hasLength(1));
  rig.connector.last.sendReady();
  await _settle(tester);
}

void _say(
  VoiceRig rig, {
  required String role,
  required String text,
  required String item,
  bool isFinal = true,
}) => rig.connector.last.event({
  'type': 'transcript',
  'role': role,
  'text': text,
  'item_id': item,
  'final': isFinal,
  'at': 1.0,
});

void main() {
  testWidgets('the button dials in place and the sheet drops under the bar', (
    tester,
  ) async {
    final rig = VoiceRig();
    await _app(tester, rig);
    expect(find.byKey(const Key('voice-sheet')), findsNothing);
    await _dialAndConnect(tester, rig);
    expect(find.byKey(const Key('voice-banner')), findsOneWidget);
    expect(find.byKey(const Key('voice-sheet')), findsOneWidget);
    expect(find.byKey(const Key('voice-transcript-empty')), findsOneWidget);
    expect(find.textContaining('已接通'), findsOneWidget);
    // The page is still there, pushed down by the bar only.
    expect(tester.getTopLeft(find.byType(Scaffold)).dy, 44);
    final sheet = tester.getSize(find.byKey(const Key('voice-sheet')));
    expect(
      sheet.height,
      tester.view.physicalSize.height /
          tester.view.devicePixelRatio *
          VoiceCallSheet.fraction,
    );
  });

  testWidgets(
    'what was said shows under the names: the assistant left, me right, the helper while watching',
    (tester) async {
      final rig = VoiceRig();
      await _app(tester, rig);
      await _dialAndConnect(tester, rig);
      _say(rig, role: 'assistant', text: '三条成片做好了。', item: 'a1');
      _say(rig, role: 'user', text: '先说第二条', item: 'u1');
      await _settle(tester);
      expect(find.textContaining('助理'), findsOneWidget);
      expect(find.text('我'), findsOneWidget);
      final assistant = tester.getTopLeft(find.text('三条成片做好了。'));
      final mine = tester.getTopRight(find.text('先说第二条'));
      final width = tester.getSize(find.byKey(const Key('voice-sheet'))).width;
      expect(assistant.dx, lessThan(width / 2));
      expect(mine.dx, greaterThan(width / 2));

      // Watching a task's room: the replies are the helper's.
      rig.connector.last.event({
        'type': 'focus',
        'task': {'task_id': 't1', 'title': '艾薇兰调研', 'session_id': 's1'},
        'bound': true,
      });
      await _settle(tester);
      _say(
        rig,
        role: 'assistant',
        text: '开头换成门店实拍了',
        item: 'a2',
        isFinal: false,
      );
      await _settle(tester);
      expect(find.text('帮手'), findsOneWidget);
      final partial = tester.widget<Text>(find.text('开头换成门店实拍了'));
      expect(partial.style?.fontStyle, FontStyle.italic);
      // Leaving the room keeps the label the line started with.
      rig.connector.last.event({
        'type': 'focus',
        'task': {'task_id': 't1', 'title': '艾薇兰调研', 'session_id': 's1'},
        'bound': false,
      });
      _say(rig, role: 'assistant', text: '开头换成门店实拍了，好了。', item: 'a2');
      await _settle(tester);
      expect(find.text('帮手'), findsOneWidget);
      expect(find.textContaining('助理'), findsOneWidget);
    },
  );

  testWidgets('a swipe up folds the sheet; the bar brings it back', (
    tester,
  ) async {
    final rig = VoiceRig();
    await _app(tester, rig);
    await _dialAndConnect(tester, rig);
    await tester.fling(
      find.byKey(const Key('voice-sheet')),
      const Offset(0, -300),
      1500,
    );
    await _settle(tester);
    expect(find.byKey(const Key('voice-sheet')), findsNothing);
    expect(find.byKey(const Key('voice-banner')), findsOneWidget);
    expect(_call(tester).status, VoiceCallStatus.connected);
    await tester.tap(find.byKey(const Key('voice-banner-open')));
    await _settle(tester);
    expect(find.byKey(const Key('voice-sheet')), findsOneWidget);
    // The handle folds it too.
    await tester.tap(find.byKey(const Key('voice-sheet-handle')));
    await _settle(tester);
    expect(find.byKey(const Key('voice-sheet')), findsNothing);
  });

  testWidgets(
    'hanging up from the sheet shows the summary there; closing clears the call',
    (tester) async {
      final rig = VoiceRig();
      await _app(tester, rig);
      await _dialAndConnect(tester, rig);
      await tester.tap(find.byKey(const Key('voice-hang-up')));
      await _settle(tester);
      rig.connector.last.event({
        'type': 'ended',
        'reason': 'hangup',
        'duration_seconds': 75,
      });
      await _settle(tester);
      expect(find.byKey(const Key('voice-sheet')), findsOneWidget);
      expect(find.text('通话已结束'), findsWidgets);
      expect(find.byKey(const Key('voice-close')), findsOneWidget);
      await tester.tap(find.byKey(const Key('voice-close')));
      await _settle(tester);
      expect(_call(tester).status, VoiceCallStatus.idle);
      expect(find.byKey(const Key('voice-sheet')), findsNothing);
      expect(find.byKey(const Key('voice-banner')), findsNothing);
    },
  );

  testWidgets('the room switch shows on the focused task only, and sends', (
    tester,
  ) async {
    final rig = VoiceRig();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [await zhI18n(tester), ...rig.overrides],
        child: MaterialApp(
          theme: testTheme(),
          builder: (context, child) => VoiceCallHost(child: child!),
          home: Scaffold(
            appBar: AppBar(actions: const [VoiceCallButton(enabled: true)]),
            body: const Column(
              children: [
                VoiceRoomSwitch(key: Key('t1'), taskId: 't1'),
                VoiceRoomSwitch(key: Key('t2'), taskId: 't2'),
              ],
            ),
          ),
        ),
      ),
    );
    await tester.pump();
    expect(find.byKey(const Key('voice-room-switch')), findsNothing);
    await _dialAndConnect(tester, rig);
    rig.connector.last.event({
      'type': 'focus',
      'task': {'task_id': 't1', 'title': '艾薇兰调研'},
      'bound': false,
    });
    await _settle(tester);
    expect(find.byKey(const Key('voice-room-switch')), findsOneWidget);
    expect(find.text('盯现场'), findsOneWidget);
    // The sheet covers the top of the page: fold it to reach the card.
    await tester.tap(find.byKey(const Key('voice-banner-open')));
    await _settle(tester);
    await tester.tap(find.byKey(const Key('voice-room-switch')));
    await _settle(tester);
    expect(
      rig.connector.last.jsonSent
          .where((m) => m['type'] == 'room')
          .map((m) => m['task_id']),
      ['t1'],
    );
  });
}
