import 'dart:async';

import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/shared/events/app_lifecycle.dart';
import 'package:flutter_test/flutter_test.dart';

import 'knowledge_fixture.dart';

int _reads(FakeKnowledgeServer server, String path) =>
    server.requests.where((r) => r.method == 'GET' && r.path == path).length;

void main() {
  setUpKnowledgeTests();

  testWidgets(
    'files still being organized are checked again every few seconds',
    (tester) async {
      final server = FakeKnowledgeServer()
        ..documents = [documentJson('doc-1', 'guide.md', status: 'parsing')];
      await mountKnowledge(tester, server, location: '/app/wiki?view=files');
      expect(find.text('正在整理'), findsOneWidget);
      final before = _reads(server, '/api/memory-documents');

      server.documents = [
        documentJson('doc-1', 'guide.md', pageIds: ['p']),
      ];
      await tester.pump(const Duration(seconds: 4));
      await settle(tester);
      expect(_reads(server, '/api/memory-documents'), before + 1);
      expect(find.text('可以阅读'), findsOneWidget);

      // Once nothing is left to organize, the page checks back far less often.
      await tester.pump(const Duration(seconds: 5));
      await settle(tester);
      expect(_reads(server, '/api/memory-documents'), before + 1);
      await tester.pump(const Duration(seconds: 30));
      await settle(tester);
      expect(_reads(server, '/api/memory-documents'), before + 2);
    },
  );

  testWidgets(
    'nothing is read while the app is off screen, everything on return',
    (tester) async {
      final server = FakeKnowledgeServer();
      final harness = await mountKnowledge(tester, server);
      final before = server.requests.length;

      harness.container.read(appVisibleProvider.notifier).state = false;
      await tester.pump(const Duration(seconds: 31));
      await settle(tester);
      expect(server.requests.length, before);

      harness.container.read(appVisibleProvider.notifier).state = true;
      await settle(tester);
      expect(
        server.calls.skip(before),
        containsAll([
          'GET /api/memories?limit=100&status=ACTIVE',
          'GET /api/memory-wiki/library?query=&status=all&offset=0',
          'GET /api/memory-documents?offset=0',
        ]),
      );
    },
  );

  testWidgets(
    'after a workspace switch nothing from the previous one is shown, even while reloading',
    (tester) async {
      final server = FakeKnowledgeServer();
      final harness = await mountKnowledge(tester, server);
      expect(find.text('Use Shanghai timezone'), findsOneWidget);

      final teamMemories = Completer<Object?>();
      server.handlers['GET /api/memories'] = (_) => teamMemories.future;
      await harness.container
          .read(activeWorkspaceProvider.notifier)
          .select('team');
      await settle(tester);
      expect(find.text('Use Shanghai timezone'), findsNothing);
      expect(find.text('Weekly report goes out on Friday'), findsNothing);

      teamMemories.complete({
        'memories': [memoryJson('memory-9', 'Team stand-up is at ten')],
        'next_offset': null,
      });
      await settle(tester);
      expect(find.text('Team stand-up is at ten'), findsOneWidget);
      expect(find.text('Use Shanghai timezone'), findsNothing);
    },
  );
}
