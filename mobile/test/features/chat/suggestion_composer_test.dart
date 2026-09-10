import 'dart:async';

import 'package:bossip_mobile/features/chat/widgets/composer/composer.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/resource_slot.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/suggestion_chips.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:bossip_mobile/shared/models/resource.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'suggestion_fixtures.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late SuggestionFixture fixture;
  setUp(() async => fixture = await SuggestionFixture.create());
  tearDown(() => fixture.dispose());

  Widget composer({
    Future<void> Function(String, List<String>)? onSend,
    String sessionKey = 's1',
    bool busy = false,
    ComposerResourceSlot? resources,
    SuggestionsPart suggestions = testSuggestions,
  }) => Column(
    children: [
      const Expanded(child: SizedBox()),
      Composer(
        sessionKey: sessionKey,
        busy: busy,
        suggestions: suggestions,
        resources: resources,
        onSend: onSend ?? (_, _) async {},
      ),
    ],
  );

  testWidgets(
    'send uses full prompt, prevents double taps and stays dismissed',
    (tester) async {
      final sent = <String>[];
      final gate = Completer<void>();
      await tester.pumpWidget(
        fixture.app(
          composer(
            onSend: (text, attachments) {
              sent.add(text);
              expect(attachments, isEmpty);
              return gate.future;
            },
          ),
        ),
      );
      await tester.pumpAndSettle();
      // Invoke twice before the next frame, as two fast touches can do.
      final onTap = tester
          .widget<SuggestionChips>(find.byType(SuggestionChips))
          .onSelect;
      onTap(sendSuggestion);
      onTap(sendSuggestion);
      await tester.pump();
      expect(sent, [sendSuggestion.prompt]);
      expect(find.byType(SuggestionChips), findsNothing);
      await tester.enterText(find.byType(TextField), 'A new draft');
      gate.complete();
      await tester.pumpAndSettle();
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'A new draft',
      );
      await tester.enterText(find.byType(TextField), '');
      expect(find.byType(SuggestionChips), findsNothing);
    },
  );

  testWidgets(
    'draft only fills input, focuses keyboard and places caret at end',
    (tester) async {
      var calls = 0;
      await tester.pumpWidget(
        fixture.app(
          composer(
            onSend: (_, _) async {
              calls++;
            },
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text(draftSuggestion.label));
      await tester.pumpAndSettle();
      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, draftSuggestion.prompt);
      expect(
        field.controller!.selection.baseOffset,
        draftSuggestion.prompt.length,
      );
      expect(field.focusNode!.hasFocus, isTrue);
      expect(calls, 0);
      expect(find.byType(SuggestionChips), findsNothing);
    },
  );

  for (final newerDraft in [false, true]) {
    testWidgets(
      'failed send restores prompt without erasing newer text ($newerDraft)',
      (tester) async {
        final gate = Completer<void>();
        await tester.pumpWidget(
          fixture.app(composer(onSend: (_, _) => gate.future)),
        );
        await tester.pumpAndSettle();
        await tester.tap(find.text(sendSuggestion.label));
        await tester.pump();
        if (newerDraft) {
          await tester.enterText(find.byType(TextField), 'Keep this');
        }
        gate.completeError(StateError('offline'));
        await tester.pumpAndSettle();
        final field = tester.widget<TextField>(find.byType(TextField));
        expect(
          field.controller!.text,
          newerDraft ? 'Keep this' : sendSuggestion.prompt,
        );
        expect(find.byType(SuggestionChips), findsNothing);
      },
    );
  }

  testWidgets('late failure never restores a prompt into another session', (
    tester,
  ) async {
    final gate = Completer<void>();
    await tester.pumpWidget(
      fixture.app(composer(onSend: (_, _) => gate.future)),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text(sendSuggestion.label));
    await tester.pump();
    await tester.pumpWidget(fixture.app(composer(sessionKey: 's2')));
    gate.completeError(StateError('offline'));
    await tester.pumpAndSettle();
    expect(
      tester.widget<TextField>(find.byType(TextField)).controller!.text,
      isEmpty,
    );
    expect(find.byType(SuggestionChips), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('late completion after unmount is harmless', (tester) async {
    final gate = Completer<void>();
    await tester.pumpWidget(
      fixture.app(composer(onSend: (_, _) => gate.future)),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text(sendSuggestion.label));
    await tester.pumpWidget(const SizedBox());
    gate.completeError(StateError('offline'));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
  });

  testWidgets('draft, busy, upload and attachment hide suggestions', (
    tester,
  ) async {
    final uploaded = Completer<List<Resource>>();
    final slot = ComposerResourceSlot(
      mentionSection:
          (_, {required query, required projectId, required onPick}) =>
              const SizedBox(),
      pickAndUpload: (_, {required projectId}) => uploaded.future,
    );
    await tester.pumpWidget(fixture.app(composer(resources: slot)));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), ' ');
    await tester.pump();
    expect(find.byType(SuggestionChips), findsNothing);
    await tester.enterText(find.byType(TextField), '');
    await tester.pump();
    expect(find.byType(SuggestionChips), findsOneWidget);
    await tester.pumpWidget(fixture.app(composer(busy: true, resources: slot)));
    expect(find.byType(SuggestionChips), findsNothing);
    await tester.pumpWidget(fixture.app(composer(resources: slot)));
    await tester.tap(find.byIcon(Icons.add));
    await tester.pumpAndSettle();
    await tester.tap(find.byIcon(Icons.upload_outlined));
    await tester.pump(const Duration(milliseconds: 400));
    expect(find.byType(SuggestionChips), findsNothing);
    uploaded.complete([
      Resource.fromJson({'id': 'asset', 'name': 'notes.txt'}),
    ]);
    await tester.pumpAndSettle();
    expect(find.text('notes.txt'), findsOneWidget);
    expect(find.byType(SuggestionChips), findsNothing);
  });

  for (final width in [320.0, 390.0, 430.0]) {
    for (final brightness in Brightness.values) {
      for (final language in ['zh-CN', 'en-US']) {
        testWidgets(
          'native layout $width $brightness $language with keyboard and large text',
          (tester) async {
            tester.view.physicalSize = Size(width, 844);
            tester.view.devicePixelRatio = 1;
            addTearDown(tester.view.resetPhysicalSize);
            addTearDown(tester.view.resetDevicePixelRatio);
            addTearDown(tester.view.resetViewInsets);
            fixture.container.read(i18nProvider.notifier).setLanguage(language);
            final suggestions = language == 'zh-CN'
                ? testSuggestions
                : SuggestionsPart(
                    id: 'english',
                    items: [
                      NextStepSuggestion(
                        label: 'Add automated tests',
                        prompt: sendSuggestion.prompt,
                        mode: SuggestionMode.send,
                      ),
                      NextStepSuggestion(
                        label: 'Adjust the visual style',
                        prompt: draftSuggestion.prompt,
                        mode: SuggestionMode.draft,
                      ),
                      const NextStepSuggestion(
                        label: 'Check mobile layout',
                        prompt: 'Check the mobile layout.',
                        mode: SuggestionMode.send,
                      ),
                    ],
                  );
            await tester.pumpWidget(
              fixture.app(
                composer(suggestions: suggestions),
                brightness: brightness,
                scale: 1.5,
              ),
            );
            await tester.pumpAndSettle();
            final row = find.byType(SuggestionChips);
            final buttons = find.descendant(
              of: row,
              matching: find.byType(OutlinedButton),
            );
            expect(buttons, findsNWidgets(3));
            expect(
              tester.getSize(buttons.first).height,
              greaterThanOrEqualTo(44),
            );
            expect(
              tester.getBottomLeft(row).dy,
              lessThan(tester.getTopLeft(find.byType(TextField)).dy),
            );
            final semantics = tester.ensureSemantics();
            final label = suggestions.items.first.label;
            expect(
              find.bySemanticsLabel(
                language == 'zh-CN' ? '发送建议：$label' : 'Send suggestion: $label',
              ),
              findsOneWidget,
            );
            semantics.dispose();
            await tester.drag(row, const Offset(-500, 0));
            await tester.pumpAndSettle();
            await tester.drag(row, const Offset(-500, 0));
            await tester.pumpAndSettle();
            expect(buttons.last.hitTestable(), findsOneWidget);
            // Keyboard changes the viewport while the empty input has focus.
            await tester.showKeyboard(find.byType(TextField));
            tester.view.viewInsets = const FakeViewPadding(bottom: 300);
            await tester.pumpAndSettle();
            expect(
              tester.getBottomLeft(find.byType(Composer)).dy,
              lessThanOrEqualTo(544),
            );
            expect(find.byType(SuggestionChips), findsOneWidget);
            expect(tester.takeException(), isNull);
          },
        );
      }
    }
  }
}
