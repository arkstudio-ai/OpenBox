import 'dart:async';

import 'package:bossip_mobile/features/memory/memory_detail_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'knowledge_fixture.dart';

Future<void> _openDetail(WidgetTester tester) async {
  await tapVisible(tester, find.text('Use Shanghai timezone'));
  expect(find.byType(MemoryDetailPage), findsOneWidget);
}

Finder _inDetail(Finder finder) =>
    find.descendant(of: find.byType(MemoryDetailPage), matching: finder);

void main() {
  setUpKnowledgeTests();

  testWidgets(
    'a memory\'s sources and history are read without writing anything',
    (tester) async {
      final server = FakeKnowledgeServer();
      final harness = await mountKnowledge(tester, server);
      await _openDetail(tester);

      expect(find.text('记忆详情'), findsOneWidget);
      expect(_inDetail(find.text('Use Shanghai timezone')), findsOneWidget);
      expect(_inDetail(find.text('个人')), findsOneWidget);
      expect(
        _inDetail(find.text('Original immutable evidence')),
        findsOneWidget,
      );
      expect(_inDetail(find.text('你在对话中说')), findsOneWidget);
      expect(_inDetail(find.text('Working hours')), findsOneWidget);

      await tapVisible(tester, _inDetail(find.textContaining('修改记录')));
      expect(_inDetail(find.text('Earlier value')), findsOneWidget);
      expect(_inDetail(find.textContaining('你修改了')), findsOneWidget);

      expect(
        server.calls,
        containsAll([
          'GET /api/memories/memory-1',
          'GET /api/memories/memory-1/cleanup',
          'GET /api/memories/memory-1/sources',
          'GET /api/memories/memory-1/history',
        ]),
      );
      expect(server.writes(), isEmpty);

      await tapVisible(tester, _inDetail(find.text('打开对话')));
      expect(harness.location, '/app/s/session-1');
    },
  );

  testWidgets('a topic opens from its memory, and back returns to the memory', (
    tester,
  ) async {
    final server = FakeKnowledgeServer();
    server.handlers['GET /api/memory-wiki/pages/page-1'] = (_) => {
      'id': 'page-1',
      'slug': 'working-hours',
      'title': 'Working hours',
      'status': 'published',
      'body_available': true,
      'body': '# Working hours\n\nWe work from Shanghai.',
      'paragraphs': <Object>[],
      'source_details': <Object>[],
    };
    final harness = await mountKnowledge(tester, server);
    await _openDetail(tester);

    await tapVisible(tester, _inDetail(find.text('Working hours')));
    expect(harness.location, '/app/wiki/page-1');
    expect(
      find.textContaining('We work from Shanghai.', findRichText: true),
      findsOneWidget,
    );

    await tapVisible(tester, find.byType(BackButton));
    expect(find.byType(MemoryDetailPage), findsOneWidget);
    expect(_inDetail(find.text('Use Shanghai timezone')), findsOneWidget);
  });

  testWidgets('a memory shows as it is now, never the list\'s older copy', (
    tester,
  ) async {
    final server = FakeKnowledgeServer();
    server.handlers['GET /api/memories/memory-1'] = (_) => {
      ...memoryJson('memory-1', 'Use Beijing timezone', revision: 4),
      'body_available': true,
    };
    await mountKnowledge(tester, server);
    await _openDetail(tester);

    expect(_inDetail(find.text('Use Beijing timezone')), findsOneWidget);
    expect(_inDetail(find.text('Use Shanghai timezone')), findsNothing);

    // An edit from here names the revision now on screen.
    await tapVisible(tester, find.byKey(const ValueKey('memory-detail-edit')));
    await tester.enterText(
      find.byKey(const ValueKey('memory-editor-text')),
      'Use UTC',
    );
    await tapVisible(tester, find.byKey(const ValueKey('memory-editor-save')));
    expect(lastBody(server, '/api/memories/memory-1')['expected_revision'], 4);
  });

  testWidgets('no text shows when the current read no longer allows it', (
    tester,
  ) async {
    final server = FakeKnowledgeServer();
    server.handlers['GET /api/memories/memory-1'] = (_) => {
      ...memoryJson('memory-1', '', revision: 3),
      'body_available': false,
    };
    await mountKnowledge(tester, server);
    await _openDetail(tester);

    expect(_inDetail(find.text('内容已无法查看。')), findsOneWidget);
    expect(_inDetail(find.text('Use Shanghai timezone')), findsNothing);
    expect(_inDetail(find.text('Original immutable evidence')), findsNothing);
    final edit = tester.widget<InkWell>(
      find.descendant(
        of: find.byKey(const ValueKey('memory-detail-edit')),
        matching: find.byType(InkWell),
      ),
    );
    expect(edit.onTap, isNull);
  });

  testWidgets(
    'a corrected memory is explained in the person\'s own words, replaced wording last',
    (tester) async {
      final server = FakeKnowledgeServer()
        ..sources = [
          {
            'id': 'said-first',
            'source_kind': 'user_statement',
            'body': null,
            'body_available': false,
            'superseded': true,
          },
          {
            'id': 'corrected',
            'source_kind': 'verified_memory_revision',
            'body': 'Corrected record',
            'body_available': true,
            'superseded': false,
            'changes': [
              {
                'body': 'Actually, Shanghai rather than Tokyo',
                'session_id': 'session-2',
              },
            ],
          },
        ];
      await mountKnowledge(tester, server);
      await _openDetail(tester);

      expect(
        _inDetail(find.text('Actually, Shanghai rather than Tokyo')),
        findsOneWidget,
      );
      expect(_inDetail(find.text('你的更正')), findsOneWidget);
      expect(_inDetail(find.text('这段原话已无法查看。')), findsNothing);
      final current = tester
          .getTopLeft(_inDetail(find.text('Corrected record')))
          .dy;
      final replaced = tester
          .getTopLeft(_inDetail(find.text('这段原话已被你后来的更正取代。')))
          .dy;
      expect(replaced, greaterThan(current));
    },
  );

  testWidgets(
    'an edit sends the shown revision and keeps the text on a conflict',
    (tester) async {
      final server = FakeKnowledgeServer();
      server.handlers['PATCH /api/memories/memory-1'] = (_) =>
          throw const FakeHttpError(409, {
            'detail': {
              'code': 'MEMORY_REVISION_CONFLICT',
              'message': 'changed',
            },
          });
      await mountKnowledge(tester, server);

      await tapVisible(
        tester,
        find.byKey(const ValueKey('memory-edit-memory-1')),
      );
      expect(find.text('编辑记忆'), findsOneWidget);
      await tester.enterText(
        find.byKey(const ValueKey('memory-editor-text')),
        'Use UTC instead',
      );
      await tapVisible(
        tester,
        find.byKey(const ValueKey('memory-editor-save')),
      );

      final body = lastBody(server, '/api/memories/memory-1');
      expect(body['summary'], 'Use UTC instead');
      expect(body['expected_revision'], 3);
      expect(body['request_id'], isA<String>());
      expect(find.textContaining('这条记忆刚刚在别处更新过'), findsOneWidget);
      expect(find.text('Use UTC instead'), findsOneWidget);
      expect(find.text('编辑记忆'), findsOneWidget);
    },
  );

  testWidgets(
    'a lost answer is retried as the same command, a refusal is not',
    (tester) async {
      final server = FakeKnowledgeServer();
      var replies = <Object>[
        const FakeHttpError(0),
        const FakeHttpError(422, {'detail': 'nope'}),
        memoryJson('memory-1', 'Use UTC', revision: 4),
      ];
      server.handlers['PATCH /api/memories/memory-1'] = (_) {
        final reply = replies.first;
        replies = replies.sublist(1);
        if (reply is FakeHttpError) throw reply;
        return reply;
      };
      final harness = await mountKnowledge(tester, server);
      await tapVisible(
        tester,
        find.byKey(const ValueKey('memory-edit-memory-1')),
      );
      await tester.enterText(
        find.byKey(const ValueKey('memory-editor-text')),
        'Use UTC',
      );
      final save = find.byKey(const ValueKey('memory-editor-save'));
      for (var i = 0; i < 3; i++) {
        await tapVisible(tester, save);
      }
      final ids = [
        for (final r in server.requests.where((r) => r.method == 'PATCH'))
          (r.data as Map)['request_id'],
      ];
      expect(ids, hasLength(3));
      expect(ids[1], ids[0]);
      expect(ids[2], isNot(ids[1]));
      expect(find.text('编辑记忆'), findsNothing);
      expect(harness.toasts, contains('已保存。相关主题会在后台自动更新。'));
    },
  );

  testWidgets(
    'a memory is added to the scope the person picks, plain text only',
    (tester) async {
      final server = FakeKnowledgeServer();
      server.handlers['POST /api/memories'] = (_) =>
          memoryJson('memory-3', 'Meetings are on Thursday.');
      final harness = await mountKnowledge(
        tester,
        server,
        location: '/app/wiki?project=p1',
      );

      await tapVisible(tester, find.byKey(const ValueKey('knowledge-add')));
      expect(find.text('写下希望助手长期记住的偏好、约定或事实，之后的对话中它会参考这些信息。'), findsOneWidget);
      // Saving starts in the scope in view; the person moves it to personal.
      final scope = find.byKey(const ValueKey('memory-editor-scope'));
      expect(
        find.descendant(of: scope, matching: find.text('Project One')),
        findsOneWidget,
      );
      await tapVisible(tester, scope);
      await tapVisible(tester, find.text('个人').last);
      await tester.enterText(
        find.byKey(const ValueKey('memory-editor-text')),
        'Meetings are on Thursday.',
      );
      await settle(tester);
      expect(find.text('25 / 2000'), findsOneWidget);
      await tapVisible(
        tester,
        find.byKey(const ValueKey('memory-editor-save')),
      );

      final body = lastBody(server, '/api/memories');
      expect(body['summary'], 'Meetings are on Thursday.');
      expect(body.containsKey('project_id'), isTrue);
      expect(body['project_id'], isNull);
      expect(body['request_id'], isA<String>());
      expect(server.writes(), hasLength(1));
      expect(find.text('添加记忆'), findsOneWidget); // only the header button
      expect(harness.toasts, contains('已添加。助手之后会参考这条信息。'));
    },
  );

  testWidgets(
    'an ID, card or phone number is refused with the reason, text kept',
    (tester) async {
      final server = FakeKnowledgeServer();
      server.handlers['POST /api/memories'] = (_) =>
          throw const FakeHttpError(422, {
            'detail': {
              'code': 'MEMORY_SENSITIVE_CONTENT',
              'message': 'memory_sensitive_content',
            },
          });
      await mountKnowledge(tester, server);

      await tapVisible(tester, find.byKey(const ValueKey('knowledge-add')));
      await tester.enterText(
        find.byKey(const ValueKey('memory-editor-text')),
        'My ID number is 110101199003071234',
      );
      await tapVisible(
        tester,
        find.byKey(const ValueKey('memory-editor-save')),
      );
      expect(find.textContaining('为了你的安全，记忆里不保存密码'), findsOneWidget);
      expect(find.text('My ID number is 110101199003071234'), findsOneWidget);
    },
  );

  testWidgets(
    'forgetting takes only the memory unless its original wording is ticked too',
    (tester) async {
      final server = FakeKnowledgeServer();
      await mountKnowledge(tester, server);

      await tapVisible(
        tester,
        find.byKey(const ValueKey('memory-forget-memory-1')),
      );
      expect(find.text('忘记这条记忆？'), findsOneWidget);
      expect(find.text('同时清除助手保存的原话（1 条）'), findsOneWidget);
      await tapVisible(tester, find.byKey(const ValueKey('forget-confirm')));
      expect(lastBody(server, '/api/memories/memory-1/forget'), {
        'expected_revision': 3,
        'request_id': isA<String>(),
        'mode': 'memory',
        'source_ids': <String>[],
      });
      expect(find.text('忘记这条记忆？'), findsNothing);

      await tapVisible(
        tester,
        find.byKey(const ValueKey('memory-forget-memory-1')),
      );
      await tapVisible(
        tester,
        find.byKey(const ValueKey('forget-clear-sources')),
      );
      expect(find.text('基于同一段原话的其他记忆也会一起忘记。'), findsOneWidget);
      await tapVisible(tester, find.byKey(const ValueKey('forget-confirm')));
      final second = lastBody(server, '/api/memories/memory-1/forget');
      expect(second['mode'], 'sources');
      expect(second['source_ids'], ['snapshot-1']);
      // Never a chat, session or project deletion — only the memory API.
      expect(
        server.writes().every((r) => r.path.startsWith('/api/memories/')),
        isTrue,
      );
    },
  );

  testWidgets('cancelling the forget dialog writes nothing', (tester) async {
    final server = FakeKnowledgeServer();
    await mountKnowledge(tester, server);
    await tapVisible(
      tester,
      find.byKey(const ValueKey('memory-forget-memory-1')),
    );
    await tapVisible(tester, find.text('取消'));
    expect(find.text('忘记这条记忆？'), findsNothing);
    expect(server.writes(), isEmpty);
  });

  testWidgets(
    'once forgotten from its page no old text shows, even when a slow read lands later',
    (tester) async {
      final server = FakeKnowledgeServer();
      final lateSources = Completer<Object?>();
      var forgotten = false;
      server.handlers['GET /api/memories/memory-1/sources'] = (_) =>
          forgotten ? lateSources.future : {'sources': server.sources};
      server.handlers['GET /api/memories/memory-1/cleanup'] = (_) => forgotten
          ? {'status': server.cleanup['status'], 'stopped': true}
          : {'status': 'active', 'stopped': false};
      server.handlers['POST /api/memories/memory-1/forget'] = (_) {
        forgotten = true;
        server.cleanup = {'status': 'stopped_cleanup_pending'};
        return {'status': 'stopped_cleanup_pending'};
      };
      await mountKnowledge(tester, server);
      await _openDetail(tester);
      expect(
        _inDetail(find.text('Original immutable evidence')),
        findsOneWidget,
      );

      await tapVisible(
        tester,
        find.byKey(const ValueKey('memory-detail-forget')),
      );
      await tapVisible(tester, find.byKey(const ValueKey('forget-confirm')));

      expect(_inDetail(find.text('这条记忆已忘记')), findsOneWidget);
      expect(_inDetail(find.text('助手已停止使用这条信息，相关数据正在后台清除。')), findsOneWidget);
      void noOldText() {
        expect(find.text('Use Shanghai timezone'), findsNothing);
        expect(find.text('Original immutable evidence'), findsNothing);
        expect(find.text('Earlier value'), findsNothing);
      }

      noOldText();
      lateSources.complete({'sources': server.sources});
      await settle(tester);
      noOldText();
      expect(find.byKey(const ValueKey('memory-detail-edit')), findsNothing);

      server.cleanup = {'status': 'cleaned'};
      await tapVisible(
        tester,
        find.byKey(const ValueKey('memory-detail-check')),
      );
      expect(_inDetail(find.text('相关数据已全部清除。')), findsOneWidget);
      noOldText();
      expect(server.writes(), hasLength(1));
    },
  );

  testWidgets('a memory the server reports stopped never shows its text', (
    tester,
  ) async {
    final server = FakeKnowledgeServer()
      ..cleanup = {'status': 'cleaned', 'stopped': true};
    await mountKnowledge(tester, server);
    await _openDetail(tester);

    expect(_inDetail(find.text('相关数据已全部清除。')), findsOneWidget);
    expect(_inDetail(find.text('Use Shanghai timezone')), findsNothing);
    expect(_inDetail(find.text('Original immutable evidence')), findsNothing);
    expect(server.writes(), isEmpty);
  });
}
