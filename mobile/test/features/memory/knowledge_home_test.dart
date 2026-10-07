import 'package:bossip_mobile/features/memory/knowledge_screen.dart';
import 'package:bossip_mobile/features/memory/widgets/knowledge_controls.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'knowledge_fixture.dart';

String _tabView(WidgetTester tester) =>
    tester.widget<KnowledgeTabs>(find.byType(KnowledgeTabs)).view;

Map<String, String?> _counts(WidgetTester tester) =>
    tester.widget<KnowledgeTabs>(find.byType(KnowledgeTabs)).counts;

void main() {
  setUpKnowledgeTests();

  testWidgets(
    'shows memories, topics and files in one place, with counts and no writes',
    (tester) async {
      final server = FakeKnowledgeServer();
      await mountKnowledge(tester, server);

      expect(find.text('知识库'), findsOneWidget);
      expect(find.text('记忆、主题与文件'), findsOneWidget);
      for (final label in ['管理', '上传文件', '添加记忆']) {
        expect(find.text(label), findsOneWidget);
      }
      expect(find.text('搜索记忆、主题和文件'), findsOneWidget);
      expect(find.text('全部内容'), findsOneWidget);
      expect(_counts(tester), {'memories': '2', 'topics': '1', 'files': '1'});

      expect(find.text('Use Shanghai timezone'), findsOneWidget);
      expect(find.text('Weekly report goes out on Friday'), findsOneWidget);
      // Scope label on each personal memory, and the topic it belongs to.
      expect(find.text('个人'), findsNWidgets(2));
      expect(find.text('Working hours'), findsNWidgets(2));
      // A document's own page is reached through its file, not as a topic.
      expect(find.text('Venue guide'), findsNothing);
      // Markdown and citation markers never leak into a card preview.
      expect(find.text('Hours We work from Shanghai.'), findsOneWidget);
      await tester.ensureVisible(find.text('venue-guide.pdf'));
      expect(find.text('venue-guide.pdf'), findsOneWidget);
      expect(find.text('可以阅读'), findsOneWidget);

      // Opening the page reads; nothing is written or searched by a model.
      expect(server.writes(), isEmpty);
      expect(server.calls.where((c) => c.contains('/search')), isEmpty);
    },
  );

  testWidgets('the tabs switch between the four views', (tester) async {
    final server = FakeKnowledgeServer();
    await mountKnowledge(tester, server);

    await tapVisible(tester, find.text('记忆'));
    expect(_tabView(tester), 'memories');
    expect(find.text('助手会在之后的对话中参考这些信息。说得不对可以编辑，不想让它记住可以忘记。'), findsOneWidget);
    expect(find.text('Weekly report goes out on Friday'), findsOneWidget);
    expect(find.text('venue-guide.pdf'), findsNothing);

    await tapVisible(tester, find.text('主题'));
    expect(find.text('相关的记忆会自动整理成主题，方便集中阅读。'), findsOneWidget);
    expect(find.text('1 个来源 · ${_since(tester)}'), findsOneWidget);

    await tapVisible(tester, find.text('文件'));
    expect(find.text('上传的文件会自动整理，助手回答问题时会参考其中的内容。'), findsOneWidget);
    expect(find.text('venue-guide.pdf'), findsOneWidget);
    expect(find.text('Use Shanghai timezone'), findsNothing);

    await tapVisible(tester, find.text('全部'));
    expect(_tabView(tester), 'overview');
  });

  for (final retired in ['reviews', 'workflows', 'concepts', 'graph']) {
    testWidgets('the retired $retired link opens the plain overview', (
      tester,
    ) async {
      final server = FakeKnowledgeServer();
      await mountKnowledge(tester, server, location: '/app/wiki?view=$retired');
      expect(_tabView(tester), 'overview');
      expect(find.text('Use Shanghai timezone'), findsOneWidget);
      expect(server.writes(), isEmpty);
    });
  }

  testWidgets('/app/memory lands on the memories of the same scope', (
    tester,
  ) async {
    final server = FakeKnowledgeServer();
    final harness = await mountKnowledge(
      tester,
      server,
      location: '/app/memory?project=p1',
    );
    expect(harness.location, '/app/wiki?project=p1&view=memories');
    expect(_tabView(tester), 'memories');
    expect(
      server.calls,
      contains('GET /api/memories?limit=100&status=ACTIVE&project_id=p1'),
    );
    expect(find.text('Project One'), findsOneWidget);
  });

  testWidgets('search narrows memories here and asks the server for topics', (
    tester,
  ) async {
    final server = FakeKnowledgeServer();
    await mountKnowledge(tester, server);

    await tester.enterText(
      find.byKey(const ValueKey('knowledge-search')),
      'shanghai',
    );
    await settle(tester);

    expect(
      server.calls,
      contains('GET /api/memories?limit=100&status=ACTIVE&query=shanghai'),
    );
    expect(
      server.calls,
      contains(
        'GET /api/memory-wiki/library?query=shanghai&status=all&offset=0',
      ),
    );
    expect(find.text('Weekly report goes out on Friday'), findsNothing);
    // The hit is marked inside the sentence.
    final text = tester.widget<Text>(find.text('Use Shanghai timezone'));
    final marked = text.textSpan!.toPlainText();
    expect(marked, 'Use Shanghai timezone');
    final spans = (text.textSpan! as TextSpan).children!.cast<TextSpan>();
    expect(
      spans.where((s) => s.style?.backgroundColor != null).single.text,
      'Shanghai',
    );
    expect(server.writes(), isEmpty);

    // A search with nothing anywhere says so, and offers the assistant.
    await tester.enterText(
      find.byKey(const ValueKey('knowledge-search')),
      'zzz',
    );
    await settle(tester);
    expect(find.text('没有找到与「zzz」相关的内容'), findsOneWidget);
    expect(find.text('去问助手'), findsOneWidget);

    await tester.tap(find.byKey(const ValueKey('knowledge-search-clear')));
    await settle(tester);
    expect(find.text('Weekly report goes out on Friday'), findsOneWidget);
  });

  testWidgets('the scope filter narrows everything to one project', (
    tester,
  ) async {
    final server = FakeKnowledgeServer();
    await mountKnowledge(tester, server);

    await tester.tap(find.byKey(const ValueKey('knowledge-scope')));
    await settle(tester);
    await tester.tap(find.text('Project Two').last);
    await settle(tester);

    expect(find.text('Project Two'), findsOneWidget);
    expect(
      server.calls,
      containsAll([
        'GET /api/memories?limit=100&status=ACTIVE&project_id=p2',
        'GET /api/memory-wiki/library?query=&status=all&offset=0&project_id=p2',
        'GET /api/memory-documents?offset=0&project_id=p2',
        'GET /api/memories/processing?project_id=p2',
        'GET /api/memory-wiki/memory-groups?project_id=p2',
      ]),
    );
  });

  testWidgets('older memories are paged in from the server', (tester) async {
    final server = FakeKnowledgeServer();
    server.handlers['GET /api/memories'] = (request) =>
        request.queryParameters['offset'] == '100'
        ? {
            'memories': [server.memories[1]],
            'next_offset': null,
          }
        : {
            'memories': [server.memories[0]],
            'next_offset': 100,
          };
    await mountKnowledge(tester, server, location: '/app/wiki?view=memories');

    expect(_counts(tester)['memories'], '1+');
    expect(find.text('Weekly report goes out on Friday'), findsNothing);
    await tapVisible(tester, find.text('加载更多'));
    expect(find.text('Weekly report goes out on Friday'), findsOneWidget);
    expect(
      server.calls,
      contains('GET /api/memories?limit=100&status=ACTIVE&offset=100'),
    );
    expect(find.text('加载更多'), findsNothing);
    expect(_counts(tester)['memories'], '2');
  });

  testWidgets(
    'what could not be saved shows in the person\'s words, retried or dismissed one by one',
    (tester) async {
      final server = FakeKnowledgeServer()
        ..processing = {
          'pending': 1,
          'failed': [
            {
              'id': 'job-1',
              'session_id': 'chat-1',
              'session_title': 'Weekly plans',
              'excerpt': 'I take guitar on Wednesdays.',
              'failed_at': '2026-10-02T15:25:29Z',
            },
          ],
        };
      final harness = await mountKnowledge(tester, server);

      expect(find.text('1 段对话正在整理成记忆…'), findsOneWidget);
      expect(find.text('有 1 段话没能记下来'), findsOneWidget);
      await tapVisible(tester, find.text('查看'));
      expect(find.text('“I take guitar on Wednesdays.”'), findsOneWidget);

      await tapVisible(tester, find.text('重试'));
      expect(
        server.calls,
        contains('POST /api/memories/processing/job-1/retry'),
      );
      await tapVisible(tester, find.text('不用了'));
      expect(
        server.calls,
        contains('POST /api/memories/processing/job-1/dismiss'),
      );
      expect(server.writes(), hasLength(2));
      expect(harness.toasts, contains('好的，这段话不会再提醒。'));

      await tapVisible(tester, find.text('Weekly plans'));
      expect(harness.location, '/app/s/chat-1');
    },
  );

  testWidgets('automatic saving is turned off from the manage sheet', (
    tester,
  ) async {
    final server = FakeKnowledgeServer();
    server.handlers['PUT /api/memories/settings'] = (_) => {
      'auto_save': false,
      'session_paused': false,
    };
    final harness = await mountKnowledge(tester, server);

    await tapVisible(tester, find.byKey(const ValueKey('knowledge-manage')));
    expect(find.text('助手会从对话里记住你的偏好和信息。'), findsOneWidget);
    await tapVisible(tester, find.text('自动记住对话内容'));

    final put = server.requests.singleWhere((r) => r.method == 'PUT');
    expect(put.path, '/api/memories/settings');
    expect(put.data, {'auto_save': false});
    expect(harness.toasts.single, startsWith('已关闭：'));
  });

  testWidgets(
    'everything in view is cleared only after an explicit confirmation',
    (tester) async {
      final server = FakeKnowledgeServer();
      server.handlers['POST /api/memories/forget-all'] = (_) => {
        'forgotten': 2,
      };
      final harness = await mountKnowledge(
        tester,
        server,
        location: '/app/wiki?project=p1',
      );

      await tapVisible(tester, find.byKey(const ValueKey('knowledge-manage')));
      await tapVisible(tester, find.text('清空全部记忆…'));
      expect(find.text('清空全部记忆？'), findsOneWidget);
      expect(find.textContaining('「Project One」里的所有记忆都会被忘记'), findsOneWidget);

      final confirm = find.byKey(const ValueKey('knowledge-clear-confirm'));
      expect(tester.widget<TextButton>(confirm).onPressed, isNull);
      await tapVisible(
        tester,
        find.byKey(const ValueKey('knowledge-clear-understood')),
      );
      expect(tester.widget<TextButton>(confirm).onPressed, isNotNull);
      await tapVisible(tester, confirm);

      expect(lastBody(server, '/api/memories/forget-all'), {
        'project_id': 'p1',
        'confirm': 'forget-all',
      });
      expect(find.text('清空全部记忆？'), findsNothing);
      expect(harness.toasts, contains('已清空 2 条记忆。'));
    },
  );

  testWidgets('everything is exported as one Markdown file', (tester) async {
    final server = FakeKnowledgeServer();
    server.handlers['GET /api/memories/export'] = (_) => '# 我的记忆\n'.codeUnits;
    final harness = await mountKnowledge(tester, server);

    await tapVisible(tester, find.byKey(const ValueKey('knowledge-manage')));
    await tapVisible(tester, find.text('导出全部记忆'));

    expect(server.calls, contains('GET /api/memories/export?lang=zh-CN'));
    expect(harness.downloads.saved.single.name, '我的记忆.md');
    expect(harness.downloads.saved.single.mimeType, 'text/markdown');
  });

  testWidgets(
    'someone with nothing saved yet is welcomed with the ways to start',
    (tester) async {
      final server = FakeKnowledgeServer()
        ..memories = []
        ..pages = []
        ..documents = [];
      final harness = await mountKnowledge(tester, server);

      expect(find.text('你的知识库还是空的'), findsOneWidget);
      expect(find.text('聊天时自动记住'), findsOneWidget);
      await tapVisible(tester, find.text('去聊天'));
      expect(harness.location, '/app');
    },
  );

  testWidgets('a failed read says so and can be retried', (tester) async {
    final server = FakeKnowledgeServer();
    var fail = true;
    server.handlers['GET /api/memory-documents'] = (_) {
      if (fail) throw const FakeHttpError(502, {'detail': 'upstream'});
      return {'documents': <Object>[], 'next_offset': null};
    };
    await mountKnowledge(tester, server);

    expect(find.byKey(const ValueKey('knowledge-load-error')), findsOneWidget);
    expect(find.textContaining('有些内容暂时没有加载出来。'), findsOneWidget);
    fail = false;
    await tapVisible(tester, find.text('重试'));
    expect(find.byKey(const ValueKey('knowledge-load-error')), findsNothing);
    expect(find.byType(KnowledgeScreen), findsOneWidget);
  });
}

/// The relative time the fixture's topic shows, as the screen formats it.
String _since(WidgetTester tester) {
  final footer = tester
      .widgetList<Text>(find.textContaining('1 个来源 · '))
      .first
      .data!;
  return footer.substring('1 个来源 · '.length);
}
