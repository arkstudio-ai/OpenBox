import 'dart:async';

import 'package:bossip_mobile/features/auth_center/state/auth_center_providers.dart';
import 'package:bossip_mobile/features/auth_center/widgets/desktop_login_panel.dart';
import 'package:bossip_mobile/features/auth_center/widgets/notification_panel.dart';
import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/platform_accounts_api.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/platform_account.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:intl/date_symbol_data_local.dart';
import 'package:shared_preferences/shared_preferences.dart';

const _scope = (userId: 'user-a', workspaceId: 'workspace-a');
const _site = PlatformInfo(
  key: 'creator',
  display: '创作者中心',
  kind: 'desktop',
  configured: true,
);
const _bound = PlatformAccount(
  id: 'desktop-a',
  platform: 'creator',
  authKind: 'desktop_cookie',
  status: 'bound',
  nickname: '测试店主',
  probeDisplay: {'account_name': '测试店铺', 'role': '店主'},
);
const _unknown = PlatformAccount(
  id: 'desktop-a',
  platform: 'creator',
  authKind: 'desktop_cookie',
  status: 'unknown',
);

class _Api extends PlatformAccountsApi {
  _Api() : super(Dio(), AuthSession(), WorkspaceScope());
  int opens = 0, probes = 0, probeAll = 0, logouts = 0, reads = 0;
  PlatformAccount result = _unknown;
  Completer<PlatformAccount>? opening;
  Completer<PlatformAccount>? probing;
  Completer<void>? reading;
  CancelToken? openingCancel;
  CancelToken? probingCancel;
  List<PlatformNotification> notices = const [
    PlatformNotification(id: 'n1', title: '登录已失效', body: '请重新登录云电脑站点'),
  ];

  @override
  Future<PlatformAccount> openDesktopLogin(
    PlatformScope scope,
    String site, {
    CancelToken? cancel,
  }) async {
    opens++;
    openingCancel = cancel;
    return opening?.future ?? result;
  }

  @override
  Future<PlatformAccount> probe(
    PlatformScope scope,
    String id, {
    CancelToken? cancel,
  }) async {
    probes++;
    probingCancel = cancel;
    return probing?.future ?? result;
  }

  @override
  Future<List<PlatformAccount>> probeDesktopLogins(
    PlatformScope scope, {
    CancelToken? cancel,
  }) async {
    probeAll++;
    return [result];
  }

  @override
  Future<void> logoutDesktopLogin(
    PlatformScope scope,
    String id, {
    CancelToken? cancel,
  }) async {
    logouts++;
  }

  @override
  Future<PlatformNotificationPage> notifications(
    PlatformScope scope, {
    CancelToken? cancel,
  }) async => PlatformNotificationPage(items: notices, unread: notices.length);
  @override
  Future<void> markNotificationRead(
    PlatformScope scope,
    String id, {
    CancelToken? cancel,
  }) async {
    reads++;
    await reading?.future;
    notices = notices.where((n) => n.id != id).toList();
  }
}

late I18nBundle _bundle;
Future<void> _mount(
  WidgetTester tester,
  _Api api,
  Widget child, {
  DateTime Function()? now,
}) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
  final prefs = await SharedPreferences.getInstance();
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = const Size(320, 844);
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        if (now != null) desktopLoginClockProvider.overrideWithValue(now),
        platformAccountsApiProvider.overrideWithValue(api),
        i18nProvider.overrideWith(() => I18nController(_bundle, prefs)),
      ],
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: Scaffold(body: SingleChildScrollView(child: child)),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

Widget _panel({
  List<PlatformAccount> accounts = const [],
  bool manager = true,
  PlatformScope scope = _scope,
  VoidCallback? onOpen,
  List<PlatformInfo>? sites,
}) => DesktopLoginPanel(
  key: ValueKey(scope),
  scope: scope,
  sites: sites ?? [_site],
  accounts: accounts,
  canManage: manager,
  onChanged: () {},
  onOpenDesktop: onOpen ?? () {},
);

Future<void> _tap(WidgetTester tester, Finder target) async {
  await tester.ensureVisible(target);
  await tester.tap(target);
  await tester.pumpAndSettle();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() async {
    _bundle = await I18nBundle.load();
    await initializeDateFormatting('zh_CN');
  });

  test(
    'desktop wire fields preserve predicted expiry separately from OAuth',
    () {
      final row = PlatformAccount.fromJson({
        'id': 'one',
        'platform': 'creator',
        'status': 'bound',
        'authKind': 'desktop_cookie',
        'desktopId': 'desktop-1',
        'siteDisplay': '站点',
        'predictedExpiresAt': '2026-09-20T12:00:00Z',
        'probeDetail': {
          'display': {'account_name': '店铺'},
        },
      });
      expect(row.authKind, 'desktop_cookie');
      expect(row.desktopId, 'desktop-1');
      expect(row.probeDisplay['account_name'], '店铺');
      expect(row.predictedExpiresAt, DateTime.utc(2026, 9, 20, 12));
      expect(row.expectedExpiry, isNull);
      expect(PlatformInfo.fromJson({'key': 'old'}).kind, 'oauth');
    },
  );

  testWidgets('desktop and OAuth accounts stay separate on a narrow phone', (
    tester,
  ) async {
    final api = _Api();
    await _mount(
      tester,
      api,
      _panel(
        accounts: const [
          _bound,
          PlatformAccount(
            id: 'oauth',
            platform: 'creator',
            status: 'expired',
            nickname: 'OAuth 名称',
          ),
        ],
      ),
    );
    expect(find.text('测试店主'), findsOneWidget);
    expect(find.text('店铺：测试店铺'), findsOneWidget);
    expect(find.text('OAuth 名称'), findsNothing);
    expect(find.text('已登录'), findsOneWidget);
    // Site cards fill the panel even when their title and actions are short.
    final card = find.byKey(const ValueKey('desktop-site-creator'));
    expect(tester.getSize(card).width, closeTo(286, 0.1));
    expect(api.probes + api.probeAll + api.opens + api.logouts, 0);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'members can probe but cannot log out; unreconnoitred sites disable login',
    (tester) async {
      final api = _Api();
      await _mount(
        tester,
        api,
        _panel(
          manager: false,
          accounts: [_bound],
          sites: [
            _site,
            const PlatformInfo(
              key: 'pending',
              display: '待支持站点',
              configured: true,
              kind: 'desktop',
              reconPending: true,
            ),
          ],
        ),
      );
      expect(find.text('退出登录'), findsNothing);
      final login = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, '去登录'),
      );
      expect(login.onPressed, isNull);
      await _tap(tester, find.text('全部检测'));
      expect(api.probeAll, 1);
    },
  );

  testWidgets(
    'logout requires confirmation and cancellation leaves the session intact',
    (tester) async {
      final api = _Api();
      await _mount(tester, api, _panel(accounts: [_bound]));
      await _tap(tester, find.text('退出登录'));
      expect(api.logouts, 0);
      await _tap(tester, find.text('取消'));
      expect(api.logouts, 0);
      await _tap(tester, find.text('退出登录'));
      await _tap(tester, find.byKey(const ValueKey('confirm-desktop-logout')));
      expect(api.logouts, 1);
      expect(find.text('已退出该站点登录'), findsOneWidget);
    },
  );

  testWidgets('login opens the desktop once and stops polling when bound', (
    tester,
  ) async {
    final api = _Api();
    var opened = 0;
    await _mount(tester, api, _panel(onOpen: () => opened++));
    await _tap(tester, find.text('去登录'));
    expect(api.opens, 1);
    expect(opened, 1);
    expect(find.text('等待扫码…'), findsOneWidget);
    api.result = _bound;
    await tester.pump(const Duration(seconds: 5));
    await tester.pumpAndSettle();
    expect(api.probes, 1);
    expect(find.text('云电脑登录成功'), findsOneWidget);
    await tester.pump(const Duration(seconds: 20));
    expect(api.probes, 1);
  });

  testWidgets(
    'login polling pauses in background and expires after three minutes',
    (tester) async {
      final api = _Api();
      var now = DateTime.utc(2026, 9, 10);
      await _mount(tester, api, _panel(), now: () => now);
      await _tap(tester, find.text('去登录'));
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      await tester.pump(const Duration(seconds: 20));
      expect(api.probes, 0);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pump();
      await tester.pumpAndSettle();
      expect(api.probes, 1);
      now = now.add(const Duration(minutes: 4));
      await tester.pump(const Duration(seconds: 5));
      await tester.pumpAndSettle();
      expect(find.textContaining('三分钟内没有检测到登录'), findsOneWidget);
      final probes = api.probes;
      await tester.pump(const Duration(minutes: 4));
      expect(api.probes, probes);
    },
  );

  testWidgets(
    'slow probes never overlap and disposal cancels the pending request',
    (tester) async {
      final api = _Api()..probing = Completer<PlatformAccount>();
      await _mount(tester, api, _panel());
      await _tap(tester, find.text('去登录'));
      await tester.pump(const Duration(seconds: 5));
      await tester.pump(const Duration(seconds: 30));
      expect(api.probes, 1);
      await tester.pumpWidget(const SizedBox.shrink());
      expect(api.probingCancel!.isCancelled, isTrue);
      api.probing!.complete(_bound);
      await tester.pump();
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'scrolling the login panel off screen preserves its active polling',
    (tester) async {
      final api = _Api();
      final scroll = ScrollController();
      addTearDown(scroll.dispose);
      await _mount(
        tester,
        api,
        SizedBox(
          height: 650,
          child: ListView(
            controller: scroll,
            children: [_panel(), const SizedBox(height: 1800)],
          ),
        ),
      );
      await _tap(tester, find.text('去登录'));
      scroll.jumpTo(scroll.position.maxScrollExtent);
      await tester.pump();
      await tester.pump(const Duration(seconds: 5));
      await tester.pump();
      expect(api.probes, 1);
      scroll.jumpTo(0);
      await tester.pumpAndSettle();
      expect(find.text('等待扫码…'), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets('switching scope cancels login and ignores its late navigation', (
    tester,
  ) async {
    final api = _Api()..opening = Completer<PlatformAccount>();
    var opened = 0;
    await _mount(tester, api, _panel(onOpen: () => opened++));
    await _tap(tester, find.text('去登录'));
    await _mount(
      tester,
      api,
      _panel(scope: (userId: 'other', workspaceId: 'other')),
    );
    expect(api.openingCancel!.isCancelled, isTrue);
    api.opening!.complete(_unknown);
    await tester.pumpAndSettle();
    expect(opened, 0);
    expect(find.text('等待扫码…'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'notification reads are explicit, deduplicated and reflected after refresh',
    (tester) async {
      final api = _Api()..reading = Completer<void>();
      await _mount(tester, api, const NotificationPanel(scope: _scope));
      expect(find.text('登录已失效'), findsOneWidget);
      expect(api.reads, 0);
      final mark = find.byKey(const ValueKey('read-notification-n1'));
      await _tap(tester, mark);
      await _tap(tester, mark);
      expect(api.reads, 1);
      api.reading!.complete();
      await tester.pumpAndSettle();
      expect(find.text('登录已失效'), findsNothing);
    },
  );
}
