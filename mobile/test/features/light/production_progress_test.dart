import 'package:bossip_mobile/features/light/production/production_api.dart';
import 'package:bossip_mobile/features/light/production/production_stages.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  testWidgets(
    'shows completed shots, retakes, and the final review separately',
    (tester) async {
      SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
      final prefs = await SharedPreferences.getInstance();
      final bundle = (await tester.runAsync(I18nBundle.load))!;
      final theme = ThemeData(
        extensions: [
          BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
        ],
      );

      Future<void> show(String phase, [Map<String, dynamic>? progress]) async {
        await tester.pumpWidget(
          ProviderScope(
            overrides: [
              i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
            ],
            child: MaterialApp(
              theme: theme,
              home: Scaffold(
                body: ProductionProgress(
                  task: LightProduction.fromJson({
                    'entry_id': 'video',
                    'status': 'working',
                    'phase': phase,
                    'shot_progress': ?progress,
                  }),
                ),
              ),
            ),
          ),
        );
        await tester.pump();
      }

      LinearProgressIndicator bar() => tester.widget<LinearProgressIndicator>(
        find.byKey(const Key('production-progress')),
      );
      await show('wan_shots', {
        'phase': 'wan_shots',
        'total': 10,
        'filmed': 6,
        'reviewed': 3,
        'retaking': 1,
      });
      expect(find.text('已拍摄 6/10 镜 · 初审通过 3/10 镜'), findsOneWidget);
      expect(find.text('正在补拍 1 个镜头'), findsOneWidget);
      expect(bar().value, 0.45);
      await show('compose', {
        'phase': 'wan_delivery',
        'total': 10,
        'filmed': 10,
        'reviewed': 10,
      });
      expect(find.text('正在核对成片画面和对白'), findsOneWidget);
      expect(find.textContaining('初审通过'), findsNothing);
      expect(bar().value, isNull);
      await show('compose');
      expect(find.text('正在合成视频'), findsOneWidget);
      expect(bar().value, isNull);
    },
  );
}
