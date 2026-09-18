import 'package:bossip_mobile/features/chat/widgets/composer/context_ring.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/app_config.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final config = {
    'id': 'm',
    'compaction': {
      'enabled': true,
      'threshold': 80000,
      'variants': {'high': 36000},
    },
  };

  test(
    'model metadata selects the runtime ceiling and tolerates older servers',
    () {
      final model = ModelInfo.fromJson(config);
      expect(model.compaction?.thresholdFor(null), 80000);
      expect(model.compaction?.thresholdFor('high'), 36000);
      expect(ModelInfo.fromJson({'id': 'old'}).compaction, isNull);
      expect(
        ModelInfo.fromJson({
          'id': 'off',
          'compaction': {'enabled': false, 'threshold': 80000},
        }).compaction?.thresholdFor(null),
        isNull,
      );
    },
  );

  testWidgets(
    'ring uses output reserve and does not promise disabled auto compression',
    (tester) async {
      SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
      final prefs = await SharedPreferences.getInstance();
      final bundle = await I18nBundle.load();
      final tokens = BossipTokens.resolve(
        BossipThemeName.default_,
        Brightness.light,
      );
      Widget page(int? threshold) => ProviderScope(
        overrides: [
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
        ],
        child: MaterialApp(
          theme: ThemeData(extensions: [tokens]),
          home: Scaffold(
            body: ContextRing(
              used: 60000,
              limit: 100000,
              compactionThreshold: threshold,
            ),
          ),
        ),
      );
      await tester.pumpWidget(page(60000));
      expect(
        tester
            .widget<CircularProgressIndicator>(
              find.byType(CircularProgressIndicator),
            )
            .color,
        tokens.danger,
      );
      expect(
        tester.widget<Tooltip>(find.byType(Tooltip)).message,
        contains('60%'),
      );
      expect(
        tester.widget<Tooltip>(find.byType(Tooltip)).message,
        contains('自动优化上下文'),
      );
      await tester.pumpWidget(page(null));
      expect(
        tester.widget<Tooltip>(find.byType(Tooltip)).message,
        isNot(contains('自动')),
      );
      expect(tester.takeException(), isNull);
    },
  );
}
