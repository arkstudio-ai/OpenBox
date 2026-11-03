import 'package:bossip_mobile/features/chat/widgets/assistant_tour.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import '../voice/voice_fakes.dart';

void main() {
  testWidgets('the tour walks four steps and closes on the last', (
    tester,
  ) async {
    var closed = 0;
    await tester.pumpWidget(
      ProviderScope(
        overrides: [await zhI18n(tester)],
        child: MaterialApp(
          theme: testTheme(),
          home: Scaffold(body: AssistantTour(onClose: () => closed++)),
        ),
      ),
    );
    await tester.pump();
    expect(find.text('你只跟我说'), findsOneWidget);
    expect(find.textContaining('1 / 4'), findsOneWidget);
    for (var i = 0; i < 3; i++) {
      await tester.tap(find.byKey(const Key('assistant-tour-next')));
      await tester.pump();
    }
    expect(find.text('做好了我回报'), findsOneWidget);
    expect(find.text('知道了'), findsOneWidget);
    await tester.tap(find.byKey(const Key('assistant-tour-next')));
    expect(closed, 1);
    await tester.tap(find.byKey(const Key('assistant-tour-skip')));
    expect(closed, 2);
  });
}
