import 'dart:convert';

import 'package:bossip_mobile/features/memory/wiki/topic_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'knowledge_fixture.dart';

Map<String, dynamic> _page({
  String id = 'p1',
  String status = 'published',
  bool bodyAvailable = true,
  String? body,
  Map<String, dynamic>? document,
  List<Map<String, dynamic>>? sourceDetails,
}) => {
  'id': id,
  'slug': 'guide',
  'title': 'Guide',
  'project_id': 'project',
  'revision': 2,
  'content_hash': 'a' * 64,
  'status': status,
  'body_available': bodyAvailable,
  'body':
      body ??
      '# Guide\n\n## Working together\n\nUse **weekly** check-ins.\n'
          '[source:s1@2]\n\n[[decisions|Decision log]]\n\n`[[decisions]]`\n\n'
          '<script>alert(1)</script>\n\n'
          '![hidden](https://untrusted.test/track)',
  'paragraphs': bodyAvailable
      ? [
          {
            'text': 'Use weekly check-ins.',
            'citations': [
              {
                'source_id': 's1',
                'revision': 2,
                'quote': 'We meet every week.',
              },
            ],
          },
        ]
      : <Object>[],
  'sources': [
    {'id': 's1', 'revision': 2},
  ],
  'source_details':
      sourceDetails ??
      [
        {
          'id': 's1',
          'revision': 2,
          'body': 'We meet every week. Decisions are documented.',
          'session_id': null,
          'kind': 'user_note',
          'created_at': '2026-10-02T00:00:00Z',
        },
      ],
  'document': ?document,
  'updated_at': '2026-10-02T09:00:00Z',
};

FakeKnowledgeServer _server(Map<String, dynamic> page) {
  final server = FakeKnowledgeServer()
    ..pages = [
      topicJson('p2', 'Decisions', slug: 'decisions', projectId: 'project'),
    ];
  server.handlers['GET /api/memory-wiki/pages/${page['id']}'] = (_) => page;
  return server;
}

Finder _rich(String text) => find.textContaining(text, findRichText: true);

Finder _inSheet(Finder finder) =>
    find.descendant(of: find.byType(BottomSheet), matching: finder);

void main() {
  setUpKnowledgeTests();

  testWidgets(
    'renders Markdown, scope-resolved topic links and citations, never raw HTML or remote images',
    (tester) async {
      final semantics = tester.ensureSemantics();
      final server = _server(_page());
      final harness = await mountKnowledge(
        tester,
        server,
        location: '/app/wiki/p1?project=project',
      );

      expect(find.byType(TopicPage), findsOneWidget);
      expect(find.text('Guide'), findsOneWidget);
      expect(_rich('Working together'), findsWidgets);
      expect(_rich('weekly'), findsWidgets);
      // Code stays literal; the marker inside it is not a link.
      expect(_rich('[[decisions]]'), findsOneWidget);
      expect(_rich('alert(1)'), findsNothing);
      expect(_rich('<script>'), findsNothing);
      expect(_rich('[source:'), findsNothing);
      expect(find.byType(Image), findsNothing);
      expect(find.bySemanticsLabel('查看引用 1'), findsOneWidget);
      expect(server.writes(), isEmpty);

      await tapVisible(tester, find.text('Decision log'));
      expect(harness.location, '/app/wiki/p2?project=project');
      semantics.dispose();
    },
  );

  testWidgets(
    'a citation opens what it quotes, with the full original on request',
    (tester) async {
      final semantics = tester.ensureSemantics();
      final server = _server(_page());
      await mountKnowledge(
        tester,
        server,
        location: '/app/wiki/p1?project=project',
      );

      await tapVisible(tester, find.bySemanticsLabel('查看引用 1'));
      expect(find.byType(BottomSheet), findsOneWidget);
      expect(_inSheet(find.text('We meet every week.')), findsOneWidget);
      expect(_inSheet(find.text('原始资料')), findsOneWidget);
      expect(
        _inSheet(find.text('We meet every week. Decisions are documented.')),
        findsNothing,
      );
      await tapVisible(tester, _inSheet(find.text('查看完整原文')));
      expect(
        _inSheet(find.text('We meet every week. Decisions are documented.')),
        findsOneWidget,
      );
      expect(server.writes(), isEmpty);
      semantics.dispose();
    },
  );

  testWidgets('related topics and the evidence follow the text', (
    tester,
  ) async {
    final server = _server(_page());
    final harness = await mountKnowledge(
      tester,
      server,
      location: '/app/wiki/p1?project=project',
    );

    await tester.scrollUntilVisible(
      find.text('点击正文里的编号，可以看到对应的原文。'),
      300,
      scrollable: find.byType(Scrollable).first,
    );
    expect(find.text('相关主题'), findsOneWidget);
    expect(find.text('We meet every week.'), findsOneWidget);
    await tapVisible(
      tester,
      find.byKey(const ValueKey('wiki-evidence-0')).first,
    );
    await tapVisible(tester, find.text('查看完整原文'));
    expect(
      find.text('We meet every week. Decisions are documented.'),
      findsOneWidget,
    );

    await tapVisible(tester, find.text('Decisions'));
    expect(harness.location, '/app/wiki/p2?project=project');
  });

  testWidgets(
    'a correction is told apart from the original and links its conversation',
    (tester) async {
      final server = _server(
        _page(
          sourceDetails: [
            {
              'id': 's1',
              'revision': 2,
              'body': 'We meet on Tuesdays.',
              'session_id': null,
              'kind': 'verified_memory_revision',
              'created_at': '2026-10-02T00:00:00Z',
              'changes': [
                {
                  'body': 'Starting next month we meet on Tuesdays.',
                  'session_id': 'correction-session',
                },
              ],
            },
          ],
        ),
      );
      final harness = await mountKnowledge(
        tester,
        server,
        location: '/app/wiki/p1?project=project',
      );
      final evidence = find.byKey(const ValueKey('wiki-evidence-0'));
      await tester.scrollUntilVisible(
        evidence,
        300,
        scrollable: find.byType(Scrollable).first,
      );
      expect(
        find.descendant(of: evidence, matching: find.text('更正后的记录')),
        findsOneWidget,
      );
      await tapVisible(
        tester,
        find.descendant(of: evidence, matching: find.text('更正后的记录')),
      );
      expect(
        find.text('Starting next month we meet on Tuesdays.'),
        findsOneWidget,
      );
      await tapVisible(tester, find.text('打开对话'));
      expect(harness.location, '/app/s/correction-session');
    },
  );

  testWidgets('a page that is gone says so plainly and is asked for once', (
    tester,
  ) async {
    final server = FakeKnowledgeServer();
    server.handlers['GET /api/memory-wiki/pages/gone'] = (_) =>
        throw const FakeHttpError(404, {'detail': 'wiki object not found'});
    final harness = await mountKnowledge(
      tester,
      server,
      location: '/app/wiki/gone?project=project',
    );

    expect(find.text('这篇内容已经不在了'), findsOneWidget);
    expect(find.text('重试'), findsNothing);
    await tester.pump(const Duration(seconds: 25));
    expect(
      server.calls.where((c) => c == 'GET /api/memory-wiki/pages/gone'),
      hasLength(1),
    );
    await tapVisible(tester, find.text('回到知识库'));
    expect(harness.location, '/app/wiki?project=project');
  });

  testWidgets('a page being rebuilt withholds its text, evidence and export', (
    tester,
  ) async {
    final server = _server(_page(status: 'stale', bodyAvailable: false));
    await mountKnowledge(
      tester,
      server,
      location: '/app/wiki/p1?project=project',
    );

    expect(
      find.descendant(
        of: find.byKey(const ValueKey('topic-unavailable')),
        matching: find.text('正在更新'),
      ),
      findsOneWidget,
    );
    expect(find.textContaining('这页依赖的信息有变化'), findsOneWidget);
    expect(_rich('Working together'), findsNothing);
    expect(find.text('来源'), findsNothing);
    final export = tester.widget<InkWell>(
      find.descendant(
        of: find.byKey(const ValueKey('topic-export')),
        matching: find.byType(InkWell),
      ),
    );
    expect(export.onTap, isNull);
  });

  testWidgets('a retired topic is explained, with nothing to edit or export', (
    tester,
  ) async {
    final server = _server(_page(status: 'retired', bodyAvailable: false));
    await mountKnowledge(
      tester,
      server,
      location: '/app/wiki/p1?project=project',
    );

    expect(find.text('这个主题不再单独显示'), findsOneWidget);
    expect(find.text('正在更新'), findsNothing);
    expect(find.byKey(const ValueKey('topic-edit')), findsNothing);
    expect(find.byKey(const ValueKey('topic-export')), findsNothing);
  });

  testWidgets('a page is exported as Markdown with what it cites', (
    tester,
  ) async {
    final server = _server(_page());
    final harness = await mountKnowledge(
      tester,
      server,
      location: '/app/wiki/p1?project=project',
    );
    await tapVisible(tester, find.byKey(const ValueKey('topic-export')));

    final saved = harness.downloads.saved.single;
    expect(saved.name, 'guide.md');
    expect(saved.mimeType, 'text/markdown');
    expect(
      utf8.decode(saved.bytes),
      contains('[source:s1@2]\n\n> We meet every week.'),
    );
  });

  testWidgets('a document page names its file, its sections and its original', (
    tester,
  ) async {
    final server = _server(
      _page(
        document: {
          'id': 'doc-1',
          'filename': 'venue-guide.pdf',
          'sections': [
            {'id': 'p1', 'title': 'Arrival'},
            {'id': 'p3', 'title': 'Schedule'},
          ],
          'warnings': <Object>[],
        },
      ),
    );
    server.handlers['GET /api/memory-documents/doc-1/original'] = (_) => [
      1,
      2,
      3,
    ];
    final harness = await mountKnowledge(
      tester,
      server,
      location: '/app/wiki/p1?project=project',
    );

    expect(find.text('venue-guide.pdf'), findsOneWidget);
    expect(find.text('Arrival'), findsOneWidget);
    await tapVisible(tester, find.byKey(const ValueKey('topic-download')));
    expect(harness.downloads.saved.single.name, 'venue-guide.pdf');

    await tapVisible(tester, find.text('Schedule'));
    expect(harness.location, '/app/wiki/p3?project=project');
  });

  testWidgets(
    'an edit saves the versions first shown and keeps the text on a conflict',
    (tester) async {
      final server = _server(_page());
      server.handlers['GET /api/memory-wiki/pages/p1/edit'] = (_) => {
        'id': 'p1',
        'revision': 3,
        'content_hash': 'b' * 64,
        'title': 'My guide',
        'entries': [
          {
            'id': 'm1',
            'revision': 7,
            'text': 'Original preference',
            'max_length': 2000,
          },
        ],
      };
      server.handlers['POST /api/memory-wiki/pages/p1/edit'] = (_) =>
          throw const FakeHttpError(409, {
            'detail': {'code': 'WIKI_EDIT_CHANGED', 'message': 'changed'},
          });
      await mountKnowledge(
        tester,
        server,
        location: '/app/wiki/p1?project=project',
      );

      await tapVisible(tester, find.byKey(const ValueKey('topic-edit')));
      expect(find.text('用自己的话修改即可，保存后会自动更新相关知识。'), findsOneWidget);
      final save = find.byKey(const ValueKey('wiki-editor-save'));
      await tester.enterText(
        find.byKey(const ValueKey('wiki-editor-entry-0')),
        'Only on weekends',
      );
      await tapVisible(tester, save);

      final body = lastBody(server, '/api/memory-wiki/pages/p1/edit');
      expect(body['expected_revision'], 3);
      expect(body['content_hash'], 'b' * 64);
      expect(body['title'], 'My guide');
      expect(body['entries'], [
        {'id': 'm1', 'revision': 7, 'text': 'Only on weekends'},
      ]);
      expect(body['request_id'], isA<String>());
      expect(find.textContaining('内容刚刚在别处更新过'), findsOneWidget);
      expect(find.text('Only on weekends'), findsOneWidget);
    },
  );

  testWidgets('a saved edit closes the editor and says so', (tester) async {
    final server = _server(_page());
    server.handlers['GET /api/memory-wiki/pages/p1/edit'] = (_) => {
      'id': 'p1',
      'revision': 3,
      'content_hash': 'b' * 64,
      'title': 'My guide',
      'entries': [
        {'id': 'm1', 'revision': 7, 'text': 'Original', 'max_length': 2000},
      ],
    };
    server.handlers['POST /api/memory-wiki/pages/p1/edit'] = (_) => {
      'id': 'p1',
      'status': 'updating',
    };
    final harness = await mountKnowledge(
      tester,
      server,
      location: '/app/wiki/p1?project=project',
    );
    await tapVisible(tester, find.byKey(const ValueKey('topic-edit')));
    final save = find.byKey(const ValueKey('wiki-editor-save'));
    // Nothing changed yet: nothing to save.
    expect(
      tester
          .widget<InkWell>(
            find.descendant(of: save, matching: find.byType(InkWell)),
          )
          .onTap,
      isNull,
    );
    await tester.enterText(
      find.byKey(const ValueKey('wiki-editor-title')),
      'Our guide',
    );
    await tapVisible(tester, save);

    expect(find.byKey(const ValueKey('wiki-editor-save')), findsNothing);
    expect(harness.toasts, contains('已保存。相关内容会在后台自动更新。'));
  });
}
