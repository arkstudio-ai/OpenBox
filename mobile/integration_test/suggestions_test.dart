import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/suggestion_chips.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';

import '../test/features/chat/suggestion_fixtures.dart';

/// Native-device verification with real Flutter widgets and fully isolated
/// REST/WS fixtures. No authentication, production writes or model calls.
void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  late SuggestionFixture fixture;
  setUp(() async => fixture = await SuggestionFixture.create());
  tearDown(() => fixture.dispose());

  testWidgets(
    'native aligned suggestions, complete labels, themes and draft keyboard',
    (tester) async {
      fixture.api.messages = [
        answer(
          parts: const [
            TextPart(
              id: 'body',
              text: '已完成聊天建议功能。\n\n建议会在回复完成后显示，帮助你继续测试、调整样式或检查移动端布局。',
            ),
            layoutSuggestions,
          ],
        ),
      ];
      await tester.pumpWidget(fixture.app(const ChatScreen(sessionId: 's1')));
      await tester.pumpAndSettle();
      expect(find.byType(SuggestionChips), findsOneWidget);
      await binding.takeScreenshot('native-suggestions-light');

      await tester.pumpWidget(
        fixture.app(
          const ChatScreen(sessionId: 's1'),
          brightness: Brightness.dark,
          scale: 1.3,
        ),
      );
      await tester.pumpAndSettle();
      await binding.takeScreenshot('native-suggestions-dark-large');
      for (final item in layoutSuggestions.items) {
        expect(find.text(item.label).hitTestable(), findsOneWidget);
      }
      await tester.tap(find.text(layoutSuggestions.items[1].label));
      await tester.pumpAndSettle();
      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, layoutSuggestions.items[1].prompt);
      expect(field.focusNode!.hasFocus, isTrue);
      expect(find.byType(SuggestionChips), findsNothing);
      expect(fixture.api.sends, isEmpty);
      await binding.takeScreenshot('native-suggestions-draft-keyboard');
      expect(tester.takeException(), isNull);
    },
  );
}
