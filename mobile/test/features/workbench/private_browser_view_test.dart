import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/assistant_screen.dart';
import 'package:bossip_mobile/features/chat/state/config_providers.dart';
import 'package:bossip_mobile/features/workbench/api/private_browser_api.dart';
import 'package:bossip_mobile/features/workbench/state/private_browser_controller.dart';
import 'package:bossip_mobile/features/workbench/widgets/private_browser_tab.dart';
import 'package:bossip_mobile/features/workbench/widgets/workbench_runtime_gate.dart';
import 'package:bossip_mobile/features/workbench/workbench_screen.dart';
import 'package:bossip_mobile/features/workbench/workbench_surface_page.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/containers_api.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/app_config.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../chat/assistant_fixture.dart' as chat;
import 'private_browser_fixture.dart';

final selectedScope = StateProvider<AssistantScope?>(
  (ref) => (userId: browserScope.userId, workspaceId: browserScope.workspaceId),
);

I18nBundle bundle() => I18nBundle({
  for (final lang in ['zh-CN', 'en-US'])
    lang: {
      for (final ns in ['workbench', 'chat', 'common', 'workspace'])
        ns: jsonDecode(
          File('assets/locales/$lang/$ns.json').readAsStringSync(),
        ),
      'private-browser': jsonDecode(
        File(
          'assets/locales-mobile/$lang/private-browser.json',
        ).readAsStringSync(),
      ),
    },
});

Future<ProviderContainer> setup(
  BrowserServer server, {
  chat.TestApi? assistant,
}) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
  final prefs = await SharedPreferences.getInstance();
  return ProviderContainer(
    overrides: [
      prefsProvider.overrideWithValue(prefs),
      authSessionProvider.overrideWithValue(server.auth),
      workspaceScopeProvider.overrideWithValue(server.workspace),
      apiDioProvider.overrideWithValue(server.dio),
      assistantScopeProvider.overrideWith((ref) => ref.watch(selectedScope)),
      i18nProvider.overrideWith(() => I18nController(bundle(), prefs)),
      if (assistant != null) ...[
        assistantApiProvider(chat.scope).overrideWithValue(assistant),
        wsClientProvider.overrideWith((ref) {
          final ws = chat.TestWs();
          ref.onDispose(() => unawaited(ws.close()));
          return ws;
        }),
        appConfigProvider.overrideWith(
          (ref) async => AppConfig.fromJson({
            'models': <Map<String, dynamic>>[],
            'default_model': 'test/model',
          }),
        ),
        runningContainerProvider.overrideWith(
          (ref) => throw StateError(
            'Main assistant must not discover a native desktop',
          ),
        ),
      ],
    ],
  );
}

Widget app(ProviderContainer container, Widget child) =>
    UncontrolledProviderScope(
      container: container,
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: child,
      ),
    );

Future<void> settle(WidgetTester tester) async {
  // Dio's real request/response transformers traverse several event turns.
  for (var i = 0; i < 10; i++) {
    await tester.pump(const Duration(milliseconds: 10));
  }
}

Future<void> complete(WidgetTester tester, Future<void> future) async {
  var done = false;
  unawaited(
    future.then(
      (_) {
        done = true;
      },
      onError: (Object _) {
        done = true;
      },
    ),
  );
  for (var i = 0; i < 100 && !done; i++) {
    await tester.pump(const Duration(milliseconds: 1));
  }
  expect(
    done,
    isTrue,
    reason: 'Offline API must complete within bounded event turns',
  );
  await future;
}

void main() {
  testWidgets('private deep links cannot mount any ordinary/native surface', (
    tester,
  ) async {
    final server = BrowserServer();
    final container = await setup(server);
    addTearDown(container.dispose);
    for (final kind in ['desktop', 'browser', 'terminal', 'files', 'review']) {
      await tester.pumpWidget(
        app(
          container,
          WorkbenchSurfacePage(
            key: ValueKey(kind),
            sessionId: browserScope.sessionId,
            kind: kind,
            control: true,
          ),
        ),
      );
      await settle(tester);
      expect(find.byType(PrivateBrowserTab), findsOneWidget);
      expect(server.controls, isEmpty);
      expect(server.operations, isEmpty);
      expect(server.ensures, 0);
    }
    expect(
      server.requests.every(
        (r) =>
            r.method == 'GET' &&
            (r.path.startsWith('/api/agent/session/') ||
                r.path.endsWith('/current')),
      ),
      isTrue,
    );
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump();
  });

  testWidgets('metadata pending or denied never falls back to native', (
    tester,
  ) async {
    final server = BrowserServer();
    final gate = Completer<void>();
    server.intercept = (request) async {
      await gate.future;
      return {'id': 'wrong-session', 'workspace_id': 'workspace'};
    };
    final container = await setup(server);
    addTearDown(container.dispose);
    await tester.pumpWidget(
      app(
        container,
        const WorkbenchScreen(
          sessionId: 'private-session',
          initialTab: 'desktop',
          initialControl: true,
        ),
      ),
    );
    await settle(tester);
    expect(find.byType(PrivateBrowserTab), findsNothing);
    expect(server.requests, hasLength(1));
    gate.complete();
    await settle(tester);
    expect(find.textContaining('无法确认当前控制权'), findsOneWidget);
    expect(server.requests, hasLength(1));
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets(
    'known assistant entry rejects ordinary or empty metadata scope',
    (tester) async {
      final server = BrowserServer()..sessionPrivate = false;
      final container = await setup(server);
      addTearDown(container.dispose);
      for (final id in [browserScope.sessionId, '']) {
        await tester.pumpWidget(
          app(
            container,
            WorkbenchScreen(
              key: ValueKey(id),
              sessionId: id,
              privateOnly: true,
            ),
          ),
        );
        await settle(tester);
        expect(find.textContaining('无法确认当前控制权'), findsOneWidget);
        expect(find.byType(PrivateBrowserTab), findsNothing);
      }
      expect(server.requests, hasLength(1));
      expect(server.requests.single.path, startsWith('/api/agent/session/'));
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets(
    'ordinary Session and explicit global desktop retain their old builder',
    (tester) async {
      final server = BrowserServer()..sessionPrivate = false;
      final container = await setup(server);
      addTearDown(container.dispose);
      for (final id in [browserScope.sessionId, '']) {
        await tester.pumpWidget(
          app(
            container,
            WorkbenchRuntimeGate(
              key: ValueKey(id),
              sessionId: id,
              privateBuilder: (_) => const Text('private'),
              ordinaryBuilder: (_) => const Text('ordinary-native-builder'),
            ),
          ),
        );
        await settle(tester);
        expect(find.text('ordinary-native-builder'), findsOneWidget);
        expect(find.text('private'), findsNothing);
      }
      expect(server.requests, hasLength(1));
      expect(server.controls, isEmpty);
    },
  );

  testWidgets(
    'finite input uses current screenshot coordinates and shows explicit giveback',
    (tester) async {
      final server = BrowserServer();
      final container = await setup(server);
      addTearDown(container.dispose);
      await tester.pumpWidget(
        app(container, const WorkbenchScreen(sessionId: 'private-session')),
      );
      await settle(tester);
      await tester.tap(find.text('接管操控'));
      await settle(tester);
      expect(find.byKey(const ValueKey('browser-frame')), findsOneWidget);
      final frame = find.byKey(const ValueKey('browser-frame'));
      await tester.ensureVisible(frame);
      await tester.tapAt(tester.getCenter(frame));
      await settle(tester);
      final mouse = server.operations.singleWhere((r) => r['kind'] == 'mouse');
      expect(mouse['args'], {'x': 512, 'y': 384, 'button': 'left'});
      await tester.scrollUntilVisible(
        find.byKey(const ValueKey('browser-text')),
        300,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.enterText(
        find.byKey(const ValueKey('browser-text')),
        'hello',
      );
      await tester.pump();
      await tester.tap(find.text('发送文本'));
      await settle(tester);
      expect(server.operations.where((r) => r['kind'] == 'text'), hasLength(1));
      expect(server.operations.last['kind'], 'capture');
      await tester.scrollUntilVisible(
        find.text('交回控制'),
        -300,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.tap(find.text('交回控制'));
      await settle(tester);
      expect(server.controls.last['action'], 'giveback');
      expect(
        container
            .read(privateBrowserControllerProvider(browserScope))
            .givenBack,
        (resumed: 1, changed: 0),
      );
      await tester.scrollUntilVisible(
        find.byKey(const ValueKey('browser-given-back')),
        -300,
        scrollable: find.byType(Scrollable).first,
      );
      expect(find.byKey(const ValueKey('browser-given-back')), findsOneWidget);
      expect(find.byKey(const ValueKey('browser-frame')), findsNothing);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump();
    },
  );

  testWidgets(
    'unknown operation remains visible through status checks without input replay',
    (tester) async {
      final server = BrowserServer();
      final container = await setup(server);
      addTearDown(container.dispose);
      await tester.pumpWidget(
        app(container, const WorkbenchScreen(sessionId: 'private-session')),
      );
      await settle(tester);
      await tester.tap(find.text('接管操控'));
      await settle(tester);
      server.intercept = (request) async {
        if (request.path.endsWith('/operations') &&
            server.data(request)['kind'] == 'text') {
          await server.handle(request);
          server.timeout(request);
        }
        return server.handle(request);
      };
      final controller = container.read(
        privateBrowserControllerProvider(browserScope),
      );
      await complete(tester, controller.operate('text', {'text': 'once'}));
      await settle(tester);
      expect(find.byKey(const ValueKey('browser-unknown')), findsOneWidget);
      final count = server.operations.length;
      await complete(tester, controller.refresh());
      await tester.pump(const Duration(seconds: 6));
      await settle(tester);
      expect(find.byKey(const ValueKey('browser-unknown')), findsOneWidget);
      expect(server.operations, hasLength(count));
      expect(server.heartbeatCount, 0);
      expect(find.byKey(const ValueKey('browser-frame')), findsNothing);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets('natural TTL stops input/heartbeats without automatic giveback', (
    tester,
  ) async {
    final server = BrowserServer()..ttl = const Duration(seconds: 2);
    final controller = PrivateBrowserController(server.api);
    addTearDown(controller.dispose);
    await complete(tester, controller.start());
    await complete(tester, controller.control('takeover'));
    expect(controller.controlled, isTrue);
    await tester.pump(const Duration(seconds: 3));
    expect(controller.controlled, isFalse);
    expect(controller.frame, isNull);
    await tester.pump(const Duration(seconds: 60));
    await complete(tester, controller.operate('text', {'text': 'late'}));
    expect(server.heartbeatCount, 0);
    expect(server.controls.map((r) => r['action']), ['takeover']);
    expect(server.operations.map((r) => r['kind']), ['capture']);
    controller.setVisible(false);
  });

  testWidgets(
    'background and disposal stop heartbeat; reopening never restores credentials',
    (tester) async {
      final server = BrowserServer();
      final container = await setup(server);
      addTearDown(container.dispose);
      await tester.pumpWidget(
        app(container, const WorkbenchScreen(sessionId: 'private-session')),
      );
      await settle(tester);
      await tester.tap(find.text('接管操控'));
      await settle(tester);
      await tester.pump(const Duration(seconds: 30));
      await settle(tester);
      expect(server.heartbeatCount, 1);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
      await tester.pump();
      await tester.pump(const Duration(seconds: 60));
      expect(server.heartbeatCount, 1);
      expect(
        container.read(privateBrowserControllerProvider(browserScope)).frame,
        isNull,
      );
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await settle(tester);
      expect(find.byKey(const ValueKey('browser-frame')), findsNothing);
      expect(server.controls, hasLength(1));
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump(const Duration(seconds: 60));
      expect(server.heartbeatCount, 1);
      await tester.pumpWidget(
        app(container, const WorkbenchScreen(sessionId: 'private-session')),
      );
      await settle(tester);
      expect(find.byKey(const ValueKey('browser-frame')), findsNothing);
      expect(server.controls, hasLength(1));
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets('a late heartbeat cannot restore credentials after expiry', (
    tester,
  ) async {
    final server = BrowserServer()..ttl = const Duration(seconds: 32);
    final controller = PrivateBrowserController(server.api);
    addTearDown(controller.dispose);
    await complete(tester, controller.start());
    await complete(tester, controller.control('takeover'));
    final release = Completer<void>();
    server.intercept = (request) async {
      if (request.path.endsWith('/heartbeat')) {
        await release.future;
      }
      return server.handle(request);
    };
    await tester.pump(const Duration(seconds: 30));
    await tester.pump(const Duration(seconds: 3));
    expect(controller.controlled, isFalse);
    release.complete();
    await settle(tester);
    expect(controller.controlled, isFalse);
    expect(controller.frame, isNull);
    expect(server.controls, hasLength(1));
    controller.setVisible(false);
  });

  testWidgets(
    'changing Session drops a grant even for the same actor/browser',
    (tester) async {
      final server = BrowserServer();
      final container = await setup(server);
      addTearDown(container.dispose);
      await tester.pumpWidget(
        app(container, const WorkbenchScreen(sessionId: 'private-session')),
      );
      await settle(tester);
      await tester.tap(find.text('接管操控'));
      await settle(tester);
      server.intercept = (request) async {
        final response = await server.handle(request);
        if (request.path.startsWith('/api/agent/session/')) {
          response['id'] = 'other-session';
        }
        return response;
      };
      await tester.pumpWidget(
        app(container, const WorkbenchScreen(sessionId: 'other-session')),
      );
      await settle(tester);
      expect(find.byKey(const ValueKey('browser-frame')), findsNothing);
      expect(server.controls, hasLength(1));
      await tester.pump(const Duration(seconds: 31));
      expect(server.heartbeatCount, 0);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets(
    'main assistant opens its finite view without discovering a native desktop',
    (tester) async {
      const mainScope = (
        userId: 'owner',
        workspaceId: 'workspace',
        sessionId: 'main',
        authRevision: 0,
      );
      final server = BrowserServer(scope: mainScope);
      final container = await setup(server, assistant: chat.TestApi());
      var disposed = false;
      void cleanup() {
        if (disposed) return;
        disposed = true;
        container.dispose();
      }

      addTearDown(cleanup);
      await tester.pumpWidget(
        app(
          container,
          const Scaffold(body: AssistantScreen(scope: chat.scope)),
        ),
      );
      await settle(tester);
      await tester.tap(find.byKey(const ValueKey('assistant-private-browser')));
      await tester.pumpAndSettle();
      expect(
        tester
            .widget<WorkbenchScreen>(find.byType(WorkbenchScreen))
            .privateOnly,
        isTrue,
      );
      expect(find.byType(PrivateBrowserTab), findsOneWidget);
      expect(server.controls, isEmpty);
      expect(server.requests.map((r) => r.path), [
        '/api/agent/session/main',
        '${PrivateBrowserApi.base}/current',
      ]);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      cleanup();
      await tester.pump();
    },
  );

  testWidgets(
    'covering the route stops renewal and returning requires a new explicit grant',
    (tester) async {
      final server = BrowserServer();
      final container = await setup(server);
      addTearDown(container.dispose);
      await tester.pumpWidget(
        app(container, const WorkbenchScreen(sessionId: 'private-session')),
      );
      await settle(tester);
      await tester.tap(find.text('接管操控'));
      await settle(tester);
      expect(
        container
            .read(privateBrowserControllerProvider(browserScope))
            .controlled,
        isTrue,
      );
      final navigator = Navigator.of(
        tester.element(find.byType(PrivateBrowserTab)),
      );
      final priorRequests = server.requests.length;
      final priorOperations = server.operations.length;
      unawaited(
        navigator.push<void>(
          MaterialPageRoute<void>(
            builder: (_) => const Scaffold(body: Text('covering route')),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(
        container
            .read(privateBrowserControllerProvider(browserScope))
            .controlled,
        isFalse,
      );
      await tester.pump(const Duration(seconds: 61));
      expect(server.heartbeatCount, 0);
      navigator.pop();
      await tester.pumpAndSettle();
      await settle(tester);
      expect(find.byKey(const ValueKey('browser-frame')), findsNothing);
      expect(server.controls, hasLength(1));
      expect(server.operations, hasLength(priorOperations));
      expect(server.heartbeatCount, 0);
      expect(server.requests.skip(priorRequests), isNotEmpty);
      expect(
        server.requests
            .skip(priorRequests)
            .every(
              (request) =>
                  request.method == 'GET' && request.path.endsWith('/current'),
            ),
        isTrue,
      );
      expect(
        container
            .read(privateBrowserControllerProvider(browserScope))
            .controlled,
        isFalse,
      );
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets(
    'same actor reauthentication immediately removes the old frame and grant',
    (tester) async {
      final server = BrowserServer();
      final container = await setup(server);
      addTearDown(container.dispose);
      const user = AuthUser(id: 'owner', username: 'owner');
      final auth = container.read(authProvider.notifier);
      auth.setAuth('first-local-test-token', user);
      await tester.pumpWidget(
        app(container, const WorkbenchScreen(sessionId: 'private-session')),
      );
      await settle(tester);
      await tester.tap(find.text('接管操控'));
      await settle(tester);
      expect(find.byKey(const ValueKey('browser-frame')), findsOneWidget);
      final oldScope = tester
          .widget<PrivateBrowserTab>(find.byType(PrivateBrowserTab))
          .scope;
      final priorRequests = server.requests.length;
      final priorOperations = server.operations.length;
      auth.setAuth('replacement-local-test-token', user);
      await tester.pump();
      expect(find.byKey(const ValueKey('browser-frame')), findsNothing);
      await settle(tester);
      final newScope = tester
          .widget<PrivateBrowserTab>(find.byType(PrivateBrowserTab))
          .scope;
      expect(newScope.userId, oldScope.userId);
      expect(newScope.workspaceId, oldScope.workspaceId);
      expect(newScope.authRevision, oldScope.authRevision + 1);
      expect(
        container.read(privateBrowserControllerProvider(newScope)).controlled,
        isFalse,
      );
      await tester.pump(const Duration(seconds: 31));
      await settle(tester);
      expect(server.heartbeatCount, 0);
      expect(server.controls, hasLength(1));
      expect(server.operations, hasLength(priorOperations));
      expect(
        server.requests
            .skip(priorRequests)
            .every((request) => request.method == 'GET'),
        isTrue,
      );
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets('a PNG decoding failure also revokes local input authority', (
    tester,
  ) async {
    final server = BrowserServer();
    server.intercept = (request) async {
      final result = await server.handle(request);
      if (request.path.endsWith('/operations')) {
        browserMap(result['result'])['png_base64'] = base64Encode([
          137,
          80,
          78,
          71,
          13,
          10,
          26,
          10,
          0,
        ]);
      }
      return result;
    };
    final container = await setup(server);
    addTearDown(container.dispose);
    await tester.pumpWidget(
      app(container, const WorkbenchScreen(sessionId: 'private-session')),
    );
    await settle(tester);
    await tester.tap(find.text('接管操控'));
    await settle(tester);
    await tester.pumpAndSettle();
    final controller = container.read(
      privateBrowserControllerProvider(browserScope),
    );
    expect(controller.controlled, isFalse);
    expect(controller.frame, isNull);
    expect(find.byKey(const ValueKey('browser-frame')), findsNothing);
    await tester.scrollUntilVisible(
      find.byKey(const ValueKey('browser-text')),
      300,
      scrollable: find.byType(Scrollable).first,
    );
    expect(
      tester
          .widget<TextField>(find.byKey(const ValueKey('browser-text')))
          .enabled,
      isFalse,
    );
    final count = server.operations.length;
    await complete(
      tester,
      controller.operate('mouse', {'x': 1, 'y': 1, 'button': 'left'}),
    );
    expect(server.operations, hasLength(count));
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
  });
}
