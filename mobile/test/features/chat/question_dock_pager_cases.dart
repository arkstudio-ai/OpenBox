part of 'question_dock_test.dart';

// Pagination/draft persistence scenarios share the dock fixture without
// making either test source exceed the repository's 800-line limit.
void _registerPagerCases() {
  testWidgets('three numbered questions require all answers and submit once', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(390, 844);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    final request = QuestionRequest(
      id: 'three',
      sessionId: 's1',
      questions: [
        for (var i = 1; i <= 3; i++)
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
    expect(find.text('1/3'), findsOneWidget);
    expect(find.text('Question 1'), findsOneWidget);
    expect(find.text('Question 3'), findsNothing);
    expect(_canSubmit(tester), isFalse);
    await tester.tap(find.text('Answer 1'));
    await tester.pump();
    expect(find.text('2/3'), findsOneWidget);
    expect(find.text('Question 1'), findsNothing);
    expect(_canSubmit(tester), isFalse);
    await tester.tap(find.text('Answer 2'));
    await tester.pump();
    expect(find.text('3/3'), findsOneWidget);
    await tester.tap(find.text('Answer 3'));
    await tester.pump();
    expect(_canSubmit(tester), isTrue);
    expect(api.replies, isEmpty);
    await tester.tap(find.text('Confirm'));
    await tester.pumpAndSettle();
    expect(api.replies['three'], [
      ['Answer 1'],
      ['Answer 2'],
      ['Answer 3'],
    ]);
  });

  testWidgets(
    'server draft restores and switching to an option preserves text without submitting it',
    (tester) async {
      final request = QuestionRequest(
        id: 'saved',
        sessionId: 's1',
        questions: _request('saved').questions,
        draftRevision: 7,
        draft: const [QuestionDraftAnswer(custom: 'my draft', useCustom: true)],
      );
      final (api, container) = await _mount(
        tester,
        const _Transcript(turns: 0),
        questions: [request],
      );
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'my draft',
      );
      await tester.tap(find.text('Option A'));
      await tester.pump();
      expect(container.read(questionDraftProvider)['saved']!.custom, [
        'my draft',
      ]);
      await tester.tap(find.text('Confirm'));
      await tester.pumpAndSettle();
      expect(api.replies['saved'], [
        ['Option A'],
      ]);
    },
  );

  testWidgets('failed reply preserves selections for retry', (tester) async {
    final (api, _) = await _mount(
      tester,
      const _Transcript(turns: 0),
      questions: [_request('retry')],
    );
    api.failure = 500;
    await tester.tap(find.text('Option A'));
    await tester.pump();
    await tester.tap(find.text('Confirm'));
    await tester.pumpAndSettle();
    expect(find.text('Submission failed; answers preserved'), findsOneWidget);
    expect(_picked(tester), isTrue);
    expect(_canSubmit(tester), isTrue);
    api.failure = null;
    await tester.tap(find.text('Confirm'));
    await tester.pumpAndSettle();
    expect(find.byType(QuestionDock), findsNothing);
    await tester.pump(const Duration(seconds: 5));
  });

  testWidgets('410 removes card and late asked events cannot restore it', (
    tester,
  ) async {
    final (api, container) = await _mount(
      tester,
      const _Transcript(turns: 0),
      questions: [_request('gone')],
    );
    api.failure = 410;
    await tester.tap(find.text('Option A'));
    await tester.pump();
    await tester.tap(find.text('Confirm'));
    await tester.pumpAndSettle();
    container.read(pendingProvider.notifier).addQuestion(_request('gone'));
    container.read(pendingProvider.notifier).seed([], [_request('gone')]);
    await tester.pumpAndSettle();
    expect(find.byType(QuestionDock), findsNothing);
    expect(container.read(questionDraftProvider), isEmpty);
    await tester.pump(const Duration(seconds: 5));
  });

  testWidgets('skip holds all controls disabled across a rebuild', (
    tester,
  ) async {
    final (api, _) = await _mount(
      tester,
      const _Transcript(turns: 1),
      questions: [_request('skip')],
    );
    api.gate = Completer<void>();
    await tester.tap(find.text('Skip'));
    await tester.pump();
    tester.state<_TranscriptState>(find.byType(_Transcript)).grow();
    await tester.pump();
    expect(
      tester
          .widget<ChoiceChip>(find.widgetWithText(ChoiceChip, 'Option A'))
          .onSelected,
      isNull,
    );
    expect(tester.widget<TextField>(find.byType(TextField)).enabled, isFalse);
    expect(api.skipped, ['skip']);
    api.gate!.complete();
    await tester.pumpAndSettle();
    expect(find.byType(QuestionDock), findsNothing);
  });

  testWidgets(
    'locally cached unsent draft survives app restart and syncs its revision',
    (tester) async {
      final (api, container) = await _mount(
        tester,
        const _Transcript(turns: 0),
        questions: [_request('cached')],
        preferences: {
          'openbox:question-draft:anonymous:cached': jsonEncode({
            'revision': 0,
            'draft': [
              {
                'selected': <String>[],
                'custom': 'offline text',
                'use_custom': true,
              },
            ],
          }),
          'openbox:question-draft:another-user:cached': jsonEncode({
            'revision': 0,
            'draft': [
              {
                'selected': ['Option B'],
                'custom': 'private',
                'use_custom': true,
              },
            ],
          }),
        },
      );
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'offline text',
      );
      await tester.pump(const Duration(milliseconds: 500));
      await tester.pumpAndSettle();
      expect(api.savedDrafts.single.$3, 0);
      expect(api.savedDrafts.single.$2.single.custom, 'offline text');
      expect(container.read(questionDraftProvider)['cached']!.revision, 1);
      expect(api.replies, isEmpty);
    },
  );

  testWidgets(
    'reconnect updates a clean draft without replacing unsaved local edits',
    (tester) async {
      final (_, container) = await _mount(
        tester,
        const _Transcript(turns: 0),
        questions: [_request('live')],
      );
      QuestionRequest remote(int rev, String value) => QuestionRequest(
        id: 'live',
        sessionId: 's1',
        questions: _request('live').questions,
        draftRevision: rev,
        draft: [QuestionDraftAnswer(custom: value, useCustom: true)],
      );
      container
          .read(pendingProvider.notifier)
          .addQuestion(remote(1, 'from web'));
      await tester.pump();
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'from web',
      );
      await tester.enterText(find.byType(TextField), 'local edit');
      container
          .read(pendingProvider.notifier)
          .addQuestion(remote(2, 'another web edit'));
      await tester.pump();
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'local edit',
      );
      expect(container.read(questionDraftProvider)['live']!.revision, 1);
      container.read(pendingProvider.notifier).removeQuestion('live');
      await tester.pump();
    },
  );

  testWidgets('switching accounts never restores the previous users draft', (
    tester,
  ) async {
    final (_, container) = await _mount(
      tester,
      const _Transcript(turns: 0),
      questions: [_request('private')],
    );
    await tester.enterText(find.byType(TextField), 'private old-user answer');
    container
        .read(authProvider.notifier)
        .setAuth('test-token', const AuthUser(id: 'u2', username: 'user2'));
    await tester.pump();
    expect(container.read(pendingProvider).questionsOf('s1'), isEmpty);
    container.read(pendingProvider.notifier).addQuestion(_request('private'));
    await tester.pump();
    expect(
      tester.widget<TextField>(find.byType(TextField)).controller!.text,
      isEmpty,
    );
    expect(container.read(questionDraftProvider)['private']!.answers, [
      <String>[],
    ]);
  });

  testWidgets('draft conflict preserves edits and retry uses fresh revision', (
    tester,
  ) async {
    final (api, container) = await _mount(
      tester,
      const _Transcript(turns: 0),
      questions: [_request('conflict')],
    );
    api.draftFailure = 409;
    await tester.enterText(find.byType(TextField), 'keep this draft');
    await tester.pump(const Duration(milliseconds: 500));
    await tester.pumpAndSettle();
    expect(find.text('Draft changed elsewhere'), findsOneWidget);
    expect(
      tester.widget<TextField>(find.byType(TextField)).controller!.text,
      'keep this draft',
    );
    api.draftFailure = null;
    await tester.tap(find.text('Retry saving'));
    await tester.pumpAndSettle();
    expect(api.savedDrafts.single.$3, 8);
    expect(api.savedDrafts.single.$2.single.custom, 'keep this draft');
    expect(container.read(questionDraftProvider)['conflict']!.revision, 9);
    expect(api.replies, isEmpty);
  });

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
