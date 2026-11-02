import 'package:bossip_mobile/features/voice/voice_call_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'voice_fakes.dart';

Future<void> _pump(WidgetTester tester) async {
  await tester.pumpWidget(
    ProviderScope(
      key: UniqueKey(),
      overrides: [await zhI18n(tester)],
      child: MaterialApp(
        theme: testTheme(),
        home: Scaffold(
          body: Column(
            children: [
              const Spacer(),
              VoiceCardPanel(
                child: Container(
                  key: const Key('panel-body'),
                  color: Colors.blue,
                ),
              ),
            ],
          ),
        ),
      ),
    ),
  );
  await tester.pump();
}

void main() {
  setUp(() {
    VoiceCardPanel.fraction = 0.42;
    VoiceCardPanel.hidden = false;
  });

  testWidgets('dragging the handle resizes the panel within bounds', (
    tester,
  ) async {
    await _pump(tester);
    final screen = tester.getSize(find.byType(Scaffold)).height;
    expect(
      tester.getSize(find.byKey(const Key('panel-body'))).height,
      closeTo(screen * 0.42, 1),
    );
    await tester.drag(
      find.byKey(const Key('voice-cards-handle')),
      const Offset(0, -120),
    );
    await tester.pump();
    expect(
      tester.getSize(find.byKey(const Key('panel-body'))).height,
      closeTo(screen * 0.42 + 120, 1),
    );
    await tester.drag(
      find.byKey(const Key('voice-cards-handle')),
      const Offset(0, 5000),
    );
    await tester.pump();
    expect(
      tester.getSize(find.byKey(const Key('panel-body'))).height,
      closeTo(screen * VoiceCardPanel.minFraction, 1),
    );
  });

  testWidgets('the chevron hides the panel and the pill brings it back', (
    tester,
  ) async {
    await _pump(tester);
    await tester.tap(find.byKey(const Key('voice-cards-hide')));
    await tester.pump();
    expect(find.byKey(const Key('panel-body')), findsNothing);
    expect(find.text('显示卡片'), findsOneWidget);
    await tester.tap(find.byKey(const Key('voice-cards-show')));
    await tester.pump();
    expect(find.byKey(const Key('panel-body')), findsOneWidget);
  });
}
