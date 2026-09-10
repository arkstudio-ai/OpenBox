part of 'question_dock_test.dart';

void registerQuestionPagerTests() {
  for (final width in [320.0, 390.0]) {
    testWidgets(
      'pager at ${width}px shows one question and keeps multiselect editable',
      (tester) async {
        tester.view.devicePixelRatio = 1;
        tester.view.physicalSize = Size(width, 844);
        addTearDown(tester.view.resetDevicePixelRatio);
        addTearDown(tester.view.resetPhysicalSize);
        final (api, _) = await _mount(
          tester,
          const _Transcript(turns: 0),
          questions: [_pagedRequest('pages')],
        );
        expect(find.text('1/3'), findsOneWidget);
        expect(find.text('Extras'), findsNothing);
        expect(
          tester
              .widget<TextButton>(find.widgetWithText(TextButton, 'Previous'))
              .onPressed,
          isNull,
        );
        await tester.tap(find.text('30s'));
        await tester.pump();
        expect(find.text('2/3'), findsOneWidget);
        expect(find.text('Duration'), findsNothing);
        await tester.tap(find.text('Captions'));
        await tester.tap(find.text('Music'));
        await tester.pump();
        expect(find.text('2/3'), findsOneWidget);
        await tester.tap(find.text('Previous'));
        await tester.pump();
        expect(
          tester
              .widget<ChoiceChip>(find.widgetWithText(ChoiceChip, '30s'))
              .selected,
          isTrue,
        );
        await tester.tap(find.text('60s'));
        await tester.pump();
        expect(
          tester
              .widget<ChoiceChip>(find.widgetWithText(ChoiceChip, 'Music'))
              .selected,
          isTrue,
        );
        await tester.tap(find.text('Next'));
        await tester.pump();
        expect(find.text('3/3'), findsOneWidget);
        await tester.enterText(find.byType(TextField), 'Square');
        await tester.testTextInput.receiveAction(TextInputAction.done);
        await tester.pump();
        expect(api.replies, isEmpty);
        expect(_canSubmit(tester), isTrue);
        expect(
          tester
              .widget<TextButton>(find.widgetWithText(TextButton, 'Next'))
              .onPressed,
          isNull,
        );
        expect(tester.takeException(), isNull);
        await tester.tap(find.text('Confirm'));
        await tester.pumpAndSettle();
        expect(api.replies['pages'], [
          ['60s'],
          ['Captions', 'Music'],
          ['Square'],
        ]);
      },
    );
  }

  testWidgets(
    'typing and IME composition stay on the page; keyboard Next completes custom input',
    (tester) async {
      final (api, container) = await _mount(
        tester,
        const _Transcript(turns: 0),
        questions: [_pagedRequest('typing')],
      );
      await tester.enterText(find.byType(TextField), ' ');
      await tester.testTextInput.receiveAction(TextInputAction.next);
      await tester.pump();
      expect(find.text('1/3'), findsOneWidget);
      await tester.showKeyboard(find.byType(TextField));
      tester.testTextInput.updateEditingValue(
        const TextEditingValue(
          text: '输入中',
          composing: TextRange(start: 0, end: 3),
        ),
      );
      await tester.pump();
      expect(find.text('1/3'), findsOneWidget);
      await tester.enterText(find.byType(TextField), '45秒');
      await tester.pump();
      expect(find.text('1/3'), findsOneWidget);
      await tester.testTextInput.receiveAction(TextInputAction.next);
      await tester.pump();
      expect(find.text('2/3'), findsOneWidget);
      expect(container.read(questionDraftProvider)['typing']!.answers.first, [
        '45秒',
      ]);
      expect(api.replies, isEmpty);
      await tester.pump(const Duration(milliseconds: 500));
      await tester.pumpAndSettle();
    },
  );

  testWidgets(
    'manual navigation persists its page across list rebuilds without saving an answer',
    (tester) async {
      final (api, container) = await _mount(
        tester,
        const _Transcript(turns: 1),
        questions: [_pagedRequest('manual')],
      );
      await tester.tap(find.text('Next'));
      await tester.pump(const Duration(milliseconds: 500));
      expect(find.text('2/3'), findsOneWidget);
      tester.state<_TranscriptState>(find.byType(_Transcript)).grow();
      await tester.pumpAndSettle();
      expect(find.text('2/3'), findsOneWidget);
      expect(_canSubmit(tester), isFalse);
      expect(api.savedDrafts, isEmpty);
      expect(api.replies, isEmpty);
      final cached =
          jsonDecode(
                container
                    .read(prefsProvider)
                    .getString('openbox:question-draft:anonymous:manual')!,
              )
              as Map<String, dynamic>;
      expect(cached['page'], 1);
      expect(cached['revision'], 0);
    },
  );

  testWidgets('cached page and draft restore together after app restart', (
    tester,
  ) async {
    await _mount(
      tester,
      const _Transcript(turns: 0),
      questions: [_pagedRequest('cached-pages')],
      preferences: {
        'openbox:question-draft:anonymous:cached-pages': jsonEncode({
          'revision': 0,
          'page': 1,
          'draft': [
            {
              'selected': ['30s'],
              'custom': '',
              'use_custom': false,
            },
            {
              'selected': ['Music'],
              'custom': '',
              'use_custom': false,
            },
            {'selected': <String>[], 'custom': '', 'use_custom': false},
          ],
        }),
      },
    );
    expect(find.text('2/3'), findsOneWidget);
    expect(
      tester
          .widget<ChoiceChip>(find.widgetWithText(ChoiceChip, 'Music'))
          .selected,
      isTrue,
    );
    await tester.tap(find.text('Previous'));
    await tester.pump();
    expect(
      tester
          .widget<ChoiceChip>(find.widgetWithText(ChoiceChip, '30s'))
          .selected,
      isTrue,
    );
    await tester.pump(const Duration(milliseconds: 500));
    await tester.pumpAndSettle();
  });

  testWidgets('fresh server drafts start on the first unanswered page', (
    tester,
  ) async {
    await _mount(
      tester,
      const _Transcript(turns: 0),
      questions: [
        _pagedRequest(
          'server-pages',
          draft: [
            const QuestionDraftAnswer(selected: ['30s']),
          ],
        ),
      ],
    );
    expect(find.text('2/3'), findsOneWidget);
  });

  testWidgets(
    'pager is disabled during submission, including after a card rebuild',
    (tester) async {
      final (api, _) = await _mount(
        tester,
        const _Transcript(turns: 1),
        questions: [_pagedRequest('busy-pages')],
      );
      await _answerPages(tester);
      api.gate = Completer<void>();
      await tester.tap(find.text('Confirm'));
      await tester.pump();
      tester.state<_TranscriptState>(find.byType(_Transcript)).grow();
      await tester.pump();
      expect(find.text('3/3'), findsOneWidget);
      expect(
        tester
            .widget<TextButton>(find.widgetWithText(TextButton, 'Previous'))
            .onPressed,
        isNull,
      );
      expect(
        tester
            .widget<TextButton>(find.widgetWithText(TextButton, 'Next'))
            .onPressed,
        isNull,
      );
      expect(tester.widget<TextField>(find.byType(TextField)).enabled, isFalse);
      expect(api.replies.length, 1);
      api.gate!.complete();
      await tester.pumpAndSettle();
      expect(find.byType(QuestionDock), findsNothing);
    },
  );

  testWidgets(
    'failed submission preserves the last page and every answer for retry',
    (tester) async {
      final (api, _) = await _mount(
        tester,
        const _Transcript(turns: 0),
        questions: [_pagedRequest('retry-pages')],
      );
      await _answerPages(tester);
      api.failure = 500;
      await tester.tap(find.text('Confirm'));
      await tester.pumpAndSettle();
      expect(find.text('3/3'), findsOneWidget);
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'Square',
      );
      api.failure = null;
      await tester.tap(find.text('Confirm'));
      await tester.pumpAndSettle();
      expect(api.replies['retry-pages'], [
        ['30s'],
        ['Captions'],
        ['Square'],
      ]);
      await tester.pump(const Duration(seconds: 5));
    },
  );

  testWidgets('skip on an intermediate page never submits partial answers', (
    tester,
  ) async {
    final (api, _) = await _mount(
      tester,
      const _Transcript(turns: 0),
      questions: [_pagedRequest('skip-pages')],
    );
    await tester.tap(find.text('30s'));
    await tester.pump();
    await tester.tap(find.text('Skip all'));
    await tester.pumpAndSettle();
    expect(api.skipped, ['skip-pages']);
    expect(api.replies, isEmpty);
    expect(find.byType(QuestionDock), findsNothing);
  });

  for (final count in [2, 4]) {
    testWidgets(
      '$count pages advance exactly to the last page without auto submission',
      (tester) async {
        final request = QuestionRequest(
          id: 'count-$count',
          sessionId: 's1',
          questions: [
            for (var i = 0; i < count; i++)
              QuestionItem(
                question: 'Question $i',
                custom: false,
                options: [QuestionOption(label: 'Answer $i')],
              ),
          ],
        );
        final (api, _) = await _mount(
          tester,
          const _Transcript(turns: 0),
          questions: [request],
        );
        for (var i = 0; i < count; i++) {
          expect(find.text('${i + 1}/$count'), findsOneWidget);
          await tester.tap(find.text('Answer $i'));
          await tester.pump();
        }
        expect(find.text('$count/$count'), findsOneWidget);
        expect(api.replies, isEmpty);
        await tester.tap(find.text('Confirm'));
        await tester.pumpAndSettle();
        expect(api.replies[request.id], [
          for (var i = 0; i < count; i++) ['Answer $i'],
        ]);
      },
    );
  }

  for (final invalidPage in [-1, 99, 1.5, 'broken']) {
    testWidgets('invalid cached page $invalidPage cannot hide a question', (
      tester,
    ) async {
      await _mount(
        tester,
        const _Transcript(turns: 0),
        questions: [_pagedRequest('invalid-page')],
        preferences: {
          'openbox:question-draft:anonymous:invalid-page': jsonEncode({
            'revision': 0,
            'page': invalidPage,
            'draft': List.generate(
              3,
              (_) => {
                'selected': <String>[],
                'custom': '',
                'use_custom': false,
              },
            ),
          }),
        },
      );
      expect(find.text('1/3'), findsOneWidget);
      expect(find.text('Duration'), findsOneWidget);
    });
  }

  testWidgets('page preference never crosses an account switch', (
    tester,
  ) async {
    final (_, container) = await _mount(
      tester,
      const _Transcript(turns: 0),
      questions: [_pagedRequest('private-page')],
    );
    await tester.tap(find.text('Next'));
    await tester.pump();
    expect(find.text('2/3'), findsOneWidget);
    container
        .read(authProvider.notifier)
        .setAuth(
          'test-token',
          const AuthUser(id: 'new-user', username: 'new-user'),
        );
    await tester.pump();
    container
        .read(pendingProvider.notifier)
        .addQuestion(_pagedRequest('private-page'));
    await tester.pump();
    expect(find.text('1/3'), findsOneWidget);
  });
}
