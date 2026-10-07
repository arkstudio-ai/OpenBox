import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'knowledge_fixture.dart';

const _long =
    '每周一上午九点半在三楼大会议室开项目站会，同步上周进度、本周计划和需要协调的风险，'
    '会议纪要由当周值班的同事整理后发到项目群里。';
const _file = '二〇二六年度项目预算说明与执行计划（最终确认版，请勿外传）.docx';
const _title = '项目协作约定、会议安排与周报发送的全部规则（长期有效）';

FakeKnowledgeServer _server() {
  final server = FakeKnowledgeServer()
    ..memories = [
      memoryJson('memory-1', _long, revision: 3, projectId: 'p1'),
      memoryJson('memory-2', 'Weekly report goes out on Friday'),
    ]
    ..pages = [
      topicJson('page-1', _title, excerpt: '## 安排\n\n$_long [source:s1@1]'),
      topicJson('page-2', 'Short', excerpt: 'One line.'),
      topicJson('page-3', 'Being rebuilt', bodyAvailable: false),
    ]
    ..documents = [
      documentJson(
        'doc-1',
        _file,
        status: 'index_failed',
        pageIds: ['doc-page'],
        reasonCode: 'document_processing_failed',
      ),
      documentJson('doc-2', 'notes.md', status: 'parsing'),
    ]
    ..processing = {
      'pending': 3,
      'failed': [
        {
          'id': 'job-1',
          'session_id': 'chat-1',
          'session_title': '一个标题非常非常长的对话，用来检查换行是否正常',
          'excerpt': _long,
          'failed_at': '2026-10-02T15:25:29Z',
        },
      ],
    };
  // The reader: a long title, its file, sections and a citation.
  server.handlers['GET /api/memory-wiki/pages/doc-page'] = (_) => {
    'id': 'doc-page',
    'slug': 'budget',
    'title': _title,
    'status': 'published',
    'body_available': true,
    'body':
        '## 安排\n\n$_long\n[source:s1@1]\n\n| 项目 | 负责人 |\n| --- | --- |\n| 预算 | 小李 |',
    'paragraphs': [
      {
        'text': 'x',
        'citations': [
          {'source_id': 's1', 'revision': 1, 'quote': _long},
        ],
      },
    ],
    'source_details': [
      {
        'id': 's1',
        'revision': 1,
        'kind': 'document_chunk',
        'body': _long,
        'filename': _file,
        'original_pages': [3, 4],
      },
    ],
    'document': {
      'id': 'doc-1',
      'filename': _file,
      'sections': [
        {'id': 'doc-page', 'title': '第一部分：总体安排与说明'},
        {'id': 'doc-page-2', 'title': '第二部分：预算明细与审批流程'},
      ],
    },
    'updated_at': '2026-10-02T09:00:00Z',
  };
  return server;
}

/// Drags the page to its end, then jumps back to the top — without the
/// pull past the top that would start a refresh.
Future<void> _scrollThrough(WidgetTester tester) async {
  final list = find.byType(Scrollable).first;
  for (var i = 0; i < 8; i++) {
    await tester.drag(list, const Offset(0, -400));
    await settle(tester, 4);
  }
  tester.state<ScrollableState>(list).position.jumpTo(0);
  await settle(tester, 4);
}

/// Opens a sheet with [open], checks it laid out, and dismisses it.
Future<void> _sheet(WidgetTester tester, Future<void> Function() open) async {
  await open();
  expect(find.byType(BottomSheet), findsOneWidget);
  expect(tester.takeException(), isNull);
  await closeSheet(tester);
  expect(find.byType(BottomSheet), findsNothing);
}

void _fitsAndReachable(WidgetTester tester, Finder finder, {double? max}) {
  final size = tester.getSize(finder);
  expect(size.height, greaterThanOrEqualTo(44));
  if (max != null) expect(size.height, lessThanOrEqualTo(max));
  expect(tester.getTopLeft(finder).dx, greaterThanOrEqualTo(0));
  expect(tester.getTopRight(finder).dx, lessThanOrEqualTo(320));
}

void main() {
  setUpKnowledgeTests();

  for (final brightness in Brightness.values) {
    for (final scale in [1.0, 1.2]) {
      testWidgets('nothing overflows on a 320pt phone at text scale $scale '
          '(${brightness.name})', (tester) async {
        final server = _server();
        tester.platformDispatcher.textScaleFactorTestValue = scale;
        addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
        await mountKnowledge(tester, server, brightness: brightness);
        tester.view.physicalSize = const Size(320 * 3, 640 * 3);
        await settle(tester);
        expect(tester.takeException(), isNull);

        // The bar's buttons and the tabs are easy to hit.
        for (final key in ['knowledge-add', 'knowledge-manage']) {
          final size = tester.getSize(find.byKey(ValueKey(key)));
          expect(size.width, greaterThanOrEqualTo(44));
          expect(size.height, greaterThanOrEqualTo(44));
        }
        _fitsAndReachable(
          tester,
          find.byKey(const ValueKey('knowledge-tab-files')),
        );

        // One row of each kind: compact, inside the screen, easy to hit.
        final memory = find.byKey(const ValueKey('memory-row-memory-1'));
        await tester.ensureVisible(memory);
        await settle(tester, 2);
        // At most two lines of the memory and its one meta line.
        _fitsAndReachable(tester, memory, max: 29 + 62 * scale);
        final topic = find.byKey(const ValueKey('topic-row-page-1'));
        await tester.ensureVisible(topic);
        await settle(tester, 2);
        // One line of title, one of time and summary.
        _fitsAndReachable(tester, topic, max: 29 + 42 * scale);
        final file = find.byKey(const ValueKey('file-row-doc-1'));
        await tester.ensureVisible(file);
        await settle(tester, 2);
        // One line of name, one of status, size and time.
        _fitsAndReachable(tester, file, max: 29 + 42 * scale);
        _fitsAndReachable(
          tester,
          find.byKey(const ValueKey('file-more-doc-1')),
        );
        expect(tester.takeException(), isNull);

        await _scrollThrough(tester);
        for (final view in ['memories', 'topics', 'files']) {
          await tapVisible(tester, find.byKey(ValueKey('knowledge-tab-$view')));
          await _scrollThrough(tester);
        }
        await tapVisible(
          tester,
          find.byKey(const ValueKey('knowledge-tab-overview')),
        );
        expect(tester.takeException(), isNull);

        // Every sheet the page opens.
        await _sheet(
          tester,
          () =>
              tapVisible(tester, find.byKey(const ValueKey('knowledge-scope'))),
        );
        await _sheet(
          tester,
          () => tapVisible(tester, find.byKey(const ValueKey('knowledge-add'))),
        );
        await _sheet(
          tester,
          () => tapVisible(
            tester,
            find.byKey(const ValueKey('knowledge-manage')),
          ),
        );
        await _sheet(tester, () => tapVisible(tester, find.text('查看')));
        await _sheet(tester, () async {
          await tester.longPress(memory);
          await settle(tester);
        });
        await _sheet(tester, () => fileActions(tester, 'doc-1'));

        // The editor and the forget dialog.
        await memoryAction(tester, 'memory-1', 'edit');
        await tapVisible(tester, find.text('取消'));
        await memoryAction(tester, 'memory-1', 'forget');
        await tapVisible(tester, find.text('取消'));
        expect(tester.takeException(), isNull);

        // A memory in full.
        await tapVisible(tester, find.text(_long));
        await _scrollThrough(tester);
        final history = find.textContaining('修改记录');
        await tester.scrollUntilVisible(
          history,
          200,
          scrollable: find.byType(Scrollable).first,
        );
        await tapVisible(tester, history);
        await _scrollThrough(tester);
        expect(tester.takeException(), isNull);
        await tapVisible(tester, find.byTooltip('关闭'));

        // The reader, and its "more" sheet.
        await fileActions(tester, 'doc-1');
        await tapVisible(tester, find.byKey(const ValueKey('file-read-doc-1')));
        expect(find.textContaining(_file), findsWidgets);
        await _scrollThrough(tester);
        await _sheet(
          tester,
          () => tapVisible(tester, find.byKey(const ValueKey('topic-more'))),
        );
        expect(tester.takeException(), isNull);
        expect(server.writes(), isEmpty);
      });
    }
  }
}
