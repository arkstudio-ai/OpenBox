import 'dart:convert';
import 'dart:io';

import 'package:bossip_mobile/features/workbench/widgets/desktop_activation_host.dart';
import 'package:bossip_mobile/features/workbench/widgets/desktop_tab.dart';
import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/desktop_api.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/desktop.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

const scopeA = (userId: 'user-a', workspaceId: 'workspace-a');
const scopeB = (userId: 'user-a', workspaceId: 'workspace-b');
const scopeC = (userId: 'user-b', workspaceId: 'workspace-b');
const free = DesktopStatus(
  mode: 'per_user',
  state: 'subscription_required',
  entitled: false,
  retained: true,
);
const pending = DesktopStatus(
  mode: 'per_user',
  state: 'starting',
  entitled: true,
  activation: DesktopActivation(
    requestId: 'pay-a',
    state: 'running',
    step: 'starting',
  ),
);
const ready = DesktopStatus(
  mode: 'per_user',
  state: 'running',
  entitled: true,
  activation: DesktopActivation(
    requestId: 'pay-a',
    state: 'ready',
    step: 'ready',
  ),
);
final statusState = StateProvider<DesktopStatus>((ref) => pending);
final statusFailureState = StateProvider<Object?>((ref) => null);

class _NoPurchasesApi extends DesktopApi {
  _NoPurchasesApi() : super(Dio(), AuthSession(), WorkspaceScope());
  int retries = 0;
  int tickets = 0;
  @override
  Future<DesktopStatus> retry(DesktopScope scope) async {
    retries++;
    return pending;
  }

  @override
  Future<Map<String, dynamic>> ticket(
    DesktopScope scope, {
    String? taskId,
    CancelToken? cancel,
  }) async {
    tickets++;
    throw StateError('Viewer should not request a ticket for Free/pending');
  }
}

class _TicketErrorApi extends _NoPurchasesApi {
  _TicketErrorApi({this.statusCode = 423, this.code = 'RESOURCE_CONTROL_HELD'});
  final int statusCode;
  final String code;

  @override
  Future<Map<String, dynamic>> ticket(
    DesktopScope scope, {
    String? taskId,
    CancelToken? cancel,
  }) async {
    tickets++;
    final request = RequestOptions(path: '/api/desktop/ticket');
    throw DioException.badResponse(
      statusCode: statusCode,
      requestOptions: request,
      response: Response<Map<String, dynamic>>(
        requestOptions: request,
        statusCode: statusCode,
        data: {
          'available': false,
          'reason': 'resource_control_required',
          'code': code,
        },
      ),
    );
  }
}

class _Counter extends StatefulWidget {
  const _Counter();
  @override
  State<_Counter> createState() => _CounterState();
}

class _CounterState extends State<_Counter> {
  int count = 0;
  @override
  Widget build(BuildContext context) => Scaffold(
    body: Center(
      child: TextButton(
        onPressed: () => setState(() => count++),
        child: Text('chat-$count'),
      ),
    ),
  );
}

Future<ProviderContainer> setup(
  WidgetTester tester, {
  DesktopStatus initial = pending,
  DesktopApi? api,
}) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
  final prefs = await SharedPreferences.getInstance();
  final bundle = I18nBundle({
    'zh-CN': {
      'workbench': jsonDecode(
        File('assets/locales/zh-CN/workbench.json').readAsStringSync(),
      ),
      'errors': jsonDecode(
        File('assets/locales/zh-CN/errors.json').readAsStringSync(),
      ),
    },
  });
  final container = ProviderContainer(
    overrides: [
      prefsProvider.overrideWithValue(prefs),
      i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
      statusState.overrideWith((ref) => initial),
      for (final scope in [scopeA, scopeB, scopeC])
        desktopStatusProvider(scope).overrideWith((ref) async {
          final failure = ref.watch(statusFailureState);
          if (failure != null) throw failure;
          return ref.watch(statusState);
        }),
      desktopApiProvider.overrideWithValue(api ?? _NoPurchasesApi()),
    ],
  );
  addTearDown(container.dispose);
  return container;
}

Future<void> mount(
  WidgetTester tester,
  ProviderContainer container, {
  DesktopScope scope = scopeA,
  bool manage = true,
  bool viewer = false,
}) async {
  await tester.pumpWidget(
    UncontrolledProviderScope(
      container: container,
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: viewer
            ? Scaffold(
                body: ScopedDesktopViewer(
                  key: ValueKey(scope),
                  scope: scope,
                  canManage: manage,
                ),
              )
            : const _Counter(),
        builder: viewer
            ? null
            : (context, child) => DesktopActivationHost(
                key: ValueKey(scope),
                scope: scope,
                workspaceName: scope.workspaceId,
                canManage: manage,
                child: child!,
              ),
      ),
    ),
  );
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 20));
  expect(tester.takeException(), isNull);
}

void main() {
  testWidgets(
    'managed native denial remains visible through status refreshes, with no retry loop',
    (tester) async {
      final api = _TicketErrorApi();
      final container = await setup(tester, initial: ready, api: api);
      await mount(tester, container, viewer: true);
      expect(find.text('此桌面已启用任务控制，暂不支持直接连接。'), findsOneWidget);
      expect(find.text('重新连接'), findsNothing);
      expect(api.tickets, 1);
      await tester.pump(const Duration(minutes: 2));
      container.read(statusState.notifier).state = pending;
      await tester.pump();
      await tester.pump();
      container.read(statusState.notifier).state = ready;
      await tester.pump();
      await tester.pump();
      container.read(statusFailureState.notifier).state = StateError(
        'temporary status failure',
      );
      await tester.pump();
      await tester.pump();
      expect(find.text('此桌面已启用任务控制，暂不支持直接连接。'), findsOneWidget);
      expect(find.text('重新连接'), findsNothing);
      expect(api.tickets, 1);
      container.read(statusFailureState.notifier).state = null;
      await tester.pump();
      await tester.pump();
      await mount(tester, container, scope: scopeB, viewer: true);
      expect(api.tickets, 2); // A new workspace is checked independently.
      await tester.pumpWidget(const SizedBox());
      await mount(tester, container, scope: scopeB, viewer: true);
      expect(api.tickets, 3); // Explicitly reopening does a new backend check.
      await mount(tester, container, scope: scopeC, viewer: true);
      expect(api.tickets, 4); // The same workspace under another actor is new.
      expect(api.retries, 0);
    },
  );

  testWidgets(
    'ordinary temporary ticket errors still offer explicit reconnect',
    (tester) async {
      final api = _TicketErrorApi(statusCode: 503, code: 'HTTP_503');
      final container = await setup(tester, initial: ready, api: api);
      await mount(tester, container, viewer: true);
      expect(api.tickets, 1);
      await tester.tap(find.text('重新连接'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 20));
      expect(api.tickets, 2);
      expect(find.text('重新连接'), findsOneWidget);
      expect(find.text('此桌面已启用任务控制，暂不支持直接连接。'), findsNothing);
    },
  );

  testWidgets(
    'Free viewer offers subscription, never manual provisioning or tickets',
    (tester) async {
      final container = await setup(tester, initial: free);
      await mount(tester, container, viewer: true);
      expect(find.text('付费套餐专享无影云'), findsOneWidget);
      expect(find.text('查看套餐'), findsOneWidget);
      expect(find.text('开通云电脑'), findsNothing);
      expect(find.text('重新创建'), findsNothing);
      final api = container.read(desktopApiProvider) as _NoPurchasesApi;
      expect(api.retries, 0);
      expect(api.tickets, 0);
    },
  );

  testWidgets('pending viewer waits automatically and expiry becomes Free', (
    tester,
  ) async {
    final container = await setup(tester);
    await mount(tester, container, viewer: true);
    expect(find.text('正在准备你的无影云'), findsWidgets);
    expect(find.text('开通云电脑'), findsNothing);
    container.read(statusState.notifier).state = free;
    await tester.pump();
    await tester.pump();
    expect(find.text('付费套餐专享无影云'), findsOneWidget);
    expect((container.read(desktopApiProvider) as _NoPurchasesApi).tickets, 0);
  });

  testWidgets(
    'progress can close/reopen/resume without blocking ordinary chat',
    (tester) async {
      final container = await setup(tester);
      await mount(tester, container);
      expect(find.text('付款已确认'), findsOneWidget);
      expect(find.text('启动云电脑'), findsOneWidget);
      await tester.tap(find.text('继续对话'));
      await tester.pump();
      expect(find.byKey(const Key('desktop-activation-dialog')), findsNothing);
      expect(find.byKey(const Key('desktop-progress-open')), findsOneWidget);
      await tester.tap(find.text('chat-0'));
      await tester.pump();
      expect(find.text('chat-1'), findsOneWidget);
      await tester.tap(find.byKey(const Key('desktop-progress-open')));
      await tester.pump();
      expect(find.text('正在准备你的无影云'), findsOneWidget);
      await tester.tap(find.text('继续对话'));
      await tester.pump();
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pump();
      await tester.pump();
      expect(find.text('正在准备你的无影云'), findsOneWidget);
      expect(find.text('chat-1'), findsOneWidget);
      expect(
        (container.read(desktopApiProvider) as _NoPurchasesApi).retries,
        0,
      );
    },
  );

  testWidgets(
    'ready acknowledgement survives remount and is workspace-scoped',
    (tester) async {
      final container = await setup(tester, initial: ready);
      await mount(tester, container);
      expect(find.text('无影云已就绪'), findsOneWidget);
      await tester.tap(find.text('知道了'));
      await tester.pump();
      await tester.pumpWidget(const SizedBox());
      await mount(tester, container);
      expect(find.text('无影云已就绪'), findsNothing);
      await mount(tester, container, scope: scopeB);
      expect(find.text('无影云已就绪'), findsOneWidget);
    },
  );

  testWidgets('ready reopens dismissed pending progress, keeping chat state', (
    tester,
  ) async {
    final container = await setup(tester, initial: free);
    await mount(tester, container);
    await tester.tap(find.text('chat-0'));
    await tester.pump();
    container.read(statusState.notifier).state = pending;
    await tester.pump();
    await tester.pump();
    await tester.tap(find.text('继续对话'));
    await tester.pump();
    container.read(statusState.notifier).state = ready;
    await tester.pump();
    await tester.pump();
    expect(find.text('无影云已就绪'), findsOneWidget);
    expect(find.text('chat-1'), findsOneWidget);
  });

  testWidgets(
    'only managers get an explicit safe retry; attention never purchases on mount',
    (tester) async {
      const attention = DesktopStatus(
        mode: 'per_user',
        state: 'failed',
        entitled: true,
        activation: DesktopActivation(
          requestId: 'pay-a',
          state: 'needs_attention',
          step: 'assigning',
          canRetry: true,
        ),
      );
      final container = await setup(tester, initial: attention);
      await mount(tester, container, manage: false);
      expect(find.text('云电脑开通待核实'), findsOneWidget);
      expect(find.text('立即重试'), findsNothing);
      await mount(tester, container);
      expect(find.text('立即重试'), findsOneWidget);
      final api = container.read(desktopApiProvider) as _NoPurchasesApi;
      expect(api.retries, 0);
      await tester.tap(find.text('立即重试'));
      await tester.pump();
      await tester.pump();
      expect(api.retries, 1);
    },
  );
}
