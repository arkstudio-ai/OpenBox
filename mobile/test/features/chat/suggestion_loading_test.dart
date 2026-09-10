import 'package:bossip_mobile/features/chat/widgets/composer/composer.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/suggestion_chips.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/suggestion_loading.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'suggestion_fixtures.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late SuggestionFixture fixture;
  setUp(() async => fixture = await SuggestionFixture.create());
  tearDown(() => fixture.dispose());

  SuggestionsPart pending({DateTime? expiresAt}) => SuggestionsPart(
    id: testSuggestions.id,
    status: SuggestionStatus.pending,
    expiresAt: expiresAt ?? DateTime.now().add(const Duration(seconds: 60)),
    items: const [],
  );
  Widget composer(
    SuggestionsPart part, {
    Future<void> Function(String, List<String>)? onSend,
  }) => Column(
    children: [
      const Expanded(child: SizedBox()),
      Composer(
        sessionKey: 's1',
        busy: false,
        suggestions: part,
        onSend: onSend ?? (_, _) async {},
      ),
    ],
  );
  Finder beams() => find.descendant(
    of: find.byType(SuggestionLoading),
    matching: find.byType(FractionalTranslation),
  );

  testWidgets('real shimmer keeps the input position when results arrive', (
    tester,
  ) async {
    await tester.pumpWidget(fixture.app(composer(pending())));
    await tester.pump(const Duration(milliseconds: 100));
    expect(beams(), findsNWidgets(3));
    expect(tester.getSize(beams().first).width, greaterThan(100));
    final first = tester
        .widget<FractionalTranslation>(beams().first)
        .translation;
    await tester.pump(const Duration(milliseconds: 300));
    expect(
      tester.widget<FractionalTranslation>(beams().first).translation,
      isNot(first),
    );
    final inputY = tester.getTopLeft(find.byType(TextField)).dy;
    expect(
      find.descendant(
        of: find.byType(SuggestionChips),
        matching: find.byType(OutlinedButton),
      ),
      findsNothing,
    );
    await tester.pumpWidget(fixture.app(composer(testSuggestions)));
    await tester.pumpAndSettle();
    expect(find.byType(SuggestionLoading), findsNothing);
    expect(find.text(sendSuggestion.label), findsOneWidget);
    expect(tester.getTopLeft(find.byType(TextField)).dy, inputY);
    expect(tester.takeException(), isNull);
  });

  testWidgets('pending cannot send a suggestion and typing still sends', (
    tester,
  ) async {
    final sent = <String>[];
    await tester.pumpWidget(
      fixture.app(
        composer(
          pending(),
          onSend: (text, _) async {
            sent.add(text);
          },
        ),
      ),
    );
    await tester.pump(const Duration(milliseconds: 100));
    tester
        .widget<SuggestionChips>(find.byType(SuggestionChips))
        .onSelect(sendSuggestion);
    await tester.pump();
    expect(sent, isEmpty);
    await tester.enterText(find.byType(TextField), '继续这个任务');
    await tester.pump();
    expect(find.byType(SuggestionLoading), findsNothing);
    await tester.tap(find.byIcon(Icons.arrow_upward));
    await tester.pump();
    expect(sent, ['继续这个任务']);
  });

  for (final status in [
    SuggestionStatus.completed,
    SuggestionStatus.unavailable,
  ]) {
    testWidgets('$status with no results removes placeholders', (tester) async {
      await tester.pumpWidget(fixture.app(composer(pending())));
      await tester.pump(const Duration(milliseconds: 100));
      expect(find.byType(SuggestionLoading), findsOneWidget);
      await tester.pumpWidget(
        fixture.app(
          composer(
            SuggestionsPart(
              id: testSuggestions.id,
              status: status,
              items: const [],
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byType(SuggestionChips), findsNothing);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets(
    'deadline expires, cannot restart after remount, disposal cancels timers',
    (tester) async {
      await tester.pumpWidget(fixture.app(composer(pending())));
      await tester.pump(const Duration(seconds: 61));
      expect(beams(), findsNothing);
      await tester.enterText(find.byType(TextField), 'draft');
      await tester.pump();
      await tester.pumpWidget(
        fixture.app(
          composer(
            pending(
              expiresAt: DateTime.now().subtract(const Duration(seconds: 1)),
            ),
          ),
        ),
      );
      await tester.enterText(find.byType(TextField), '');
      await tester.pump();
      expect(beams(), findsNothing);
      await tester.pumpWidget(fixture.app(composer(pending())));
      await tester.pump();
      expect(beams(), findsNWidgets(3));
      await tester.pumpWidget(const SizedBox());
      await tester.pump(const Duration(seconds: 61));
      expect(tester.takeException(), isNull);
    },
  );

  for (final platform in [TargetPlatform.iOS, TargetPlatform.android]) {
    for (final brightness in Brightness.values) {
      for (final reduced in [false, true]) {
        testWidgets(
          '320px $platform $brightness reduced motion $reduced with keyboard',
          (tester) async {
            tester.view.physicalSize = const Size(320, 844);
            tester.view.devicePixelRatio = 1;
            addTearDown(tester.view.resetPhysicalSize);
            addTearDown(tester.view.resetDevicePixelRatio);
            addTearDown(tester.view.resetViewInsets);
              final semantics = tester.ensureSemantics();
            await tester.pumpWidget(
              fixture.app(
                composer(pending()),
                platform: platform,
                brightness: brightness,
                reduceMotion: reduced,
                scale: 1.5,
              ),
            );
            await tester.pump(const Duration(milliseconds: 100));
            expect(find.bySemanticsLabel('正在生成下一步建议'), findsOneWidget);
            expect(beams(), reduced ? findsNothing : findsNWidgets(3));
            await tester.drag(
              find.byType(SuggestionLoading),
              const Offset(-200, 0),
            );
            await tester.pump(const Duration(milliseconds: 400));
            await tester.showKeyboard(find.byType(TextField));
            tester.view.viewInsets = const FakeViewPadding(bottom: 300);
            await tester.pump(const Duration(milliseconds: 400));
            expect(
              tester.getBottomLeft(find.byType(Composer)).dy,
              lessThanOrEqualTo(544),
            );
            expect(
              tester.getBottomLeft(find.byType(SuggestionLoading)).dy,
              lessThan(tester.getTopLeft(find.byType(TextField)).dy),
            );
              expect(tester.takeException(), isNull);
              semantics.dispose();
          },
        );
      }
    }
  }
}
