import 'dart:async';

import 'package:bossip_mobile/app/assistant_session_entry.dart';
import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/shared/models/session.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

import '../features/chat/assistant_fixture.dart';

class _EntryApi extends TestApi {
  final entry = Completer<Session>();
  @override
  Future<Session> entrySession(String id) => entry.future;
}

void main() {
  for (final kind in ['assistant', 'normal']) {
    testWidgets(
      '$kind entry resolves kind before mounting any ordinary history or composer',
      (tester) async {
        final api = _EntryApi();
        final container = ProviderContainer(
          overrides: [
            assistantScopeProvider.overrideWithValue(scope),
            assistantApiProvider(scope).overrideWithValue(api),
          ],
        );
        var ordinaryBuilds = 0;
        final router = GoRouter(
          initialLocation: '/legacy',
          routes: [
            GoRoute(
              path: '/legacy',
              builder: (_, _) => AssistantSessionEntry(
                sessionId: 'main',
                builder: (_) {
                  ordinaryBuilds++;
                  return const Text('Ordinary chat');
                },
              ),
            ),
            GoRoute(
              path: Paths.assistant,
              builder: (_, _) => const Text('Fixed assistant'),
            ),
          ],
        );
        await tester.pumpWidget(
          UncontrolledProviderScope(
            container: container,
            child: MaterialApp.router(routerConfig: router),
          ),
        );
        await tester.pump();
        expect(ordinaryBuilds, 0);
        expect(find.text('Ordinary chat'), findsNothing);
        api.entry.complete(Session.fromJson({'id': 'main', 'kind': kind}));
        await tester.pumpAndSettle();
        expect(
          find.text(kind == 'assistant' ? 'Fixed assistant' : 'Ordinary chat'),
          findsOneWidget,
        );
        expect(ordinaryBuilds, kind == 'assistant' ? 0 : greaterThan(0));
        expect(api.historyReads, isEmpty);
        expect(api.sends, isEmpty);
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox.shrink());
        router.dispose();
        container.dispose();
        await tester.pump(const Duration(milliseconds: 1));
      },
    );
  }
}
