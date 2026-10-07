import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'knowledge_fixture.dart';

const _long =
    '每周一上午九点半在三楼大会议室开项目站会，同步上周进度、本周计划和需要协调的风险，'
    '会议纪要由当周值班的同事整理后发到项目群里。';

void main() {
  setUpKnowledgeTests();

  for (final scale in [1.0, 1.2]) {
    testWidgets('nothing overflows on a 320pt phone at text scale $scale', (
      tester,
    ) async {
      final server = FakeKnowledgeServer()
        ..memories = [
          memoryJson('memory-1', _long, revision: 3, projectId: 'p1'),
          memoryJson('memory-2', 'Weekly report goes out on Friday'),
        ]
        ..pages = [
          topicJson(
            'page-1',
            '项目协作约定、会议安排与周报发送的全部规则（长期有效）',
            excerpt: '## 安排\n\n$_long [source:s1@1]',
          ),
          topicJson('page-2', 'Short', excerpt: 'One line.'),
          topicJson('page-3', 'Being rebuilt', bodyAvailable: false),
        ]
        ..documents = [
          documentJson(
            'doc-1',
            '二〇二六年度项目预算说明与执行计划（最终确认版，请勿外传）.docx',
            status: 'index_failed',
            pageIds: ['doc-page'],
            reasonCode: 'document_processing_failed',
          ),
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
      tester.platformDispatcher.textScaleFactorTestValue = scale;
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
      await mountKnowledge(tester, server);
      tester.view.physicalSize = const Size(320 * 3, 640 * 3);
      await settle(tester);

      Future<void> scrollThrough() async {
        final list = find.byType(Scrollable).first;
        for (var i = 0; i < 8; i++) {
          await tester.drag(list, const Offset(0, -400));
          await settle(tester, 4);
        }
        await tester.drag(list, const Offset(0, 4000));
        await settle(tester, 4);
      }

      await tapVisible(tester, find.text('查看'));
      await scrollThrough();
      for (final view in ['memories', 'topics', 'files']) {
        await tapVisible(tester, find.byKey(ValueKey('knowledge-tab-$view')));
        await scrollThrough();
      }
      await tapVisible(
        tester,
        find.byKey(const ValueKey('knowledge-tab-overview')),
      );

      await tapVisible(
        tester,
        find.byKey(const ValueKey('memory-edit-memory-1')),
      );
      await tapVisible(tester, find.text('取消'));
      await tapVisible(
        tester,
        find.byKey(const ValueKey('memory-forget-memory-1')),
      );
      await tapVisible(tester, find.text('取消'));
      await tapVisible(tester, find.text(_long));
      await scrollThrough();
      expect(tester.takeException(), isNull);

      // The reader: a long title, its file, sections and actions.
      server.handlers['GET /api/memory-wiki/pages/doc-page'] = (_) => {
        'id': 'doc-page',
        'slug': 'budget',
        'title': '项目协作约定、会议安排与周报发送的全部规则（长期有效）',
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
            'filename': '二〇二六年度项目预算说明与执行计划（最终确认版，请勿外传）.docx',
            'original_pages': [3, 4],
          },
        ],
        'document': {
          'id': 'doc-1',
          'filename': '二〇二六年度项目预算说明与执行计划（最终确认版，请勿外传）.docx',
          'sections': [
            {'id': 'doc-page', 'title': '第一部分：总体安排与说明'},
            {'id': 'doc-page-2', 'title': '第二部分：预算明细与审批流程'},
          ],
        },
      };
      await tapVisible(tester, find.byTooltip('关闭'));
      await tester.drag(find.byType(Scrollable).first, const Offset(0, 4000));
      await settle(tester, 4);
      await tapVisible(
        tester,
        find.byKey(const ValueKey('knowledge-tab-files')),
      );
      await tapVisible(tester, find.byKey(const ValueKey('file-read-doc-1')));
      await scrollThrough();
      expect(tester.takeException(), isNull);
    });
  }
}
