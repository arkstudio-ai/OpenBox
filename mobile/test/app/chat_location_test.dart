import 'dart:async';

import 'package:bossip_mobile/app/chat_location.dart';
import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

const _scope = (userId: 'alice', workspaceId: 'shop');

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test(
    'bookmark accepts only chat pages and strips transient task queries',
    () {
      expect(
        ChatLocationStore.chatPath('/app/assistant?intro=all'),
        '/app/assistant',
      );
      expect(
        ChatLocationStore.chatPath('/app/s/session_123?result=old'),
        '/app/s/session_123',
      );
      for (final path in [
        'https://example.com/app/s/secret',
        '//example.com/app',
        '/login',
        '/app/voice',
        '/app/settings',
        '/app/s/',
        '/app/s/a%2Fb',
        '/app/s/../../login',
      ]) {
        expect(ChatLocationStore.chatPath(path), isNull, reason: path);
      }
    },
  );

  testWidgets(
    'push, pop, background and reconstruction preserve visible chat',
    (tester) async {
      SharedPreferences.setMockInitialValues({});
      final prefs = await SharedPreferences.getInstance();
      final store = ChatLocationStore(prefs);
      final container = ProviderContainer(
        overrides: [
          prefsProvider.overrideWithValue(prefs),
          assistantScopeProvider.overrideWithValue(_scope),
        ],
      );
      final router = GoRouter(
        initialLocation: '/app/assistant',
        routes: [
          for (final path in ['/app', '/app/assistant', '/app/s/fruit-shop'])
            GoRoute(
              path: path,
              builder: (_, _) => RememberChatLocation(
                location: path,
                child: Scaffold(body: Text(path)),
              ),
            ),
          GoRoute(
            path: '/app/settings',
            builder: (_, _) => const Scaffold(body: Text('Settings')),
          ),
        ],
      );
      await tester.pumpWidget(
        UncontrolledProviderScope(
          container: container,
          child: MaterialApp.router(routerConfig: router),
        ),
      );
      await tester.pumpAndSettle();
      expect(store.read(_scope), '/app/assistant');

      unawaited(router.push('/app/s/fruit-shop'));
      await tester.pumpAndSettle();
      expect(store.read(_scope), '/app/s/fruit-shop');
      unawaited(router.push('/app/settings'));
      await tester.pumpAndSettle();
      expect(store.read(_scope), '/app/s/fruit-shop');

      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pumpAndSettle();
      expect(find.text('Settings'), findsOneWidget);
      expect(store.read(_scope), '/app/s/fruit-shop');
      router.pop();
      await tester.pumpAndSettle();
      router.pop();
      await tester.pumpAndSettle();
      expect(store.read(_scope), '/app/assistant');

      router.go('/app/s/fruit-shop');
      await tester.pumpAndSettle();
      await tester.pumpWidget(const SizedBox.shrink());
      router.dispose();
      container.dispose();
      // A fresh store has no in-memory route/controller from the prior process.
      expect(ChatLocationStore(prefs).read(_scope), '/app/s/fruit-shop');
      expect(
        ChatLocationStore(prefs).read((userId: 'bob', workspaceId: 'shop')),
        isNull,
      );
      expect(
        ChatLocationStore(prefs).read((userId: 'alice', workspaceId: 'other')),
        isNull,
      );
    },
  );
}
