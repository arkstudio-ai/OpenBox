import 'dart:io';

import 'package:bossip_mobile/features/chat/state/config_providers.dart';
import 'package:bossip_mobile/features/light/production/production_api.dart';
import 'package:bossip_mobile/features/light/production/production_page.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/appearance/theme_builder.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/app_config.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:integration_test/integration_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _DraftApi extends LightProductionApi {
  _DraftApi(this.draft) : super(Dio());

  Map<String, dynamic> draft;
  Map<String, Object?> edits = {};

  @override
  Future<LightProduction> get(String sessionId) async {
    expect(sessionId, draft['session_id']);
    return LightProduction.fromJson(draft);
  }

  @override
  Future<LightProduction> edit(
    LightProduction task,
    Map<String, Object?> fields,
  ) async {
    expect(task.revision, draft['revision']);
    edits.addAll(fields);
    draft = {...draft, ...fields, 'revision': task.revision + 1};
    return LightProduction.fromJson(draft);
  }
}

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const output = String.fromEnvironment('MODEL_SETTINGS_OUT');

  for (final kind in ProductionKind.values) {
    testWidgets('material settings persist for ${kind.id}', (tester) async {
      SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
      final prefs = await SharedPreferences.getInstance();
      final bundle = await I18nBundle.load();
      final config = AppConfig.fromJson({
        'models': [],
        'model_tiers': {
          'product_chat': true,
          'chat': [
            for (final tier in ['standard', 'creative', 'deep', 'fast'])
              {
                'tier': tier,
                'model': 'internal-$tier',
                'prices': {'input': '2', 'output': '10'},
              },
          ],
        },
      });
      final video = kind != ProductionKind.menu;
      final api = _DraftApi({
        'session_id': 'draft',
        'entry_id': kind.id,
        'revision': 1,
        'shop': '兴旺菜馆',
        'chat_tier': 'standard',
        if (video) 'video_tier': 'fast',
        if (video) 'available_video_tiers': ['fast', 'medium', 'high'],
        if (!video)
          'dishes': [
            {'name': '麻辣小龙虾', 'price': ''},
          ],
      });
      final router = GoRouter(
        initialLocation: '/',
        overridePlatformDefaultLocation: true,
        routes: [
          GoRoute(
            path: '/',
            builder: (_, _) => ProductionPage(
              sessionId: 'draft',
              initial: LightProduction.fromJson(api.draft),
            ),
          ),
          GoRoute(
            path: Paths.chat('draft'),
            builder: (_, _) => const Scaffold(body: Text('Agent: draft')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
            appConfigProvider.overrideWith((_) => config),
            lightProductionApiProvider.overrideWithValue(api),
          ],
          child: MaterialApp.router(
            routerConfig: router,
            debugShowCheckedModeBanner: false,
            theme: buildBossipTheme(BossipThemeName.default_, Brightness.light),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byType(ProductionPage), findsOneWidget);
      final textPicker = find.byKey(const Key('production-chat-tier-picker'));
      final videoPicker = find.byKey(const Key('production-tier-picker'));
      expect(textPicker, findsOneWidget);
      expect(videoPicker, video ? findsOneWidget : findsNothing);
      if (kind == ProductionKind.footage) {
        expect(find.text('用于 AI 补镜'), findsOneWidget);
      }
      if (output.isNotEmpty) {
        final folder = await Directory(output).create(recursive: true);
        final image = await binding.takeScreenshot(kind.id);
        await File('${folder.path}/${kind.id}.png').writeAsBytes(image);
      }
      await tester.ensureVisible(textPicker);
      await tester.tap(textPicker);
      await tester.pumpAndSettle();
      expect(find.textContaining('internal-'), findsNothing);
      for (final choice in ['标准', '创造', '深度', '快速']) {
        expect(find.text(choice).hitTestable(), findsWidgets);
      }
      await tester.tap(find.byKey(const Key('chat-tier-creative')));
      await tester.pumpAndSettle();
      if (video) {
        await tester.ensureVisible(videoPicker);
        await tester.tap(videoPicker);
        await tester.pumpAndSettle();
        await tester.tap(find.byKey(const Key('production-model-medium')));
        await tester.pumpAndSettle();
      }
      await tester.tap(find.byKey(const Key('production-advanced')));
      await tester.pumpAndSettle();
      expect(find.text('Agent: draft'), findsOneWidget);
      expect(api.edits['chat_tier'], 'creative');
      expect(api.edits['video_tier'], video ? 'medium' : isNull);
      await tester.pumpWidget(const SizedBox.shrink());
    });
  }
}
