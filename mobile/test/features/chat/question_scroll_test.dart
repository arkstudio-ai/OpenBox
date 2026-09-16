import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/chat/state/pending_store.dart';
import 'package:bossip_mobile/features/chat/state/question_draft.dart';
import 'package:bossip_mobile/features/chat/widgets/cards/question_dock.dart';
import 'package:bossip_mobile/features/chat/widgets/chat_flow.dart';
import 'package:bossip_mobile/shared/models/interaction.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'suggestion_fixtures.dart';

Finder get _history => find.descendant(
  of: find.byType(ChatFlow),
  matching: find.byType(CustomScrollView),
);

ScrollPosition _position(WidgetTester tester) =>
    tester.widget<CustomScrollView>(_history).controller!.position;

/// Start on the visible question text, rather than the list's empty margin:
/// dragging the margin missed the nested scroll views that trapped gestures.
Offset _onText(WidgetTester tester, String text) =>
    tester.getRect(find.text(text)).intersect(tester.getRect(_history)).center;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  registerQuestionScrollTests();
}

/// Reused on the iOS simulator with its real viewport and native gestures.
void registerQuestionScrollTests({
  List<TargetPlatform> platforms = const [
    TargetPlatform.iOS,
    TargetPlatform.android,
  ],
  bool nativeViewport = false,
  Future<void> Function(String name)? screenshot,
}) {
  late SuggestionFixture fixture;
  setUp(() async {
    fixture = await SuggestionFixture.create(language: 'en-US');
    fixture.api.dio.interceptors.insert(
      0,
      InterceptorsWrapper(
        onRequest: (options, handler) {
          if (options.path != '/api/agent/question/scroll-ask/draft') {
            handler.next(options);
            return;
          }
          final body = options.data as Map<String, dynamic>;
          handler.resolve(
            Response<Map<String, dynamic>>(
              requestOptions: options,
              data: {
                'id': 'scroll-ask',
                'session_id': 's1',
                'draft': body['draft'],
                'draft_revision': (body['revision'] as int) + 1,
              },
            ),
          );
        },
      ),
    );
  });
  tearDown(() => fixture.dispose());

  Future<void> mount(
    WidgetTester tester,
    TargetPlatform platform,
    QuestionItem item, {
    Brightness brightness = Brightness.light,
    double scale = 1,
  }) async {
    if (!nativeViewport) {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(390, 844);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
    }
    fixture.api.status = 'waiting_input';
    fixture.api.messages = [
      answer(
        parts: [
          TextPart(
            id: 'history',
            text: List.generate(
              30,
              (i) => 'Earlier answer line $i',
            ).join('\n\n'),
          ),
        ],
      ),
    ];
    await tester.pumpWidget(
      fixture.app(
        const ChatScreen(sessionId: 's1'),
        platform: platform,
        brightness: brightness,
        scale: scale,
      ),
    );
    await tester.pumpAndSettle();
    fixture.container
        .read(pendingProvider.notifier)
        .addQuestion(
          QuestionRequest(id: 'scroll-ask', sessionId: 's1', questions: [item]),
        );
    await tester.pumpAndSettle();
    expect(find.byType(QuestionDock), findsOneWidget);
  }

  for (final platform in platforms) {
    testWidgets(
      '${platform.name}: four long choices and a custom answer remain reachable',
      (tester) async {
        const labels = [
          '放慢运镜与拿取动作',
          '加强成熟度与表皮纹理细节',
          '更近距离的特写镜头',
          '进一步强化手部结构稳定性',
        ];
        await mount(
          tester,
          platform,
          QuestionItem(
            header: '第 1 段微调方向',
            question: '你希望重点优化哪一方面？（支持在输入框填写具体要求）',
            options: [for (final label in labels) QuestionOption(label: label)],
          ),
          brightness: Brightness.dark,
          scale: 1.2,
        );
        await screenshot?.call('ask-options-bottom');
        final before = _position(tester).pixels;
        expect(find.text(labels.first).hitTestable(), findsOneWidget);
        await tester.drag(find.text(labels.first), const Offset(0, 160));
        await tester.pumpAndSettle();
        expect(_position(tester).pixels, lessThan(before - 100));
        await screenshot?.call('ask-options-history');

        await tester.drag(find.text(labels.first), const Offset(0, -200));
        await tester.pumpAndSettle();
        final field = find.descendant(
          of: find.byType(QuestionDock),
          matching: find.byType(TextField),
        );
        expect(field.hitTestable(), findsOneWidget);
        await tester.enterText(field, '请保留自然光，镜头再放慢一点');
        await tester.pumpAndSettle();
        if (!nativeViewport) {
          // Give the keyboard its real layout cost; showKeyboard alone does
          // not reduce the test viewport.
          tester.view.viewInsets = const FakeViewPadding(bottom: 300);
          addTearDown(tester.view.resetViewInsets);
          await tester.pumpAndSettle();
        }
        expect(
          field.hitTestable(),
          findsOneWidget,
          reason:
              'field=${tester.getRect(field)} history=${tester.getRect(_history)} '
              'scroll=${_position(tester).pixels}/${_position(tester).maxScrollExtent}',
        );
        final confirm = find.descendant(
          of: find.byType(QuestionDock),
          matching: find.byType(FilledButton),
        );
        await screenshot?.call('ask-options-answer');
        // With the keyboard open, a swipe starting on the card must still
        // reach the actions below the focused field.
        final beforeKeyboardDrag = _position(tester).pixels;
        await tester.dragFrom(
          _onText(tester, labels.last),
          const Offset(0, -120),
        );
        await tester.pumpAndSettle();
        if (!nativeViewport) {
          expect(_position(tester).pixels, greaterThan(beforeKeyboardDrag));
        }
        expect(
          confirm.hitTestable(),
          findsOneWidget,
          reason:
              'confirm=${tester.getRect(confirm)} history=${tester.getRect(_history)} '
              'scroll=${_position(tester).pixels}/${_position(tester).maxScrollExtent}',
        );
        expect(tester.widget<FilledButton>(confirm).onPressed, isNotNull);
        await screenshot?.call('ask-options-confirm');
        expect(
          fixture.container.read(questionDraftProvider)['scroll-ask']!.custom,
          ['请保留自然光，镜头再放慢一点'],
        );
        expect(fixture.api.sends, isEmpty);
        expect(tester.takeException(), isNull);
      },
    );

    testWidgets(
      '${platform.name}: drag long ask text to scroll history both ways',
      (tester) async {
        final question = List.generate(
          14,
          (i) => 'Question detail line $i',
        ).join('\n');
        await mount(
          tester,
          platform,
          QuestionItem(
            question: question,
            custom: false,
            options: const [QuestionOption(label: 'Agree')],
          ),
        );
        final before = _position(tester).pixels;
        await tester.dragFrom(_onText(tester, question), const Offset(0, 180));
        await tester.pumpAndSettle();
        final above = _position(tester).pixels;
        expect(above, lessThan(before - 100));

        await tester.dragFrom(_onText(tester, question), const Offset(0, -140));
        await tester.pumpAndSettle();
        expect(_position(tester).pixels, greaterThan(above + 70));
        expect(fixture.api.sends, isEmpty);
        expect(tester.takeException(), isNull);
      },
    );

    testWidgets(
      '${platform.name}: video approval text scrolls with the conversation',
      (tester) async {
        final script = List.generate(
          24,
          (i) => 'Video script line $i',
        ).join('\n');
        await mount(
          tester,
          platform,
          QuestionItem(
            question: 'Approve this script?',
            custom: false,
            options: const [QuestionOption(label: 'Approve')],
            detail: {'kind': 'video_script_approval', 'script_text': script},
          ),
        );
        final before = _position(tester).pixels;
        // The script can be taller than the screen; swipe in its visible middle.
        final start = _onText(tester, script);
        expect(tester.getRect(_history).contains(start), isTrue);
        await tester.dragFrom(start, const Offset(0, 180));
        await tester.pumpAndSettle();
        final above = _position(tester).pixels;
        expect(above, lessThan(before - 100));

        await tester.dragFrom(_onText(tester, script), const Offset(0, -140));
        await tester.pumpAndSettle();
        expect(_position(tester).pixels, greaterThan(above + 70));
        expect(tester.takeException(), isNull);
      },
    );
  }
}
