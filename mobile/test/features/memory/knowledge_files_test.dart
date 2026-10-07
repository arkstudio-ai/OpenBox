import 'dart:convert';

import 'package:bossip_mobile/features/memory/state/document_upload.dart';
import 'package:bossip_mobile/features/memory/widgets/knowledge_controls.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'knowledge_fixture.dart';

PickedDocument _file(String name, int size) => PickedDocument(
  name: name,
  size: size,
  read: () async => size > maxDocumentBytes
      ? throw StateError('an oversized file must not be read')
      : utf8.encode('text'),
);

FormData _upload(FakeKnowledgeServer server) =>
    server.requests
            .lastWhere(
              (r) => r.method == 'POST' && r.path == '/api/memory-documents',
            )
            .data
        as FormData;

bool _enabled(WidgetTester tester, Finder button) =>
    tester
        .widget<InkWell>(
          find.descendant(of: button, matching: find.byType(InkWell)).first,
        )
        .onTap !=
    null;

void main() {
  setUpKnowledgeTests();

  testWidgets('each file says how far organizing has come', (tester) async {
    final server = FakeKnowledgeServer()
      ..documents = [
        documentJson('doc-1', 'venue-guide.pdf', pageIds: ['doc-page']),
        documentJson('doc-2', 'notes.md', status: 'indexing'),
        documentJson(
          'doc-3',
          'scan.pdf',
          status: 'index_failed',
          pageIds: ['scan-page'],
        ),
        documentJson(
          'doc-4',
          'photo.pdf',
          status: 'failed',
          reasonCode: 'document_ocr_required',
        ),
      ];
    await mountKnowledge(tester, server, location: '/app/wiki?view=files');

    expect(find.text('可以阅读'), findsOneWidget);
    expect(find.text('可以阅读，正在准备搜索'), findsOneWidget);
    expect(find.text('可以阅读，搜索准备失败'), findsOneWidget);
    expect(find.text('整理失败'), findsOneWidget);
    expect(
      find.text('此 PDF 含扫描图片页，暂时无法读取。请上传带文字层的 PDF 或 Word 文件。'),
      findsOneWidget,
    );
    // Reading needs a page; retrying is offered only where organizing failed.
    expect(find.byKey(const ValueKey('file-read-doc-1')), findsOneWidget);
    expect(find.byKey(const ValueKey('file-read-doc-2')), findsNothing);
    expect(find.byKey(const ValueKey('file-retry-doc-1')), findsNothing);
    expect(find.byKey(const ValueKey('file-retry-doc-3')), findsOneWidget);
    expect(find.byKey(const ValueKey('file-retry-doc-4')), findsOneWidget);
    expect(find.text('2 KB'), findsNWidgets(4));
    expect(server.writes(), isEmpty);
  });

  testWidgets(
    'picked files go to the scope in view, oversized ones stopped first',
    (tester) async {
      final server = FakeKnowledgeServer();
      server.handlers['POST /api/memory-documents'] = (_) => {
        ...documentJson('doc-2', 'guide.md', status: 'pending'),
        'created': true,
      };
      final harness = await mountKnowledge(
        tester,
        server,
        location: '/app/wiki?view=files&project=p1',
        picked: [_file('guide.md', 4), _file('large.pdf', 11 * 1024 * 1024)],
      );

      expect(
        find.text('支持 PDF（可选中文字）、Word、TXT、Markdown、CSV 和网页文件，单个文件不超过 10 MB。'),
        findsOneWidget,
      );
      await tapVisible(
        tester,
        find.byKey(const ValueKey('knowledge-upload-card')),
      );

      final form = _upload(server);
      expect(form.files.single.key, 'file');
      expect(form.files.single.value.filename, 'guide.md');
      expect(form.files.single.value.length, 4);
      expect(form.fields.map((field) => '${field.key}=${field.value}'), [
        'project_id=p1',
      ]);
      expect(server.writes(), hasLength(1));
      expect(harness.toasts, contains('「guide.md」已加入知识库，正在自动整理。'));
      expect(
        find.descendant(
          of: find.byKey(const ValueKey('knowledge-upload-error')),
          matching: find.text('请选择 10 MB 以内的非空文件。'),
        ),
        findsOneWidget,
      );
    },
  );

  testWidgets('the header button switches to the files and uploads there', (
    tester,
  ) async {
    final server = FakeKnowledgeServer();
    server.handlers['POST /api/memory-documents'] = (_) => {
      ...documentJson('doc-1', 'venue-guide.pdf'),
      'created': false,
    };
    final harness = await mountKnowledge(
      tester,
      server,
      picked: [_file('venue-guide.pdf', 2048)],
    );

    await tapVisible(tester, find.byKey(const ValueKey('knowledge-upload')));
    expect(
      tester.widget<KnowledgeTabs>(find.byType(KnowledgeTabs)).view,
      'files',
    );
    // No project in view: the file is personal.
    expect(_upload(server).fields, isEmpty);
    expect(harness.toasts, contains('「venue-guide.pdf」已在知识库中，未重复添加。'));
  });

  testWidgets('a refused upload says why in the person\'s terms', (
    tester,
  ) async {
    final server = FakeKnowledgeServer();
    server.handlers['POST /api/memory-documents'] = (_) =>
        throw const FakeHttpError(422, {
          'detail': {
            'code': 'DOCUMENT_FORMAT_UNSUPPORTED',
            'message': 'document_format_unsupported',
          },
        });
    await mountKnowledge(
      tester,
      server,
      location: '/app/wiki?view=files',
      picked: [_file('slides.key', 100)],
    );
    await tapVisible(
      tester,
      find.byKey(const ValueKey('knowledge-upload-card')),
    );
    expect(
      find.text('请上传 PDF、DOCX、TXT、Markdown、CSV 或 HTML 文件。'),
      findsOneWidget,
    );
  });

  testWidgets('uploading is unavailable where the account cannot upload', (
    tester,
  ) async {
    final server = FakeKnowledgeServer()..uploadsEnabled = false;
    await mountKnowledge(
      tester,
      server,
      location: '/app/wiki?view=files',
      picked: [_file('guide.md', 4)],
    );
    expect(find.text('当前账号还不能上传文件。'), findsOneWidget);
    expect(
      _enabled(tester, find.byKey(const ValueKey('knowledge-upload'))),
      isFalse,
    );
    await tapVisible(
      tester,
      find.byKey(const ValueKey('knowledge-upload-card')),
    );
    expect(server.writes(), isEmpty);
  });

  testWidgets('a failed file can be organized again', (tester) async {
    final server = FakeKnowledgeServer()
      ..documents = [
        documentJson(
          'doc-3',
          'scan.pdf',
          status: 'index_failed',
          pageIds: ['scan-page'],
        ),
      ];
    await mountKnowledge(tester, server, location: '/app/wiki?view=files');
    await tapVisible(tester, find.byKey(const ValueKey('file-retry-doc-3')));
    expect(server.calls, contains('POST /api/memory-documents/doc-3/retry'));
  });

  testWidgets(
    'a file is deleted only after it is confirmed, and only that one',
    (tester) async {
      final server = FakeKnowledgeServer();
      server.handlers['DELETE /api/memory-documents/doc-1'] = (_) => {
        'ok': true,
        'status': 'deleted',
        'original_cleanup': 'pending',
      };
      final harness = await mountKnowledge(
        tester,
        server,
        location: '/app/wiki?view=files',
      );

      await tapVisible(tester, find.byKey(const ValueKey('file-delete-doc-1')));
      expect(find.text('删除这个文件？'), findsOneWidget);
      expect(
        find.descendant(
          of: find.byType(AlertDialog),
          matching: find.text('venue-guide.pdf'),
        ),
        findsOneWidget,
      );
      await tapVisible(tester, find.text('取消'));
      expect(find.text('删除这个文件？'), findsNothing);
      expect(server.writes(), isEmpty);

      await tapVisible(tester, find.byKey(const ValueKey('file-delete-doc-1')));
      await tapVisible(
        tester,
        find.byKey(const ValueKey('file-delete-confirm')),
      );
      expect(server.calls.where((c) => c.startsWith('DELETE')), [
        'DELETE /api/memory-documents/doc-1',
      ]);
      expect(find.text('删除这个文件？'), findsNothing);
      // It does not claim the file is gone while its original is still stored.
      expect(
        harness.toasts.single,
        startsWith('已删除「venue-guide.pdf」。原文件还在从存储中移除'),
      );
    },
  );

  testWidgets('originals still being removed are mentioned', (tester) async {
    final server = FakeKnowledgeServer()..cleanupPending = 1;
    await mountKnowledge(tester, server, location: '/app/wiki?view=files');
    expect(find.text('有 1 个已删除文件的原件还在从存储中移除，会自动完成。'), findsOneWidget);
  });

  testWidgets('a file opens on its first page, and its original downloads', (
    tester,
  ) async {
    final server = FakeKnowledgeServer();
    server.handlers['GET /api/memory-documents/doc-1/original'] = (_) => [1, 2];
    server.handlers['GET /api/memory-wiki/pages/doc-page'] = (_) => {
      'id': 'doc-page',
      'slug': 'venue-guide',
      'title': 'Venue guide',
      'status': 'published',
      'body_available': true,
      'body': 'Doors open at nine.',
      'paragraphs': <Object>[],
      'source_details': <Object>[],
    };
    final harness = await mountKnowledge(
      tester,
      server,
      location: '/app/wiki?view=files',
    );

    await tapVisible(tester, find.byKey(const ValueKey('file-download-doc-1')));
    expect(harness.downloads.saved.single.name, 'venue-guide.pdf');
    expect(server.calls, contains('GET /api/memory-documents/doc-1/original'));

    await tapVisible(tester, find.byKey(const ValueKey('file-read-doc-1')));
    expect(harness.location, '/app/wiki/doc-page');
    expect(find.text('Venue guide'), findsOneWidget);
  });
}
