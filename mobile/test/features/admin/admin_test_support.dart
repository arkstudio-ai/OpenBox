import 'dart:async';

import 'package:bossip_mobile/features/admin/api/admin_api.dart';
import 'package:bossip_mobile/features/admin/models/admin_data.dart';
import 'package:bossip_mobile/features/admin/widgets/admin_layout.dart';
import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

const testScope = (userId: 'operator', workspaceId: 'scope-a');
const testDesktop = AdminRecord({
  'id': 'desktop-1',
  'desktop_id': 'desktop-1',
  'workspace_id': 'tenant-1',
  'workspace_name': '测试团队',
  'pool_state': 'assigned',
  'status': 'Running',
  'tunnel_state': 'up',
  'channel_state': 'ready',
  'ecd_end_users': null,
  'members': [
    {'id': 'member-1', 'username': 'Alice', 'email': 'alice@example.invalid'},
    {'id': 'member-2', 'username': 'Bob', 'email': 'bob@example.invalid'},
  ],
});
const testEntry = <String, dynamic>{
  'catalog_id': 'community:entry-1',
  'kind': 'skill',
  'origin': 'community',
  'title': '审核测试技能',
  'name': 'test-skill',
  'icon': '🧩',
  'description': '仅用于隔离测试',
  'listing': 'pending',
  'revision': 7,
  'content': '---\nname: test-skill\n---\n# Test',
  'has_archive': true,
  'published_at': '2026-09-10T00:00:00Z',
  'listing_note': '请核对归档',
  'author': {'username': 'Author', 'email': 'author@example.invalid'},
};
const testSkill = <String, dynamic>{
  'name': '团队技能',
  'names': ['团队技能', '附属技能'],
  'kind': 'skill',
  'install_dir': '/home/member-1/skills/bundle',
  'description': '测试合集',
  'source': 'manual',
  'removable': true,
};

Map<String, dynamic> testPage(List<Map<String, dynamic>> items, {int? total}) =>
    {'items': items, 'total': total ?? items.length, 'offset': 0, 'limit': 20};

class AdminHarness {
  AdminHarness() {
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) async {
          requests.add(options);
          try {
            final result = await responder(options);
            handler.resolve(
              Response<dynamic>(
                requestOptions: options,
                data: result,
                statusCode: 200,
              ),
            );
          } catch (error) {
            handler.reject(
              error is DioException
                  ? error
                  : DioException(requestOptions: options, error: error),
            );
          }
        },
      ),
    );
    api = AdminApi(dio, auth, workspace, testScope, () => allowed);
  }
  final dio = Dio(BaseOptions(baseUrl: 'https://example.invalid'));
  final auth = AuthSession()..userId = testScope.userId;
  final workspace = WorkspaceScope()..currentId = testScope.workspaceId;
  final requests = <RequestOptions>[];
  bool allowed = true;
  int skillsChanged = 0;
  late final AdminApi api;
  FutureOr<dynamic> Function(RequestOptions) responder = (options) {
    if (options.path.endsWith('/pool')) {
      return {
        'states': {'assigned': 1, 'prewarm': 0},
        'gates': <String, dynamic>{},
      };
    }
    if (options.path.endsWith('/snapshots/latest')) {
      return {'sources': <dynamic>[]};
    }
    if (options.path.endsWith('/providers')) {
      return {
        'items': [
          {'id': 'test', 'name': 'Test Pay'},
        ],
      };
    }
    if (options.path.endsWith('/desktops')) return testPage([testDesktop.data]);
    if (options.path.endsWith('/store') || options.path.endsWith('/review')) {
      return testPage([testEntry]);
    }
    return testPage([]);
  };
  void dispose() {
    api.dispose();
    dio.close(force: true);
  }
}

late I18nBundle adminBundle;
Future<void> mountAdmin(
  WidgetTester tester,
  AdminHarness harness,
  Widget child, {
  double width = 320,
  double height = 844,
  double textScale = 1,
  double safeBottom = 0,
  String language = 'zh-CN',
  Brightness brightness = Brightness.light,
  String? fontFamily,
  bool settle = true,
  bool wrapInScaffold = true,
  List<Override> overrides = const [],
}) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': language});
  final prefs = await SharedPreferences.getInstance();
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = Size(width, height);
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(harness.dispose);
  final tokens = BossipTokens.resolve(BossipThemeName.default_, brightness);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        adminApiProvider.overrideWithValue(harness.api),
        adminSkillsChangedProvider.overrideWithValue(
          () => harness.skillsChanged++,
        ),
        i18nProvider.overrideWith(() => I18nController(adminBundle, prefs)),
        ...overrides,
      ],
      child: MaterialApp(
        theme: ThemeData(
          brightness: brightness,
          fontFamily: fontFamily,
          scaffoldBackgroundColor: tokens.bg,
          colorScheme: ColorScheme.fromSeed(
            seedColor: tokens.ink,
            brightness: brightness,
            primary: tokens.ink,
            onPrimary: tokens.bg,
            surface: tokens.card,
            onSurface: tokens.ink,
          ),
          appBarTheme: AppBarTheme(
            backgroundColor: tokens.bg,
            foregroundColor: tokens.ink,
            elevation: 0,
            scrolledUnderElevation: 0,
            centerTitle: false,
          ),
          extensions: [tokens],
        ),
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context).copyWith(
            textScaler: TextScaler.linear(textScale),
            padding: EdgeInsets.only(bottom: safeBottom),
          ),
          child: AdminTheme(child: child!),
        ),
        home: wrapInScaffold ? Scaffold(body: child) : child,
      ),
    ),
  );
  if (settle) await tester.pumpAndSettle();
}
