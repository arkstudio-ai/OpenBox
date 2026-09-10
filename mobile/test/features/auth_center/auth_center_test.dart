import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:bossip_mobile/features/auth_center/auth_center_screen.dart';
import 'package:bossip_mobile/features/auth_center/widgets/platform_qr.dart';
import 'package:bossip_mobile/features/auth_center/widgets/publish_job_view.dart';
import 'package:bossip_mobile/features/chat/api/assets_api.dart';
import 'package:bossip_mobile/features/chat/utils/content_view.dart';
import 'package:bossip_mobile/features/chat/widgets/result_artifacts.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/platform_accounts_api.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/platform_account.dart';
import 'package:bossip_mobile/shared/models/resource.dart';
import 'package:bossip_mobile/shared/platforms/platform_links.dart';
import 'package:bossip_mobile/shared/widgets/toast.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:intl/date_symbol_data_local.dart';
import 'package:shared_preferences/shared_preferences.dart';

const scopeA = (userId: 'user-a', workspaceId: 'workspace-a');
const scopeB = (userId: 'user-b', workspaceId: 'workspace-b');
const authorizing =
    'https://open.douyin.com/platform/oauth/connect/?state=nonce&redirect_uri=https%3A%2F%2Fai.bossipai.com.cn%2Fapi%2Fplatform-accounts%2Fdouyin%2Fcallback';
const launch = 'snssdk1128://openplatform/share?share_type=h5';
final bound = PlatformAccount(
  id: 'account-a',
  platform: 'douyin',
  status: 'bound',
  nickname: '验收账号',
  externalId: 'open-a',
  renewalsLeft: 1,
  refreshExpiresAt: DateTime.now().add(const Duration(days: 20)),
  estimatedExpiresAt: DateTime.now().add(const Duration(days: 50)),
);
PublishJob pending({DateTime? expires}) => PublishJob(
  id: 'job-a',
  status: 'pending',
  title: '验收视频',
  shareId: 'share-a',
  expiresAt: expires ?? DateTime.now().add(const Duration(hours: 1)),
);

class FakePlatformApi extends PlatformAccountsApi {
  FakePlatformApi() : super(Dio(), AuthSession(), WorkspaceScope());
  bool configured = true;
  List<PlatformAccount> rows = [];
  List<PublishJob> history = [];
  PublishJob current = pending();
  Object? jobError;
  int starts = 0,
      probes = 0,
      deletes = 0,
      publishes = 0,
      jobReads = 0,
      accountReads = 0;
  Completer<PublishResult>? pendingPost;
  CancelToken? postCancel;
  Map<String, dynamic>? post;
  final seenScopes = <PlatformScope>[];
  @override
  void checkScope(PlatformScope scope) {}
  @override
  Future<PlatformNotificationPage> notifications(
    PlatformScope scope, {
    CancelToken? cancel,
  }) async => const PlatformNotificationPage();
  @override
  Future<List<PlatformInfo>> platforms(
    PlatformScope scope, {
    CancelToken? cancel,
  }) async => [
    PlatformInfo(
      key: 'douyin',
      display: '抖音',
      configured: configured,
      capabilities: ['login', 'publish'],
      maxGrantDays: 195,
    ),
  ];
  @override
  Future<List<PlatformAccount>> accounts(
    PlatformScope scope, {
    CancelToken? cancel,
  }) async {
    accountReads++;
    seenScopes.add(scope);
    return rows;
  }

  @override
  Future<List<PublishJob>> jobs(
    PlatformScope scope, {
    CancelToken? cancel,
  }) async => history;
  @override
  Future<String> authorize(
    PlatformScope scope,
    String platform, {
    CancelToken? cancel,
  }) async {
    starts++;
    seenScopes.add(scope);
    return authorizing;
  }

  @override
  Future<PlatformAccount> probe(
    PlatformScope scope,
    String id, {
    CancelToken? cancel,
  }) async {
    probes++;
    return rows.first;
  }

  @override
  Future<void> unbind(
    PlatformScope scope,
    String id, {
    CancelToken? cancel,
  }) async {
    deletes++;
    rows = [];
    seenScopes.add(scope);
  }

  @override
  Future<ResourcePage> videos(
    PlatformScope scope, {
    CancelToken? cancel,
  }) async => ResourcePage(
    items: [
      Resource.fromJson({
        'id': 'clip',
        'name': 'clip.mp4',
        'mime': 'video/mp4',
        'kind': 'video',
        'size': 2048,
      }),
    ],
    total: 1,
    hasMore: false,
  );
  @override
  Future<PublishResult> publish(
    PlatformScope scope, {
    required String assetId,
    required String title,
    required List<String> hashtags,
    required int privacy,
    CancelToken? cancel,
  }) async {
    publishes++;
    postCancel = cancel;
    seenScopes.add(scope);
    post = {
      'asset': assetId,
      'title': title,
      'tags': hashtags,
      'privacy': privacy,
    };
    if (pendingPost != null) return pendingPost!.future;
    history = [current];
    return PublishResult(job: current, schema: launch);
  }

  @override
  Future<PublishJob> job(
    PlatformScope scope,
    String id, {
    CancelToken? cancel,
  }) async {
    jobReads++;
    seenScopes.add(scope);
    if (jobError != null) throw jobError!;
    return current;
  }
}

class FakeLauncher extends PlatformLinkLauncher {
  final opened = <Uri>[];
  bool supported = true;
  @override
  Future<bool> open(Uri uri) async {
    opened.add(uri);
    return supported;
  }
}

Future<ProviderContainer> setup(
  WidgetTester tester,
  FakePlatformApi api,
  FakeLauncher launcher,
) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
  final prefs = await SharedPreferences.getInstance();
  await initializeDateFormatting('zh_CN');
  final bundle = I18nBundle({
    'zh-CN': {
      for (final ns in ['auth-center', 'common'])
        ns: jsonDecode(
          File('assets/locales/zh-CN/$ns.json').readAsStringSync(),
        ),
    },
  });
  final container = ProviderContainer(
    overrides: [
      assetUrlProvider('qr-asset').overrideWith(
        (ref) async => const AssetUrl(url: 'https://example.invalid/qr.png'),
      ),
      platformAccountsApiProvider.overrideWithValue(api),
      platformLinkLauncherProvider.overrideWithValue(launcher),
      i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
    ],
  );
  addTearDown(() async {
    await tester.pumpWidget(const SizedBox.shrink());
    container.dispose();
  });
  return container;
}

Future<void> mount(
  WidgetTester tester,
  ProviderContainer container, {
  bool manage = true,
  PlatformScope scope = scopeA,
  Widget? child,
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
        home:
            child ??
            AuthCenterScreen(
              key: ValueKey(scope),
              scope: scope,
              canManage: manage,
            ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  expect(tester.takeException(), isNull);
}

Future<void> tapText(WidgetTester tester, String text) async {
  final target = find.text(text).last;
  await tester.ensureVisible(target);
  await tester.tap(target);
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 100));
}

Future<void> fillPost(WidgetTester tester) async {
  await tapText(tester, '发布到抖音');
  await tester.pumpAndSettle();
  await tester.tap(find.byType(DropdownButtonFormField<String>));
  await tester.pumpAndSettle();
  await tester.tap(find.textContaining('clip.mp4').last);
  await tester.pumpAndSettle();
  await tester.enterText(find.byType(TextFormField).at(0), '中文标题');
  await tester.enterText(find.byType(TextFormField).at(1), '#装修 好物 #装修');
}

void pauseApp(WidgetTester tester) {
  for (final state in [
    AppLifecycleState.inactive,
    AppLifecycleState.hidden,
    AppLifecycleState.paused,
  ]) {
    tester.binding.handleAppLifecycleStateChanged(state);
  }
}

void resumeApp(WidgetTester tester) {
  for (final state in [
    AppLifecycleState.hidden,
    AppLifecycleState.inactive,
    AppLifecycleState.resumed,
  ]) {
    tester.binding.handleAppLifecycleStateChanged(state);
  }
}

void main() {
  testWidgets(
    'chat QR stays square and exposes actions without expanding tool details',
    (tester) async {
      final api = FakePlatformApi();
      final container = await setup(tester, api, FakeLauncher());
      final view = buildAssistantContentView([
        ChatMessage.fromJson({
          'id': 'm',
          'session_id': 's',
          'role': 'assistant',
          'finish': 'stop',
          'parts': [
            {
              'id': 'tool',
              'type': 'tool',
              'tool': 'douyin_publish',
              'status': 'completed',
              'metadata': {
                'asset_id': 'qr-asset',
                'job_id': 'job-a',
                'launchUrl': launch,
                'expiresAt': DateTime.now()
                    .add(const Duration(hours: 1))
                    .toIso8601String(),
              },
            },
            {
              'id': 'qr',
              'type': 'file',
              'asset_id': 'qr-asset',
              'mime_type': 'image/png',
              'path': '/workspace/douyin/qr.png',
              'transient': true,
              'relation': {
                'source_part_id': 'tool',
                'group_id': 'tool:tool',
                'role': 'result',
                'kind': 'qr_code',
                'label': '抖音投稿二维码',
              },
            },
          ],
        }),
      ], false);
      await mount(
        tester,
        container,
        child: Scaffold(
          body: SingleChildScrollView(
            child: ResultArtifacts(
              groups: view.resultGroups,
              verification: null,
            ),
          ),
        ),
      );
      expect(find.text('打开抖音发布'), findsOneWidget);
      expect(find.text('查看投稿结果'), findsOneWidget);
      final grid = tester.widget<GridView>(find.byType(GridView));
      expect(
        (grid.gridDelegate as SliverGridDelegateWithFixedCrossAxisCount)
            .childAspectRatio,
        1,
      );
      expect(
        tester.widget<Image>(find.byType(Image).first).fit,
        BoxFit.contain,
      );
    },
  );
  testWidgets(
    'owner sees account management; initial loads have no mutations',
    (tester) async {
      final api = FakePlatformApi();
      final launcher = FakeLauncher();
      final container = await setup(tester, api, launcher);
      await mount(tester, container);
      expect(find.text('授权中心'), findsOneWidget);
      expect(find.text('绑定账号'), findsOneWidget);
      expect(api.starts + api.probes + api.deletes + api.publishes, 0);
      expect(launcher.opened, isEmpty);
    },
  );

  testWidgets('member can check and post but cannot bind or unbind', (
    tester,
  ) async {
    final api = FakePlatformApi()..rows = [bound];
    final container = await setup(tester, api, FakeLauncher());
    await mount(tester, container, manage: false);
    expect(find.text('绑定账号'), findsNothing);
    expect(find.text('解绑'), findsNothing);
    expect(find.text('检测'), findsOneWidget);
    expect(find.textContaining('预计到期'), findsOneWidget);
    await tapText(tester, '检测');
    expect(api.probes, 1);
    await tapText(tester, '发布到抖音');
    expect(find.text('生成二维码'), findsOneWidget);
    expect(api.starts + api.deletes + api.publishes, 0);
    container.read(toastProvider.notifier).clear();
  });

  testWidgets(
    'unconfigured platforms allow unlinking but not external operations',
    (tester) async {
      final api = FakePlatformApi()
        ..configured = false
        ..rows = [bound];
      final container = await setup(tester, api, FakeLauncher());
      await mount(tester, container);
      await tapText(tester, '绑定账号');
      await tapText(tester, '发布到抖音');
      await tapText(tester, '检测');
      expect(api.starts + api.publishes + api.probes, 0);
      await tapText(tester, '解绑');
      expect(api.deletes, 0);
      final confirm = find.widgetWithText(FilledButton, '解绑');
      await tester.ensureVisible(confirm);
      await tester.tap(confirm);
      await tester.pumpAndSettle();
      expect(api.deletes, 1);
      expect(find.text('验收账号'), findsNothing);
      container.read(toastProvider.notifier).clear();
    },
  );

  testWidgets(
    'same-phone authorization is explicit; returning reloads server accounts',
    (tester) async {
      final api = FakePlatformApi();
      final launcher = FakeLauncher();
      final container = await setup(tester, api, launcher);
      await mount(tester, container);
      await tapText(tester, '绑定账号');
      expect(api.starts, 1);
      expect(launcher.opened, isEmpty);
      await tapText(tester, '在手机上授权抖音');
      expect(launcher.opened.single.queryParameters['is_call_app'], '1');
      expect(find.text('账号已绑定'), findsNothing);
      final reads = api.accountReads;
      pauseApp(tester);
      await tester.pump(const Duration(seconds: 30));
      expect(api.accountReads, reads);
      api.rows = [bound];
      resumeApp(tester);
      await tester.pumpAndSettle();
      expect(api.accountReads, greaterThan(reads));
      await tapText(tester, '返回查看账号状态');
      expect(find.text('验收账号'), findsOneWidget);
      expect(api.starts, 1);
    },
  );

  testWidgets(
    'unlink requires a confirmation and removes only the requested account',
    (tester) async {
      final api = FakePlatformApi()..rows = [bound];
      final container = await setup(tester, api, FakeLauncher());
      await mount(tester, container);
      await tapText(tester, '解绑');
      expect(api.deletes, 0);
      expect(find.text('解绑这个账号？'), findsOneWidget);
      final confirm = find.widgetWithText(FilledButton, '解绑');
      await tester.ensureVisible(confirm);
      await tester.tap(confirm);
      await tester.pumpAndSettle();
      expect(api.deletes, 1);
      expect(find.text('验收账号'), findsNothing);
      container.read(toastProvider.notifier).clear();
    },
  );

  testWidgets(
    'double tap cannot duplicate a pending publish; title/tags are preserved',
    (tester) async {
      final api = FakePlatformApi()
        ..rows = [bound]
        ..pendingPost = Completer<PublishResult>();
      final container = await setup(tester, api, FakeLauncher());
      await mount(tester, container);
      await fillPost(tester);
      await tapText(tester, '生成二维码');
      expect(api.publishes, 1);
      final buttons = find.byType(FilledButton);
      await tester.tap(buttons.last);
      await tester.pump();
      expect(api.publishes, 1);
      expect(api.post, {
        'asset': 'clip',
        'title': '中文标题',
        'tags': ['装修', '好物'],
        'privacy': 0,
      });
      api.pendingPost!.complete(
        PublishResult(job: api.current, schema: launch),
      );
      await tester.pumpAndSettle();
      expect(find.text('打开抖音发布'), findsOneWidget);
      expect(find.text('已在抖音发布'), findsNothing);
    },
  );

  testWidgets(
    'job polling stops in background and only server success completes the UI',
    (tester) async {
      final api = FakePlatformApi();
      final launcher = FakeLauncher();
      final container = await setup(tester, api, launcher);
      await mount(
        tester,
        container,
        child: Scaffold(
          body: SingleChildScrollView(
            child: PublishJobView(
              scope: scopeA,
              jobId: 'job-a',
              result: PublishResult(job: api.current, schema: launch),
            ),
          ),
        ),
      );
      await tapText(tester, '打开抖音发布');
      expect(launcher.opened, hasLength(1));
      expect(find.text('已在抖音发布'), findsNothing);
      pauseApp(tester);
      final count = api.jobReads;
      await tester.pump(const Duration(seconds: 30));
      expect(api.jobReads, count);
      api.current = const PublishJob(
        id: 'job-a',
        status: 'published',
        itemId: 'video-123',
      );
      resumeApp(tester);
      await tester.pumpAndSettle();
      expect(find.text('已在抖音发布'), findsOneWidget);
      final terminalCount = api.jobReads;
      await tester.pump(const Duration(seconds: 30));
      expect(api.jobReads, terminalCount);
      expect(find.text('打开抖音发布'), findsNothing);
    },
  );

  testWidgets(
    'expired, failed and network-error jobs never offer an actionable stale QR',
    (tester) async {
      final api = FakePlatformApi();
      final launcher = FakeLauncher();
      final container = await setup(tester, api, launcher);
      for (final status in ['expired', 'failed']) {
        api.current = PublishJob(id: 'job-a', status: status);
        await mount(
          tester,
          container,
          child: Scaffold(
            body: PublishJobView(
              key: ValueKey(status),
              scope: scopeA,
              jobId: 'job-a',
              result: PublishResult(job: pending(), schema: launch),
            ),
          ),
        );
        expect(find.text('打开抖音发布'), findsNothing);
      }
      api.current = pending(
        expires: DateTime.now().subtract(const Duration(seconds: 1)),
      );
      await mount(
        tester,
        container,
        child: Scaffold(
          body: PublishJobView(
            key: const ValueKey('local-expiry'),
            scope: scopeA,
            jobId: 'job-a',
            result: PublishResult(job: api.current, schema: launch),
          ),
        ),
      );
      expect(find.text('打开抖音发布'), findsNothing);
      api.jobError = ApiError(
        status: 503,
        code: 'HTTP_503',
        message: 'offline',
      );
      await mount(
        tester,
        container,
        child: Scaffold(
          body: SingleChildScrollView(
            child: PublishJobView(
              key: const ValueKey('offline'),
              scope: scopeA,
              jobId: 'job-a',
              result: PublishResult(job: pending(), schema: launch),
            ),
          ),
        ),
      );
      expect(find.text('打开抖音发布'), findsNothing);
      expect(find.textContaining('当前无法确认最新状态'), findsOneWidget);
      expect(api.publishes, 0);
    },
  );

  testWidgets(
    'cold entry recovers server jobs without regenerating signed links',
    (tester) async {
      final api = FakePlatformApi()..history = [pending()];
      final container = await setup(tester, api, FakeLauncher());
      await mount(tester, container);
      await tapText(tester, '验收视频');
      expect(api.jobReads, 1);
      expect(find.textContaining('已恢复服务器投稿记录'), findsOneWidget);
      expect(api.publishes, 0);
    },
  );

  testWidgets(
    'changing identity disposes pending publish and never displays its late QR',
    (tester) async {
      final api = FakePlatformApi()
        ..rows = [bound]
        ..pendingPost = Completer<PublishResult>();
      final container = await setup(tester, api, FakeLauncher());
      await mount(tester, container);
      await fillPost(tester);
      await tapText(tester, '生成二维码');
      await mount(tester, container, scope: scopeB);
      expect(api.postCancel?.isCancelled, isTrue);
      api.pendingPost!.complete(
        PublishResult(job: api.current, schema: launch),
      );
      await tester.pumpAndSettle();
      expect(find.text('打开抖音发布'), findsNothing);
      expect(api.publishes, 1);
      expect(api.seenScopes.last, scopeB);
    },
  );

  testWidgets('exported QR has opaque white quiet-zone pixels', (tester) async {
    final bytes = await tester.runAsync(() async {
      final image = await platformQrImage(launch);
      final raw = await image.toByteData(format: ui.ImageByteFormat.rawRgba);
      image.dispose();
      return raw!;
    });
    final rgba = bytes!.buffer.asUint8List();
    expect(rgba.sublist(0, 4), [255, 255, 255, 255]);
    expect(rgba.sublist((1024 * 1024 - 1) * 4), [255, 255, 255, 255]);
    expect(rgba.where((b) => b == 0), isNotEmpty);
  });
}
