import 'package:bossip_mobile/features/chat/widgets/cards/plan_card.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'suggestion_fixtures.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late SuggestionFixture fixture;
  setUp(() async {
    fixture = await SuggestionFixture.create(language: 'en-US');
  });
  tearDown(() async => fixture.dispose());

  for (final managed in [true, false]) {
    testWidgets('plan snapshot uses the correct approval path: $managed', (
      tester,
    ) async {
      final plan = MessagePart.fromJson({
        'type': 'plan',
        'id': 'reviewed-plan',
        'path': '/workspace/plan.md',
        'content': 'Implement exactly this version.',
        'status': 'ready',
        if (managed) 'review_via_question': true,
      }) as PlanPart;
      expect(plan.reviewViaQuestion, managed);
      await tester.pumpWidget(fixture.app(SingleChildScrollView(
        child: PlanCard(plan: plan, sessionId: 's1'),
      )));
      await tester.pumpAndSettle();
      expect(find.text('Implement exactly this version.'), findsOneWidget);
      expect(find.widgetWithText(FilledButton, 'Run the plan'),
          managed ? findsNothing : findsOneWidget);
      expect(find.widgetWithText(OutlinedButton, 'Back to discussion'),
          managed ? findsNothing : findsOneWidget);
      expect(find.textContaining('pending plan review'),
          managed ? findsOneWidget : findsNothing);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
    });
  }
}
